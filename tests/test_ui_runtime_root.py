"""Installed code roots must never become operator output/data directories."""

import sys

import app as ui


def test_source_checkout_retains_existing_root_default(tmp_path, monkeypatch):
    monkeypatch.delenv("FOUNDRY_WORK_DIR", raising=False)
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    (checkout / "pyproject.toml").write_text("[project]\nname='foundry'\n")
    assert ui._resolve_work_dir(checkout) == checkout


def test_installed_package_uses_cwd_or_explicit_work_root(tmp_path, monkeypatch):
    monkeypatch.delenv("FOUNDRY_WORK_DIR", raising=False)
    site = tmp_path / "site-packages"
    site.mkdir()
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    assert ui._resolve_work_dir(site) == work
    explicit = tmp_path / "operator-data"
    monkeypatch.setenv("FOUNDRY_WORK_DIR", str(explicit))
    assert ui._resolve_work_dir(site) == explicit


async def test_script_and_logs_use_runtime_root_without_writing_installed_code(tmp_path, monkeypatch):
    site, work = tmp_path / "site", tmp_path / "work"
    site.mkdir()
    work.mkdir()
    monkeypatch.setattr(ui, "FOUNDRY_ROOT", site)
    monkeypatch.setattr(ui, "FOUNDRY_DIR", work)
    monkeypatch.setattr(ui, "VENV_PYTHON", sys.executable)
    monkeypatch.setattr(ui, "state", ui.PipelineState())
    ui._assert_output_dir_contained(str(work / "output"))
    assert await ui.run_script("from pathlib import Path\nprint(Path.cwd())\n", "./output") == 0
    assert next((work / "output").glob("*.log")).read_text().strip() == str(work)
    assert list(site.iterdir()) == []


async def test_training_service_keeps_code_root_and_absolute_runtime_paths(tmp_path, monkeypatch):
    site, work = tmp_path / "site", tmp_path / "work"
    site.mkdir()
    work.mkdir()
    data = work / "train.jsonl"
    data.write_text('{"messages":[{"role":"user","content":"hello"},{"role":"assistant","content":"hi"}]}\n')
    monkeypatch.setattr(ui, "FOUNDRY_ROOT", site)
    monkeypatch.setattr(ui, "FOUNDRY_DIR", work)
    monkeypatch.setattr(ui, "state", ui.PipelineState())
    captured = {}

    class TrainingService:
        def __init__(self, root, python):
            captured["code_root"] = root

        def build_script(self, **cfg):
            captured.update(cfg)
            return "training stub"

    async def preflight(stage):
        return True

    async def run_script(script, output):
        adapters = work / "output" / "lora_adapters"
        adapters.mkdir(parents=True)
        (adapters / "adapter_model.safetensors").write_bytes(b"weights")
        return 0

    monkeypatch.setattr(ui, "TrainingService", TrainingService)
    monkeypatch.setattr(ui, "_mem_preflight", preflight)
    monkeypatch.setattr(ui, "run_script", run_script)
    cfg = ui.RunRequest(training=ui.TrainingCfg(datasets=["train.jsonl"], output_dir="./output"))
    assert await ui.do_training(cfg)
    assert captured["code_root"] == site
    assert captured["datasets"] == [str(data)]
    assert captured["output_dir"] == str(work / "output")
    assert captured["model_name"] == cfg.training.model_name  # Hub IDs stay IDs.


async def test_configured_output_root_is_used_by_run_history(tmp_path, monkeypatch):
    monkeypatch.setattr(ui, "FOUNDRY_DIR", tmp_path)
    monkeypatch.setattr(ui, "_DEFAULT_OUTPUT_DIR", "artifacts")
    model = tmp_path / "artifacts" / "Model"
    model.mkdir(parents=True)
    (model / "_stage_1.log").write_text("runtime log")
    response = await ui.get_run_log("Model", "_stage_1.log")
    assert response["content"] == "runtime log"
