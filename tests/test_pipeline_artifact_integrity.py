"""Exercise real stage resume decisions while substituting tiny subprocess outputs."""

import json
import os

import markers
import pipeline


def test_export_reruns_after_adapter_changes_in_place(tmp_path, monkeypatch):
    artifacts = pipeline.Artifacts(str(tmp_path))
    artifacts.lora_dir.mkdir()
    (artifacts.lora_dir / "adapter_config.json").write_text(json.dumps({
        "base_model_name_or_path": "org/model",
    }))
    adapter = artifacts.lora_dir / "adapter_model.safetensors"
    adapter.write_bytes(b"original")
    runs = []

    def fake_run(*args, **kwargs):
        runs.append(True)
        artifacts.merged_dir.mkdir(exist_ok=True)
        (artifacts.merged_dir / "model.safetensors").write_bytes(b"merged")
        return 0

    monkeypatch.setattr(pipeline, "_run_stage_script", fake_run)
    cfg = pipeline.PipelineConfig(output_dir=str(tmp_path))
    for _ in range(2):
        assert pipeline.stage_export(cfg, artifacts, lambda *a: None, skip_preflight=True)
    assert len(runs) == 1
    mtime = adapter.stat().st_mtime_ns
    adapter.write_bytes(b"modified")
    os.utime(adapter, ns=(mtime + 1, mtime + 1))
    assert pipeline.stage_export(cfg, artifacts, lambda *a: None, skip_preflight=True)
    assert len(runs) == 2


def test_successful_subprocess_without_adapter_weights_is_failure(tmp_path, monkeypatch):
    cfg = pipeline.PipelineConfig(output_dir=str(tmp_path), training=pipeline.TrainingConfig())
    artifacts = pipeline.Artifacts(str(tmp_path))
    artifacts.lora_dir.mkdir()
    (artifacts.lora_dir / "adapter_config.json").write_text("{}")
    monkeypatch.setattr(pipeline, "validate_dataset", lambda *a: True)
    monkeypatch.setattr(pipeline, "_run_stage_script", lambda *a, **kw: 0)
    assert not pipeline.stage_training(cfg, artifacts, lambda *a: None, skip_preflight=True)
    assert markers.read_marker(artifacts.lora_dir) is None


def test_qat_metadata_without_adapters_is_incomplete(tmp_path):
    meta = tmp_path / "qat_meta.json"
    meta.write_text(json.dumps({"adapter_file": "adapter_model.safetensors"}))
    assert not markers.artifacts_present(tmp_path, meta)
    adapter = tmp_path / "adapter_model.safetensors"
    adapter.write_bytes(b"adapter")
    assert markers.artifacts_present(tmp_path, meta)
    markers.write_marker(tmp_path, "qat", meta, "h")
    adapter.unlink()
    assert not markers.is_stage_complete(tmp_path, meta, "h")
