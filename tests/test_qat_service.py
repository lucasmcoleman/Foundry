"""T9/T10: QAT pipeline stage — service builder + UI config model.

Mirrors test_script_equivalence.py: the QATService.build_script must compile and
invoke the importable core/_qat_entry.py shim, and the embedded JSON config must
carry the run_qat keys (model/config/tier/dataset/out/lora_*). T10 adds the
UIConfig field-acceptance checks (extra='forbid').
"""

from pathlib import Path

import pytest

from services import QATService

ROOT = Path(__file__).resolve().parent.parent


QAT_COMMON = dict(
    model="org/some-model",
    config_path="./output/some-model/magicquant/search_results.json",
    tier="Q4",
    dataset="data/training.jsonl",
    out="./output/some-model/qat_adapters",
    lora_r=32,
    lora_alpha=64.0,
    epochs=1.0,
    max_steps=-1,
    lr=2e-4,
    max_seq_len=512,
)


def _svc():
    return QATService(ROOT, "python")


def test_qat_shim_compiles_and_invokes_entry():
    """The generated shim must compile and call the importable entry module."""
    svc = _svc()
    script = svc.build_script(**QAT_COMMON)
    compile(script, "<qat>", "exec")  # raises SyntaxError on a codegen typo
    assert "import _qat_entry" in script
    assert "_qat_entry.run(" in script


def test_qat_build_config_carries_run_qat_keys():
    """The config the service embeds must be exactly what run_qat consumes:
    model + config + tier + dataset + out + the LoRA/training hyperparams."""
    svc = _svc()
    cfg = svc.build_config(**QAT_COMMON)
    assert cfg["model"] == "org/some-model"
    assert cfg["config"] == "./output/some-model/magicquant/search_results.json"
    assert cfg["tier"] == "Q4"
    assert cfg["dataset"] == "data/training.jsonl"
    assert cfg["out"] == "./output/some-model/qat_adapters"
    assert cfg["lora_r"] == 32
    assert cfg["lora_alpha"] == 64.0
    assert cfg["epochs"] == 1.0
    assert cfg["max_steps"] == -1
    assert cfg["lr"] == 2e-4
    assert cfg["max_seq_len"] == 512


def test_qat_string_values_are_repr_escaped():
    """No bare interpolation of model/dataset into the shim: it goes through
    json.dumps then repr, so a nasty value still compiles and round-trips."""
    svc = _svc()
    nasty = "org/m'; import os; os.system('x') #"
    cfg = dict(QAT_COMMON)
    cfg["model"] = nasty
    script = svc.build_script(**cfg)
    compile(script, "<qat>", "exec")
    built = svc.build_config(**cfg)
    assert built["model"] == nasty


def test_qat_entry_imports_run_qat():
    """The entry body must wire magicquant.qat.run_qat (the QAT core)."""
    entry_src = (ROOT / "core" / "_qat_entry.py").read_text()
    assert "from magicquant.qat" in entry_src or "magicquant.qat.run_qat" in entry_src
    assert "run_qat" in entry_src
    assert "PIPELINE_STAGE_COMPLETE=qat" in entry_src


def test_qat_entry_module_is_importable_and_parses_config(tmp_path):
    """core/_qat_entry.py must be import-clean (stdlib-only at module scope) and
    expose a parse_config helper, like the other entry modules."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "_qat_entry_test", ROOT / "core" / "_qat_entry.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    cfg_path = tmp_path / "c.json"
    cfg_path.write_text('{"model": "m", "dataset": "d", "out": "o"}')
    cfg = mod.parse_config(str(cfg_path))
    assert cfg["model"] == "m"


# ── T9: pipeline wiring (stage present, gated off by default, CLI flags) ──────

def _pipeline():
    import importlib

    import core.pipeline as pl

    return importlib.reload(pl)


def test_pipeline_has_qat_stage_between_reap_and_magicquant():
    pl = _pipeline()
    names = [s for s, _ in pl.STAGES]
    assert "qat" in names
    # The spec flow is search-config-aware: QAT sits before magicquant generation.
    assert names.index("qat") < names.index("magicquant")
    assert names.index("reap") < names.index("qat")


def test_qat_is_disabled_by_default_on_pipeline_config():
    """Back-compat: a default PipelineConfig has no qat stage (gated off)."""
    pl = _pipeline()
    cfg = pl.PipelineConfig()
    assert cfg.qat is None


def test_qat_config_dataclass_defaults():
    pl = _pipeline()
    qc = pl.QATConfig()
    assert qc.tier == "Q4"
    assert qc.lora_r == 32
    assert qc.dataset == ""


def test_qat_cli_flag_enables_stage(monkeypatch):
    """--qat flips on the stage; the default (no flag) leaves it off."""
    pl = _pipeline()

    captured = {}

    def _fake_run_pipeline(cfg, **kwargs):
        captured["cfg"] = cfg
        return {"qat": True}

    monkeypatch.setattr(pl, "run_pipeline", _fake_run_pipeline)

    # Default: no --qat → cfg.qat stays None (stage gated off).
    pl.main(["--model", "org/m", "--no-export", "--no-magicquant",
             "--no-heretic", "--no-reap"])
    assert captured["cfg"].qat is None

    # --qat with a dataset enables the stage and carries its config.
    pl.main(["--model", "org/m", "--no-export", "--no-magicquant",
             "--no-heretic", "--no-reap",
             "--qat", "--qat-dataset", "data/qat.jsonl", "--qat-tier", "Q5"])
    qcfg = captured["cfg"].qat
    assert qcfg is not None
    assert qcfg.dataset == "data/qat.jsonl"
    assert qcfg.tier == "Q5"

    # --no-qat wins back off even if --qat is also passed.
    pl.main(["--model", "org/m", "--no-export", "--no-magicquant",
             "--no-heretic", "--no-reap", "--qat", "--no-qat"])
    assert captured["cfg"].qat is None


# ── T10: UIConfig accepts the QAT fields, rejects unknown ones ────────────────

def test_uiconfig_accepts_qat_fields():
    """UIConfig (extra='forbid') must accept the QAT persisted fields."""
    import app as app_module

    cfg = app_module.UIConfig(
        qat_enabled=True,
        qat_dataset="data/qat.jsonl",
        qat_tier="Q4",
        qat_lora_r=32,
        qat_lora_alpha=64.0,
        qat_epochs=1.0,
        qat_lr=2e-4,
    )
    assert cfg.qat_enabled is True
    assert cfg.qat_tier == "Q4"
    assert cfg.qat_lora_r == 32


def test_uiconfig_rejects_unknown_field():
    """extra='forbid' still holds: a bogus key is rejected."""
    import app as app_module
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        app_module.UIConfig(qat_bogus_field="x")


def test_qatcfg_model_defaults():
    """The per-stage QATCfg request model carries sane defaults (mirrors the
    other per-stage request models, which do not forbid extras)."""
    import app as app_module

    qc = app_module.QATCfg()
    assert qc.tier == "Q4"
    assert qc.lora_r == 32
    assert qc.epochs == 1.0
