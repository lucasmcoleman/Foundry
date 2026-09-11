"""Changing data/configuration must not reuse an unrelated optimizer state."""

import json

import pytest

from training_state import STATE_NAME, prepare_training_run


def _checkpoint(output, step):
    checkpoint = output / f"checkpoint-{step}"
    checkpoint.mkdir()
    (checkpoint / "trainer_state.json").write_text(json.dumps({"global_step": step}))
    for name in ("adapter_model.safetensors", "optimizer.pt", "scheduler.pt"):
        (checkpoint / name).write_bytes(b"checkpoint")
    return checkpoint


def test_matching_run_resumes_latest_complete_checkpoint(tmp_path):
    cfg = {"model_name": "org/model", "datasets": [], "lora_r": 8}
    assert prepare_training_run(str(tmp_path), cfg) is None
    complete = _checkpoint(tmp_path, 10)
    partial = _checkpoint(tmp_path, 20)
    (partial / "optimizer.pt").unlink()
    assert prepare_training_run(str(tmp_path), cfg) == str(complete)


def test_changed_config_preserves_existing_checkpoint_and_identity(tmp_path):
    cfg = {"model_name": "org/model", "datasets": [], "lora_r": 8}
    prepare_training_run(str(tmp_path), cfg)
    checkpoint = _checkpoint(tmp_path, 10)
    before = (tmp_path / STATE_NAME).read_bytes()
    with pytest.raises(ValueError, match="new output_dir"):
        prepare_training_run(str(tmp_path), {**cfg, "lora_r": 16})
    assert checkpoint.is_dir()
    assert (tmp_path / STATE_NAME).read_bytes() == before


def test_in_place_dataset_change_refuses_resume(tmp_path):
    dataset = tmp_path / "data.jsonl"
    dataset.write_text("first\n")
    cfg = {"model_name": "org/model", "datasets": [str(dataset)]}
    prepare_training_run(str(tmp_path), cfg)
    _checkpoint(tmp_path, 10)
    dataset.write_text("second\n")
    with pytest.raises(ValueError, match="data, or loss-mask provenance"):
        prepare_training_run(str(tmp_path), cfg)


def test_legacy_checkpoint_is_not_silently_adopted(tmp_path):
    checkpoint = _checkpoint(tmp_path, 10)
    with pytest.raises(ValueError, match="missing or different"):
        prepare_training_run(str(tmp_path), {"model_name": "org/model", "datasets": []})
    assert checkpoint.is_dir()
    assert not (tmp_path / STATE_NAME).exists()
