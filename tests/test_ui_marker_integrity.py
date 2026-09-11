"""UI resume observes changed inputs and validates complete output artifacts."""
from pathlib import Path

import pytest

import app as ui


@pytest.fixture
def isolated_ui(monkeypatch, tmp_path):
    monkeypatch.setattr(ui, "state", ui.PipelineState())
    monkeypatch.setattr(ui, "FOUNDRY_DIR", tmp_path)

    async def preflight(stage):
        return True

    monkeypatch.setattr(ui, "_mem_preflight", preflight)
    return tmp_path


@pytest.mark.parametrize("stage,key", [
    ("training", "adapter_model.safetensors"),
    ("export", "model.safetensors"),
    ("heretic", "model.safetensors"),
    ("reap", "model.safetensors"),
    ("qat", "qat_meta.json"),
    ("magicquant", "model.gguf"),
    ("rocmfpx", "model.gguf"),
])
async def test_zero_exit_without_weights_is_failure(isolated_ui, stage, key):
    path = isolated_ui / stage
    assert not await ui._finish_artifact_stage(stage, path, path / key, "hash", 0)
    assert ui.state.stages[stage] == ui.StageStatus.FAILED
    assert not (path / "_stage_complete.json").exists()


async def test_missing_shard_prevents_ui_success_marker(isolated_ui):
    stage_dir = isolated_ui / "merged"
    stage_dir.mkdir()
    key = stage_dir / "model-01.safetensors"
    key.write_bytes(b"weights")
    (stage_dir / "model.safetensors.index.json").write_text(
        '{"weight_map":{"one":"model-01.safetensors","two":"missing.safetensors"}}'
    )
    assert not await ui._finish_artifact_stage("export", stage_dir, key, "hash", 0)
    assert not (stage_dir / "_stage_complete.json").exists()


async def test_marker_is_invalidated_before_ui_rerun(isolated_ui):
    stage_dir = isolated_ui / "merged"
    stage_dir.mkdir()
    key = stage_dir / "model.safetensors"
    key.write_bytes(b"weights")
    ui.markers.write_marker(stage_dir, "export", key, "old")
    done, _ = await ui._check_marker(
        "export", "Export", stage_dir, "new", key_glob="*.safetensors",
        default_key_name="model.safetensors",
    )
    assert not done
    assert not ui.markers.marker_path(stage_dir).exists()


def test_training_hash_tracks_local_dataset_and_model_changes(isolated_ui):
    model = isolated_ui / "model"
    model.mkdir()
    weights = model / "model.safetensors"
    weights.write_bytes(b"a")
    dataset = isolated_ui / "training.jsonl"
    dataset.write_text("first")
    cfg = ui.TrainingCfg(model_name="model", datasets=["training.jsonl"])
    before = ui._training_marker_hash(cfg)
    dataset.write_text("changed dataset")
    after_dataset = ui._training_marker_hash(cfg)
    assert before != after_dataset
    weights.write_bytes(b"changed weights")
    assert ui._training_marker_hash(cfg) != after_dataset


@pytest.mark.parametrize("stage", ["export", "heretic", "reap", "qat", "magicquant", "rocmfpx"])
async def test_each_ui_stage_hash_tracks_source_content(isolated_ui, monkeypatch, stage):
    output = isolated_ui / "run"
    model = isolated_ui / "source"
    model.mkdir()
    weights = model / "model.safetensors"
    weights.write_bytes(b"first")
    dataset = isolated_ui / "data.jsonl"
    dataset.write_text("data")
    quant_config = isolated_ui / "search_results.json"
    quant_config.write_text("{}")
    if stage == "heretic":
        model = output / "merged_model"
    elif stage == "reap":
        model = output / "heretic_model"
    model.mkdir(parents=True, exist_ok=True)
    weights = model / "model.safetensors"
    weights.write_bytes(b"first")
    cfg = ui.RunRequest(
        enabled_stages=[stage],
        training=ui.TrainingCfg(model_name=str(model), output_dir=str(output)),
        export=ui.ExportCfg(source_model=str(model)),
        heretic=ui.HereticCfg(), reap=ui.ReapCfg(),
        qat=ui.QATCfg(dataset=str(dataset), config_source=str(quant_config)),
        magicquant=ui.MagicQuantCfg(source_model=str(model)),
        rocmfpx=ui.ROCmFPXCfg(source_model=str(model)),
    )
    hashes = []

    async def capture(stage, display, stage_dir, cfg_hash, **kwargs):
        hashes.append(cfg_hash)
        return True, Path(stage_dir) / kwargs["default_key_name"]

    monkeypatch.setattr(ui, "_check_marker", capture)
    runner = getattr(ui, f"do_{stage}")
    assert await runner(cfg)
    weights.write_bytes(b"changed weights")
    assert await runner(cfg)
    assert hashes[0] != hashes[1]


async def test_gguf_export_passthrough_is_complete_and_resumes(isolated_ui, monkeypatch):
    out = isolated_ui / "run"
    source = isolated_ui / "source-BF16.gguf"
    source.write_bytes(b"gguf weights")
    cfg = ui.RunRequest(
        enabled_stages=["export"],
        training=ui.TrainingCfg(output_dir=str(out)),
        export=ui.ExportCfg(source_model=str(source)),
    )
    calls = []

    async def fake_export(script, output):
        calls.append(script)
        out.mkdir(parents=True, exist_ok=True)
        (out / "model-bf16.gguf").symlink_to(source)
        return 0

    monkeypatch.setattr(ui, "run_script", fake_export)
    assert await ui.do_export(cfg)
    assert ui.state.stages["export"] == ui.StageStatus.COMPLETE
    assert not (out / "merged_model").exists()
    assert ui.markers.read_marker(out)["stage"] == "export"
    assert await ui.do_export(cfg)
    assert len(calls) == 1


async def test_zero_exit_from_export_without_either_artifact_fails(isolated_ui, monkeypatch):
    cfg = ui.RunRequest(
        enabled_stages=["export"],
        training=ui.TrainingCfg(output_dir=str(isolated_ui / "run")),
        export=ui.ExportCfg(source_model="org/model"),
    )

    async def fake_export(script, output):
        return 0

    monkeypatch.setattr(ui, "run_script", fake_export)
    assert not await ui.do_export(cfg)
    assert ui.state.stages["export"] == ui.StageStatus.FAILED
