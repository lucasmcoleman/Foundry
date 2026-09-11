"""Prevent Qwen configs without MTP weights from producing phantom blocks."""

import importlib
import json
from pathlib import Path
import struct
from types import SimpleNamespace

import pytest

from conftest import fake_quantized_gguf
from conversion_source import (
    bf16_conversion_policy, cached_bf16_matches, record_bf16_conversion,
)


def _source(tmp_path, *, mtp=(), declared=1):
    root = tmp_path / "source"
    root.mkdir()
    (root / "config.json").write_text(json.dumps({
        "architectures": ["Qwen3_5MoeForConditionalGeneration"],
        "text_config": {"num_hidden_layers": 2, "mtp_num_hidden_layers": declared},
    }))
    names = [f"model.language_model.layers.{i}.input_layernorm.weight" for i in range(2)]
    names += list(mtp)
    header = {name: {"dtype": "F32", "shape": [1], "data_offsets": [i * 4, i * 4 + 4]}
              for i, name in enumerate(names)}
    raw = json.dumps(header).encode()
    raw += b" " * (-len(raw) % 8)
    shard = root / "model.safetensors"
    shard.write_bytes(struct.pack("<Q", len(raw)) + raw + bytes(4 * len(names)))
    (root / "model.safetensors.index.json").write_text(json.dumps({
        "weight_map": {name: shard.name for name in names},
    }))
    return root


def test_proven_absent_mtp_uses_official_converter_flag(tmp_path):
    policy = bf16_conversion_policy(_source(tmp_path))
    assert policy["flags"] == ["--no-mtp"]
    assert "model.safetensors" in policy["source"]["files"]


@pytest.mark.parametrize("prefix", ["mtp", "model.mtp", "model.language_model.mtp"])
def test_existing_mtp_weights_are_preserved(tmp_path, prefix):
    source = _source(tmp_path, mtp=[f"{prefix}.layers.0.input_layernorm.weight"])
    assert bf16_conversion_policy(source)["flags"] == []


@pytest.mark.parametrize("names,declared", [
    (["mtp.fc.weight"], 1),
    (["mtp.layers.0.input_layernorm.weight"], 2),
    (["mtp.layers.0.self_attn.q_proj.weight"], 1),
    (["nextn.layers.0.input_layernorm.weight"], 1),
])
def test_partial_or_ambiguous_mtp_is_not_silently_dropped(tmp_path, names, declared):
    with pytest.raises(ValueError, match="Cannot establish Qwen MTP inventory"):
        bf16_conversion_policy(_source(tmp_path, mtp=names, declared=declared))


@pytest.mark.parametrize("damage", ["missing-shard", "short-payload", "missing-index-entry", "extra-shard"])
def test_incomplete_inventory_cannot_license_no_mtp(tmp_path, damage):
    source = _source(tmp_path)
    shard = source / "model.safetensors"
    if damage == "missing-shard":
        shard.unlink()
    elif damage == "short-payload":
        shard.write_bytes(shard.read_bytes()[:-1])
    elif damage == "extra-shard":
        (source / "extra.safetensors").write_bytes(shard.read_bytes())
    else:
        index = source / "model.safetensors.index.json"
        data = json.loads(index.read_text())
        data["weight_map"].pop(next(iter(data["weight_map"])))
        index.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="Cannot establish Qwen MTP inventory"):
        bf16_conversion_policy(source)


def test_single_safetensors_file_without_index_is_complete(tmp_path):
    source = _source(tmp_path)
    (source / "model.safetensors.index.json").unlink()
    assert bf16_conversion_policy(source)["flags"] == ["--no-mtp"]


def test_other_architectures_do_not_gain_qwen_policy(tmp_path):
    source = tmp_path / "other"
    source.mkdir()
    (source / "config.json").write_text(json.dumps({
        "architectures": ["DeepseekV3ForCausalLM"], "mtp_num_hidden_layers": 1,
    }))
    assert bf16_conversion_policy(source) is None


@pytest.fixture(params=["_magicquant_entry", "_rocmfpx_entry"])
def conversion(request, tmp_path):
    entry = importlib.import_module(request.param)
    source = _source(tmp_path)
    converter = tmp_path / "llama.cpp"
    converter.mkdir()
    (converter / "convert_hf_to_gguf.py").write_text("")
    out = tmp_path / "out"
    out.mkdir()
    cached = out / "model-bf16.gguf"

    def run():
        return entry._ensure_bf16_gguf(str(converter), str(source), out, "Nex")
    return run, source, cached


def test_legacy_phantom_cache_reconverts_then_matching_receipt_resumes(conversion, monkeypatch):
    run, source, cached = conversion
    cached.write_bytes(b"legacy phantom MTP")
    calls = []

    def fake_run(argv):
        calls.append(argv)
        assert "--no-mtp" in argv
        pending = Path(argv[argv.index("--outfile") + 1])
        assert pending != cached and pending.suffix != ".gguf"
        assert cached.read_bytes() == b"legacy phantom MTP"
        fake_quantized_gguf(pending)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr("subprocess.run", fake_run)
    assert run() == str(cached)
    assert run() == str(cached)
    assert len(calls) == 1
    assert cached_bf16_matches(cached, bf16_conversion_policy(source))
    assert not list(cached.parent.glob("*.partial"))


@pytest.mark.parametrize("failure", ["nonzero", "invalid-output", "launch-error", "receipt-commit"])
def test_conversion_failure_preserves_cache_and_receipt(conversion, monkeypatch, failure):
    run, _, cached = conversion
    cached.write_bytes(b"previous cache")
    receipt = cached.with_name(cached.name + ".conversion.json")
    receipt.write_text('{"previous": true}')
    original_replace = Path.replace

    def fail_receipt_replace(self, target):
        if Path(target) == receipt:
            raise OSError("cannot commit receipt")
        return original_replace(self, target)

    def fake_run(argv):
        pending = Path(argv[argv.index("--outfile") + 1])
        if failure == "launch-error":
            raise OSError("cannot execute converter")
        if failure == "invalid-output":
            pending.write_bytes(b"GGUF")
        else:
            fake_quantized_gguf(pending)
        return SimpleNamespace(returncode=1 if failure == "nonzero" else 0)

    monkeypatch.setattr("subprocess.run", fake_run)
    if failure == "receipt-commit":
        monkeypatch.setattr(Path, "replace", fail_receipt_replace)
    with pytest.raises((OSError, ValueError, RuntimeError)):
        run()
    assert cached.read_bytes() == b"previous cache"
    assert receipt.read_text() == '{"previous": true}'
    assert sorted(p.name for p in cached.parent.iterdir()) == sorted([cached.name, receipt.name])


def test_external_conversion_receipt_detects_changed_source_or_artifact(tmp_path):
    source = _source(tmp_path)
    cached = fake_quantized_gguf(tmp_path / "model-bf16.gguf")
    policy = bf16_conversion_policy(source)
    record_bf16_conversion(cached, policy)
    assert cached_bf16_matches(cached, policy)
    shard = source / "model.safetensors"
    with shard.open("ab") as handle:
        handle.write(b"modified")
    assert not cached_bf16_matches(cached, bf16_conversion_policy(source))
    cached.write_bytes(cached.read_bytes() + b"modified")
    assert not cached_bf16_matches(cached, policy)
