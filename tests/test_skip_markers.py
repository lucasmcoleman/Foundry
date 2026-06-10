"""M-skip-marker: completion-marker resume replaces existence-based skips.

The previous skip logic keyed on raw artifact existence (e.g. adapter_config.json,
which PEFT writes early), so a crash mid-stage false-passed the skip. The marker
gate requires a valid marker, a matching config hash, and a present + non-empty
key file.
"""

from pathlib import Path

import markers


def test_adapter_config_without_safetensors_does_not_skip(tmp_path):
    """The classic false-skip: adapter_config.json exists but the real weight
    file (adapter_model.safetensors) was never written. Must NOT skip."""
    stage_dir = tmp_path / "lora_adapters"
    stage_dir.mkdir()
    (stage_dir / "adapter_config.json").write_text("{}")  # PEFT writes this early
    key_file = stage_dir / "adapter_model.safetensors"  # never created
    cfg_hash = markers.config_hash({"model": "x"})
    assert markers.is_stage_complete(stage_dir, key_file, cfg_hash) is False


def test_valid_marker_with_matching_hash_skips(tmp_path):
    stage_dir = tmp_path / "lora_adapters"
    stage_dir.mkdir()
    key_file = stage_dir / "adapter_model.safetensors"
    key_file.write_bytes(b"not empty")
    cfg_hash = markers.config_hash({"model": "x"})
    markers.write_marker(stage_dir, "training", key_file, cfg_hash)
    assert markers.is_stage_complete(stage_dir, key_file, cfg_hash) is True


def test_marker_with_different_hash_does_not_skip(tmp_path):
    stage_dir = tmp_path / "lora_adapters"
    stage_dir.mkdir()
    key_file = stage_dir / "adapter_model.safetensors"
    key_file.write_bytes(b"not empty")
    markers.write_marker(stage_dir, "training", key_file, markers.config_hash({"model": "x"}))
    # A config change yields a different hash -> must re-run.
    other = markers.config_hash({"model": "y"})
    assert markers.is_stage_complete(stage_dir, key_file, other) is False


def test_marker_but_empty_key_file_does_not_skip(tmp_path):
    stage_dir = tmp_path / "merged_model"
    stage_dir.mkdir()
    key_file = stage_dir / "model.safetensors"
    key_file.write_bytes(b"")  # zero-byte
    cfg_hash = markers.config_hash({"x": 1})
    markers.write_marker(stage_dir, "export", key_file, cfg_hash)
    assert markers.is_stage_complete(stage_dir, key_file, cfg_hash) is False


def test_force_overrides_a_valid_marker(tmp_path):
    stage_dir = tmp_path / "lora_adapters"
    stage_dir.mkdir()
    key_file = stage_dir / "adapter_model.safetensors"
    key_file.write_bytes(b"x")
    cfg_hash = markers.config_hash({"a": 1})
    markers.write_marker(stage_dir, "training", key_file, cfg_hash)
    assert markers.is_stage_complete(stage_dir, key_file, cfg_hash, force=True) is False


def test_marker_written_atomically(tmp_path):
    """write_marker should leave no .tmp file behind."""
    stage_dir = tmp_path / "d"
    stage_dir.mkdir()
    kf = stage_dir / "k.safetensors"
    kf.write_bytes(b"x")
    markers.write_marker(stage_dir, "training", kf, "h")
    tmps = list(stage_dir.glob("*.tmp"))
    assert tmps == []
    assert (stage_dir / markers.MARKER_NAME).exists()
