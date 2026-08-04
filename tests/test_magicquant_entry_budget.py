"""The budget branch must (a) call v2 with a correctly-mapped V2Config,
(b) loudly ignore inapplicable v1 knobs, (c) rename the output to the
-BUDGET-<N>GiB publish convention, (d) turn BudgetInfeasibleError into a
clear stage failure naming the floor, and (e) still pass the renamed file
through the PPL smoke gate — the one sanity check between a bad quant and
upload."""
import sys
import types
from pathlib import Path

import pytest

# _magicquant_entry imports magicquant.orchestrator at module scope of run();
# the budget tests stub the whole magicquant.v2 surface instead.


class _FakeInfeasible(RuntimeError):
    def __init__(self, budget_bytes, min_bytes):
        self.budget_bytes = budget_bytes
        self.min_bytes = min_bytes
        super().__init__("infeasible")


def _install_fake_v2(monkeypatch, tmp_path, *, raise_infeasible=False):
    calls = {}
    fake = types.ModuleType("magicquant.v2")

    class V2Config:
        def __init__(self, **kw):
            calls["v2config"] = kw

    def run_budget_search(cfg):
        if raise_infeasible:
            raise _FakeInfeasible(8 * 1024**3, int(11.2 * 1024**3))
        out = tmp_path / "magicquant" / "model-v2-budget-12.40gb.gguf"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"GGUF")
        calls["ran"] = True
        return {"final_model": str(out), "budget_gb": 12.5}

    def budget_tier_key(b):
        return f"BUDGET-{b:g}GiB"

    fake.V2Config = V2Config
    fake.run_budget_search = run_budget_search
    fake.BudgetInfeasibleError = _FakeInfeasible
    fake.budget_tier_key = budget_tier_key
    monkeypatch.setitem(sys.modules, "magicquant.v2", fake)
    return calls


def _cfg(tmp_path, **over):
    cfg = {
        "budget_gib": 12.5,
        "mq_source_override": str(tmp_path / "src.gguf"),
        "out_abs_str": str(tmp_path),
        "llamacpp_hint": "",
        "model_name": "TestModel",
        "use_imatrix": True,
        "imatrix_corpus": None,
        "measurement_chunks": 40,
        "rocmfpx_schemes": False,
        "generations": 50,
        "population_size": 100,
        "seed": 7,           # inapplicable to v2 -> must be loudly ignored
        "tiers_json": '["Q4","Q5","Q6"]',
        "verify": False,
    }
    cfg.update(over)
    return cfg


def test_budget_branch_maps_config_and_renames(monkeypatch, tmp_path, capsys):
    calls = _install_fake_v2(monkeypatch, tmp_path)
    from core import _magicquant_entry as entry
    resolved_source = str(tmp_path / "resolved-bf16.gguf")
    out = entry._run_budget(_cfg(tmp_path), resolved_source, "/opt/llama.cpp")
    assert calls["ran"]
    kw = calls["v2config"]
    assert kw["source_model_path"] == resolved_source   # resolved, not raw cfg hint
    assert kw["llamacpp_path"] == "/opt/llama.cpp"      # resolved, not llamacpp_hint
    assert kw["budget_gb"] == 12.5
    assert kw["output_dir"].endswith("/magicquant")
    assert kw["model_name"] == "TestModel"
    assert kw["measurement_chunks"] == 40
    assert Path(out).name == "TestModel-BUDGET-12.5GiB.gguf"
    assert Path(out).exists()


def test_inapplicable_v1_knobs_are_loudly_ignored(monkeypatch, tmp_path, capsys):
    _install_fake_v2(monkeypatch, tmp_path)
    from core import _magicquant_entry as entry
    entry._run_budget(_cfg(tmp_path), str(tmp_path / "s.gguf"), None)
    text = capsys.readouterr().out
    assert "seed" in text and "ignored" in text.lower()


def test_infeasible_budget_exits_with_floor_in_message(monkeypatch, tmp_path, capsys):
    _install_fake_v2(monkeypatch, tmp_path, raise_infeasible=True)
    from core import _magicquant_entry as entry
    with pytest.raises(SystemExit):
        entry._run_budget(_cfg(tmp_path), str(tmp_path / "s.gguf"), None)
    text = capsys.readouterr().out
    assert "11.2" in text      # the floor, in GiB
    assert "8" in text         # the requested budget


def test_none_final_model_is_a_clear_error_not_a_typeerror(monkeypatch, tmp_path, capsys):
    """run_budget_search returns final_model=None when the budget anchor's
    build failed but a neighbor anchor succeeded (only all-anchors-failed
    raises). Path(None) would be an opaque TypeError."""
    calls = _install_fake_v2(monkeypatch, tmp_path)
    import sys as _sys
    _sys.modules["magicquant.v2"].run_budget_search = (
        lambda cfg: {"final_model": None, "budget_gb": 12.5})
    from core import _magicquant_entry as entry
    with pytest.raises(SystemExit):
        entry._run_budget(_cfg(tmp_path), str(tmp_path / "s.gguf"), None)
    assert "v2_results.json" in capsys.readouterr().out  # points at failures record
