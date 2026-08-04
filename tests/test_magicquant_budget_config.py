"""budget_gib must flow CLI -> dataclass -> service JSON unchanged, and the
measured+budget combination must be refused at config-build time (v2 does its
own real-perplexity verification; 'measured' would misrepresent what ran)."""
from pathlib import Path

import pytest

from core.pipeline import MagicQuantConfig
from core.services import MagicQuantService

BASE = dict(
    llamacpp_hint="", pipeline_root_str="/x", mq_source_override="/x/m.gguf",
    out_abs_str="/x/out", generations=50, population_size=100,
    target_base_quant="MXFP4_MOE", tiers_json='["Q4","Q5","Q6"]',
    model_name="TestModel",
)


def _svc():
    return MagicQuantService(Path("/x"), "python3")


def test_dataclass_default_is_none():
    assert MagicQuantConfig().budget_gib is None


def test_budget_gib_flows_into_config_json():
    cfg = _svc().build_config(**BASE, budget_gib=12.5)
    assert cfg["budget_gib"] == 12.5


def test_default_json_carries_none_for_v1_runs():
    cfg = _svc().build_config(**BASE)
    assert cfg["budget_gib"] is None


def test_measured_plus_budget_is_refused():
    with pytest.raises(ValueError) as e:
        _svc().build_config(**BASE, budget_gib=12.5, measured=True)
    msg = str(e.value)
    assert "--magicquant-budget-gib" in msg
    assert "--magicquant-measured" in msg


def test_measured_alone_is_allowed():
    """measured=True with no budget_gib must NOT be refused -- this is the
    ordinary --magicquant-measured run. A mutation that widens the guard from
    `budget_gib is not None and measured` to just `measured` would reject
    every measured run after training/export already burned hours; nothing
    else in the suite calls build_config with measured=True and no budget."""
    cfg = _svc().build_config(**BASE, measured=True)
    assert cfg["budget_gib"] is None


def test_cli_flag_parses_to_float():
    from core.pipeline import build_arg_parser
    args = build_arg_parser().parse_args(
        ["--model", "m", "--magicquant-budget-gib", "12.5"])
    assert args.magicquant_budget_gib == 12.5


def test_cli_flag_default_is_none():
    """The wiring guard at core/pipeline.py is `if args.magicquant_budget_gib
    is not None`, so a non-None default (e.g. 0.0 or 8) would silently put
    EVERY run into budget mode even when the flag is never passed."""
    from core.pipeline import build_arg_parser
    args = build_arg_parser().parse_args(["--model", "m"])
    assert args.magicquant_budget_gib is None


def test_stage_magicquant_rerun_when_only_budget_gib_changes(tmp_path, monkeypatch):
    """Functional guard for the cfg_hash line at core/pipeline.py:1204
    (`"budget_gib": mc.budget_gib`). If that key were dropped, an
    already-complete magicquant/ output dir would silently skip the stage
    even after budget_gib changed -- exactly the marker-staleness bug the
    config_hash mechanism exists to prevent (core/markers.py). Drives
    stage_magicquant() for real (Artifacts/PipelineConfig are plain
    dataclasses -- no GPU/network needed) with the actual subprocess launch
    (_run_stage_script) faked out, and asserts a second run that changes only
    budget_gib is NOT skipped."""
    import pipeline as pl

    out_dir = tmp_path / "out"
    artifacts = pl.Artifacts(str(out_dir))
    config = pl.PipelineConfig(output_dir=str(out_dir))
    src = tmp_path / "src.gguf"
    src.write_bytes(b"src")
    config.magicquant.source_model = str(src)

    calls = {"n": 0}

    def fake_run_stage_script(script, script_path, log, *, cfg_hash="", timeout=None, **kw):
        calls["n"] += 1
        gguf = artifacts.magicquant_dir / f"model-{calls['n']}.gguf"
        gguf.parent.mkdir(parents=True, exist_ok=True)
        gguf.write_bytes(b"x")
        return 0

    monkeypatch.setattr(pl, "_run_stage_script", fake_run_stage_script)

    def _log(msg, level="info"):
        pass

    config.magicquant.budget_gib = None
    assert pl.stage_magicquant(config, artifacts, _log) is True
    assert calls["n"] == 1, "first run should always execute (no marker yet)"

    config.magicquant.budget_gib = 8.0
    assert pl.stage_magicquant(config, artifacts, _log) is True
    assert calls["n"] == 2, (
        "stage_magicquant skipped a run after only budget_gib changed -- "
        "budget_gib must be part of the cfg_hash dict"
    )
