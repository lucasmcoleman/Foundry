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
    assert "budget" in msg and "measured" in msg


def test_cli_flag_parses_to_float():
    from core.pipeline import build_arg_parser  # if absent, see note below
    args = build_arg_parser().parse_args(
        ["--model", "m", "--magicquant-budget-gib", "12.5"])
    assert args.magicquant_budget_gib == 12.5
