"""Native quantizer failures must never publish partial GGUF files."""

import os
from pathlib import Path
import stat
import struct
from types import SimpleNamespace

import pytest

import _rocmfpx_entry as entry
from conftest import fake_quantized_gguf


@pytest.fixture(params=["preset", "tensor-types"])
def quantization(request, tmp_path, monkeypatch):
    monkeypatch.setattr(entry, "validate_types_supported", lambda *args: None)
    cleared = []
    monkeypatch.setattr(entry, "_clear_refusal", lambda *args, **kw: cleared.append(kw))
    if request.param == "preset":
        final = tmp_path / "model-Q4_0_ROCMFP4.gguf"

        def run():
            return entry._quantize_preset(
                "rocmfp4", tmp_path, "model", Path("/stub/llama-quantize"),
                "source.gguf", "",
            )
    else:
        final = tmp_path / "model-ROCMFPX-MQ-Q4.gguf"

        def run():
            return entry._run_ttf_quantize(
                spec="mq-q4", key="Q4", lines=["^x$=Q4_0_ROCMFP4"],
                base_type="Q4_0_ROCMFP4", rocmfpx_out_dir=tmp_path,
                model_name="model", quantize_bin=Path("/stub/llama-quantize"),
                bf16_gguf="source.gguf", imatrix="", allow_requantize=False,
            )
    return run, final, cleared


@pytest.mark.parametrize("returncode,output", [
    (-9, "zeros"), (1, "valid"), (0, "zeros"),
    (0, "truncated"), (0, "missing"), (0, "no-tensors"),
    (0, "header-only"), (0, "no-payload"),
])
def test_failed_output_preserves_existing_file(
    quantization, monkeypatch, capsys, returncode, output,
):
    run, final, cleared = quantization
    fake_quantized_gguf(final, b"old" * 6)
    previous = final.read_bytes()
    staged = []

    def fake_run(cmd):
        pending = Path(cmd[-2])
        staged.append(pending)
        assert pending.parent == final.parent
        assert pending.suffix != ".gguf"
        assert final.read_bytes() == previous
        if output == "valid":
            fake_quantized_gguf(pending)
        elif output == "zeros":
            pending.write_bytes(bytes(1024))
        elif output == "truncated":
            pending.write_bytes(b"GGUF")
        elif output == "missing":
            pending.unlink()
        elif output == "header-only":
            pending.write_bytes(struct.pack("<4sIQQ", b"GGUF", 3, 1, 0) + b"x")
        elif output == "no-payload":
            fake_quantized_gguf(pending, b"")
        else:
            fake_quantized_gguf(pending)
            data = pending.read_bytes()
            pending.write_bytes(data[:8] + bytes(8) + data[16:])
        return SimpleNamespace(returncode=returncode)

    monkeypatch.setattr("subprocess.run", fake_run)
    assert run() is None
    assert final.read_bytes() == previous
    assert list(final.parent.glob("*.gguf")) == [final]
    assert staged and not any(path.exists() for path in staged)
    assert cleared == []
    log = capsys.readouterr().out
    assert "quantize failed" in log
    if returncode == -9:
        assert "signal 9 (SIGKILL)" in log
        assert "check system logs" in log


def test_success_replaces_only_after_valid_fork_gguf_is_written(quantization, monkeypatch):
    run, final, _ = quantization
    fake_quantized_gguf(final, b"old" * 6)
    final.chmod(0o644)
    previous = final.read_bytes()
    staged = []
    expected = []

    def fake_run(cmd):
        pending = Path(cmd[-2])
        staged.append(pending)
        assert pending != final and pending.suffix != ".gguf"
        fake_quantized_gguf(pending, b"new" * 6)
        expected.append(pending.read_bytes())
        assert final.read_bytes() == previous
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr("subprocess.run", fake_run)
    assert run() == final
    assert final.read_bytes() == expected[0]
    assert stat.S_IMODE(final.stat().st_mode) == 0o644
    assert not staged[0].exists()


@pytest.mark.parametrize("error", [OSError("cannot execute"), KeyboardInterrupt()])
def test_launch_error_or_interruption_cleans_staging(quantization, monkeypatch, error):
    run, final, _ = quantization
    staged = []

    def fake_run(cmd):
        pending = Path(cmd[-2])
        staged.append(pending)
        pending.write_bytes(bytes(128))
        raise error

    monkeypatch.setattr("subprocess.run", fake_run)
    if isinstance(error, KeyboardInterrupt):
        with pytest.raises(KeyboardInterrupt):
            run()
    else:
        assert run() is None
    assert not final.exists()
    assert staged and not any(path.exists() for path in staged)


def test_failed_atomic_replace_preserves_existing_file(quantization, monkeypatch):
    run, final, cleared = quantization
    fake_quantized_gguf(final, b"old" * 6)
    previous = final.read_bytes()
    staged = []

    def fake_run(cmd):
        pending = Path(cmd[-2])
        staged.append(pending)
        fake_quantized_gguf(pending)
        return SimpleNamespace(returncode=0)

    def fail_replace(self, target):
        raise OSError("replace failed")

    monkeypatch.setattr("subprocess.run", fake_run)
    monkeypatch.setattr(Path, "replace", fail_replace)
    assert run() is None
    assert final.read_bytes() == previous
    assert not staged[0].exists()
    assert cleared == []


def test_new_output_honors_umask(quantization, monkeypatch):
    run, final, _ = quantization

    def fake_run(cmd):
        fake_quantized_gguf(Path(cmd[-2]))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr("subprocess.run", fake_run)
    previous_umask = os.umask(0o027)
    try:
        assert run() == final
    finally:
        os.umask(previous_umask)
    assert stat.S_IMODE(final.stat().st_mode) == 0o640


def _structured_gguf(*, alignment=64, offset=0, dimensions=1, tensor_count=1,
                     name_length=1, shape=32, array_count=2, version=3):
    def string(value):
        return struct.pack("<Q", len(value)) + value

    # Exercise scalar metadata and the tokenizer-style string-array path.
    metadata = string(b"general.alignment") + struct.pack("<II", 4, alignment)
    metadata += string(b"tokens") + struct.pack("<IIQ", 9, 8, array_count)
    metadata += string(b"hello") + string(b"world")
    header = struct.pack("<4sIQQ", b"GGUF", version, tensor_count, 2)
    tensor = struct.pack("<Q", name_length) + b"x"
    tensor += struct.pack("<IQIQ", dimensions, shape, 100, offset)
    data = header + metadata + tensor
    return data + bytes((-len(data)) % max(alignment, 1)) + bytes(18)


def test_structure_validation_accepts_custom_type_and_alignment(tmp_path):
    path = tmp_path / "fork.gguf"
    path.write_bytes(_structured_gguf())
    entry._validate_quantized_gguf(path)


@pytest.mark.parametrize("override", [
    {"alignment": 0}, {"alignment": 3}, {"offset": 1}, {"offset": 4096},
    {"dimensions": 0}, {"dimensions": 5}, {"shape": 2**63},
    {"tensor_count": 2**63}, {"name_length": 2**63},
    {"array_count": 2**63}, {"version": 999},
])
def test_structure_validation_rejects_invalid_tables_before_reading_payload(tmp_path, override):
    path = tmp_path / "broken.gguf"
    path.write_bytes(_structured_gguf(**override))
    with pytest.raises(ValueError):
        entry._validate_quantized_gguf(path)
