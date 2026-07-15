"""derive_base_model: upload-stage attribution must track the run's real
source model, not the persisted training form field (which goes stale when
switching models with training disabled).
"""
import json

import app


def _req(model_name="stale/Previous-Model", source_model="", enabled=("export", "upload")):
    return app.RunRequest(
        training=app.TrainingCfg(model_name=model_name),
        export=app.ExportCfg(source_model=source_model),
        enabled_stages=list(enabled),
    )


def test_persisted_export_cfg_wins(tmp_path):
    (tmp_path / "_export_entry.cfg.json").write_text(
        json.dumps({"base_model_id": "org/True-Source"}))
    cfg = _req(enabled=("training", "export", "upload"))
    assert app.derive_base_model(cfg, tmp_path) == "org/True-Source"


def test_training_enabled_uses_training_model(tmp_path):
    cfg = _req(model_name="org/Being-Trained", enabled=("training", "upload"))
    assert app.derive_base_model(cfg, tmp_path) == "org/Being-Trained"


def test_training_disabled_uses_export_source_not_stale_form(tmp_path):
    cfg = _req(source_model="org/Actual-Source")
    assert app.derive_base_model(cfg, tmp_path) == "org/Actual-Source"


def test_lora_source_resolves_to_adapter_base(tmp_path):
    lora_dir = tmp_path / "adapters"
    lora_dir.mkdir()
    (lora_dir / "adapter_config.json").write_text(
        json.dumps({"base_model_name_or_path": "org/Adapter-Base"}))
    cfg = _req(source_model=str(lora_dir))
    assert app.derive_base_model(cfg, tmp_path) == "org/Adapter-Base"


def test_corrupt_export_cfg_falls_through(tmp_path):
    (tmp_path / "_export_entry.cfg.json").write_text("{not json")
    cfg = _req(source_model="org/Actual-Source")
    assert app.derive_base_model(cfg, tmp_path) == "org/Actual-Source"


def test_last_resort_is_training_model(tmp_path):
    cfg = _req()
    assert app.derive_base_model(cfg, tmp_path) == "stale/Previous-Model"
