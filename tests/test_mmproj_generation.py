"""Auto-mmproj: MagicQuant exports a vision projector for multimodal source
models so the text quants can be served with image input (quant + mmproj = VL).

Covers _is_vision_model (detection) and _maybe_generate_mmproj (invokes the
converter with --mmproj for vision models, skips text-only, best-effort).
"""
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "core"))
import _magicquant_entry as mq  # noqa: E402


def _write(d: Path, name: str, obj) -> None:
    (d / name).write_text(json.dumps(obj) if not isinstance(obj, str) else obj)


# ── _is_vision_model ─────────────────────────────────────────────────────

def test_vision_detected_via_preprocessor_config(tmp_path):
    _write(tmp_path, "config.json", {"model_type": "qwen3_5"})
    _write(tmp_path, "preprocessor_config.json", {"image_processor_type": "Qwen"})
    assert mq._is_vision_model(str(tmp_path)) is True


def test_vision_detected_via_vision_config(tmp_path):
    _write(tmp_path, "config.json", {"model_type": "qwen3_5", "vision_config": {"depth": 32}})
    assert mq._is_vision_model(str(tmp_path)) is True


def test_text_only_not_detected(tmp_path):
    _write(tmp_path, "config.json", {"model_type": "qwen3", "hidden_size": 4096})
    assert mq._is_vision_model(str(tmp_path)) is False


def test_gguf_source_not_a_vision_dir(tmp_path):
    assert mq._is_vision_model(str(tmp_path / "model-bf16.gguf")) is False


# ── _maybe_generate_mmproj ───────────────────────────────────────────────

def test_generates_mmproj_for_vision_model(tmp_path, monkeypatch):
    src = tmp_path / "src"; src.mkdir()
    _write(src, "config.json", {"vision_config": {}})
    _write(src, "preprocessor_config.json", {})
    out = tmp_path / "out"; out.mkdir()

    called = {}
    def fake_run(argv, *a, **k):
        called["argv"] = argv
        # simulate the converter writing the output file
        outfile = argv[argv.index("--outfile") + 1]
        Path(outfile).write_bytes(b"GGUF-mmproj")
        class R: returncode = 0
        return R()
    monkeypatch.setattr(mq, "_find_convert_script", lambda d: Path("/x/convert_hf_to_gguf.py"))
    monkeypatch.setattr("subprocess.run", fake_run)

    mq._maybe_generate_mmproj("/llama", str(src), out, "MyModel")

    assert "--mmproj" in called["argv"]
    assert (out / "mmproj" / "mmproj-MyModel-f16.gguf").exists()
    # general.name metadata bug: convert_hf_to_gguf.py falls back to the
    # source directory's name ("Source", since run dirs stage the checkpoint
    # under a literal source/ dir) unless told the real name explicitly.
    assert "--model-name" in called["argv"]
    assert called["argv"][called["argv"].index("--model-name") + 1] == "MyModel"


def test_mmproj_omits_model_name_flag_when_not_supplied(tmp_path, monkeypatch):
    src = tmp_path / "src"; src.mkdir()
    _write(src, "preprocessor_config.json", {})
    out = tmp_path / "out"; out.mkdir()

    called = {}
    def fake_run(argv, *a, **k):
        called["argv"] = argv
        outfile = Path(argv[argv.index("--outfile") + 1])
        outfile.write_bytes(b"GGUF-mmproj")
        class R: returncode = 0
        return R()
    monkeypatch.setattr(mq, "_find_convert_script", lambda d: Path("/x/convert_hf_to_gguf.py"))
    monkeypatch.setattr("subprocess.run", fake_run)

    # empty model_name (the call site's default when cfg has none) must not
    # add a bare/empty --model-name flag.
    mq._maybe_generate_mmproj("/llama", str(src), out, "")

    assert "--model-name" not in called["argv"]


def test_skips_text_only_model(tmp_path, monkeypatch):
    src = tmp_path / "src"; src.mkdir()
    _write(src, "config.json", {"model_type": "qwen3"})
    out = tmp_path / "out"; out.mkdir()
    ran = {"convert": False}
    monkeypatch.setattr("subprocess.run", lambda *a, **k: ran.__setitem__("convert", True))
    mq._maybe_generate_mmproj("/llama", str(src), out, "M")
    assert ran["convert"] is False
    assert not (out / "mmproj").exists()


def test_reuses_existing_mmproj(tmp_path, monkeypatch):
    src = tmp_path / "src"; src.mkdir()
    _write(src, "preprocessor_config.json", {})
    out = tmp_path / "out"; (out / "mmproj").mkdir(parents=True)
    (out / "mmproj" / "mmproj-M-f16.gguf").write_bytes(b"exists")
    monkeypatch.setattr(mq, "_find_convert_script", lambda d: Path("/x/c.py"))
    ran = {"convert": False}
    monkeypatch.setattr("subprocess.run", lambda *a, **k: ran.__setitem__("convert", True))
    mq._maybe_generate_mmproj("/llama", str(src), out, "M")
    assert ran["convert"] is False  # reused, not regenerated


def test_best_effort_never_raises_on_converter_failure(tmp_path, monkeypatch):
    src = tmp_path / "src"; src.mkdir()
    _write(src, "preprocessor_config.json", {})
    out = tmp_path / "out"; out.mkdir()
    monkeypatch.setattr(mq, "_find_convert_script", lambda d: Path("/x/c.py"))
    def boom(*a, **k):
        raise RuntimeError("converter blew up")
    monkeypatch.setattr("subprocess.run", boom)
    # must not raise — text quants are still valid without the mmproj
    mq._maybe_generate_mmproj("/llama", str(src), out, "M")


# ── _ensure_bf16_gguf: --model-name threading ────────────────────────────
#
# Same general.name bug, main BF16-conversion path: run dirs stage the
# checkpoint under a literal source/ directory, and convert_hf_to_gguf.py
# derives general.name from that directory name when not told otherwise.

def test_ensure_bf16_gguf_includes_model_name_flag_when_given(tmp_path, monkeypatch):
    src = tmp_path / "src"; src.mkdir()
    out = tmp_path / "out"; out.mkdir()

    called = {}
    def fake_run(argv, *a, **k):
        called["argv"] = argv
        outfile = Path(argv[argv.index("--outfile") + 1])
        outfile.write_bytes(b"GGUF")
        class R: returncode = 0
        return R()
    monkeypatch.setattr(mq, "_find_convert_script", lambda d: Path("/x/convert_hf_to_gguf.py"))
    monkeypatch.setattr("subprocess.run", fake_run)

    mq._ensure_bf16_gguf("/llama", str(src), out, "MyModel")

    assert "--model-name" in called["argv"]
    assert called["argv"][called["argv"].index("--model-name") + 1] == "MyModel"


def test_ensure_bf16_gguf_omits_model_name_flag_when_not_given(tmp_path, monkeypatch):
    src = tmp_path / "src"; src.mkdir()
    out = tmp_path / "out"; out.mkdir()

    called = {}
    def fake_run(argv, *a, **k):
        called["argv"] = argv
        outfile = Path(argv[argv.index("--outfile") + 1])
        outfile.write_bytes(b"GGUF")
        class R: returncode = 0
        return R()
    monkeypatch.setattr(mq, "_find_convert_script", lambda d: Path("/x/convert_hf_to_gguf.py"))
    monkeypatch.setattr("subprocess.run", fake_run)

    # old-behavior default: no model_name argument at all.
    mq._ensure_bf16_gguf("/llama", str(src), out)

    assert "--model-name" not in called["argv"]


def test_no_converter_found_skips_gracefully(tmp_path, monkeypatch):
    src = tmp_path / "src"; src.mkdir()
    _write(src, "vision_config" and "config.json", {"vision_config": {}})
    out = tmp_path / "out"; out.mkdir()
    monkeypatch.setattr(mq, "_find_convert_script", lambda d: None)
    mq._maybe_generate_mmproj("/llama", str(src), out, "M")  # no raise
    assert not (out / "mmproj" / "mmproj-M-f16.gguf").exists()
