"""The budget branch must (a) call v2 with a correctly-mapped V2Config,
(b) loudly ignore inapplicable v1 knobs, (c) rename the output to the
-BUDGET-<N>GiB publish convention, (d) turn BudgetInfeasibleError into a
clear stage failure naming the floor, and (e) still pass the renamed file
through the PPL smoke gate — the one sanity check between a bad quant and
upload."""
import json
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
        # Deliberately NOT the real format ("BUDGET-{b:g}GiB") -- a sentinel
        # so the filename assertion below can only pass if _run_budget calls
        # THIS imported function, not some inline local re-derivation that
        # happens to reproduce the real format (the cross-repo contract
        # forbids re-deriving the tier-key format locally).
        return f"TIERKEY-{b:g}"

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
        # A real cfg dict (services.MagicQuantService.build_config) always
        # carries these -- enable_kl defaults True since 2026-08-07. Stock
        # values here so the "no false positive" test below reflects what a
        # real stock budget cfg actually looks like, not an absent key.
        "enable_kl": True,
        "kl_weight": 0.1,
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
    assert Path(out).name == "TestModel-TIERKEY-12.5.gguf"
    assert Path(out).exists()


def test_inapplicable_v1_knobs_are_loudly_ignored(monkeypatch, tmp_path, capsys):
    _install_fake_v2(monkeypatch, tmp_path)
    from core import _magicquant_entry as entry
    entry._run_budget(_cfg(tmp_path), str(tmp_path / "s.gguf"), None)
    text = capsys.readouterr().out
    assert "seed" in text and "ignored" in text.lower()
    # generations=50 in _cfg() equals _BUDGET_KEY_DEFAULTS["generations"] --
    # the `val not in (None, False, "", default)` filter must suppress it.
    # Pins the no-false-positive-noise half: deleting that filter would
    # announce every listed key, including ones left at their default.
    assert "generations" not in text


def test_stock_enable_kl_default_emits_no_ignored_note(monkeypatch, tmp_path, capsys):
    """enable_kl now defaults True (services.build_config); a stock budget
    cfg (enable_kl=True, kl_weight=0.1, both left at default) must not be
    misreported as a user-set 'ignored key' -- _BUDGET_KEY_DEFAULTS must
    know enable_kl's real default is True, not the False/None/"" generic
    unset sentinel every other listed key happens to share."""
    _install_fake_v2(monkeypatch, tmp_path)
    from core import _magicquant_entry as entry
    entry._run_budget(_cfg(tmp_path), str(tmp_path / "s.gguf"), None)
    text = capsys.readouterr().out
    assert "enable_kl" not in text
    assert "kl_weight" not in text


def test_infeasible_budget_exits_with_floor_in_message(monkeypatch, tmp_path, capsys):
    _install_fake_v2(monkeypatch, tmp_path, raise_infeasible=True)
    from core import _magicquant_entry as entry
    with pytest.raises(SystemExit):
        entry._run_budget(_cfg(tmp_path), str(tmp_path / "s.gguf"), None)
    text = capsys.readouterr().out
    # Labeled phrasing, not bare digits -- pins WHICH number is the request
    # and WHICH is the floor. Swapping e.budget_bytes/e.min_bytes in the
    # message would still contain "11.2" and "8" somewhere in the text, so
    # bare substring checks don't catch a role swap; these phrase-anchored
    # checks do.
    assert "budget 8.00 GiB is below the floor" in text
    assert "minimum achievable with the enabled schemes is 11.20 GiB" in text


def test_none_final_model_is_a_clear_error_not_a_typeerror(monkeypatch, tmp_path, capsys):
    """run_budget_search returns final_model=None when the budget anchor's
    build failed but a neighbor anchor succeeded (only all-anchors-failed
    raises). Path(None) would be an opaque TypeError."""
    _install_fake_v2(monkeypatch, tmp_path)
    import sys as _sys
    _sys.modules["magicquant.v2"].run_budget_search = (
        lambda cfg: {"final_model": None, "budget_gb": 12.5})
    from core import _magicquant_entry as entry
    with pytest.raises(SystemExit):
        entry._run_budget(_cfg(tmp_path), str(tmp_path / "s.gguf"), None)
    assert "v2_results.json" in capsys.readouterr().out  # points at failures record


def _run_cfg(tmp_path, **over):
    """cfg for a full run() call (not just _run_budget): needs the keys
    run()'s shared preamble reads before ever reaching the budget branch."""
    root = str(Path(__file__).resolve().parent.parent)
    cfg = _cfg(tmp_path, **over)
    cfg.setdefault("pipeline_root", root)
    cfg.setdefault("pipeline_root_str", root)
    return cfg


def test_run_dispatches_budget_with_converted_source_and_honors_smoke_gate(
    monkeypatch, tmp_path, capsys,
):
    """run()-level guard (Important 1): every existing budget test calls
    _run_budget() directly, so a refactor that moved the budget dispatch
    ABOVE should_convert_source_to_gguf (reintroducing the unconverted-
    safetensors path that caused the qwen3_5 silent-garbage-quant incident),
    or that returned early from the budget branch skipping the PPL smoke
    gate, would pass the whole suite. This test drives run() itself and pins
    both:
      (a) the value _ensure_bf16_gguf returns (the POST-conversion source)
          is what reaches V2Config.source_model_path -- not the raw
          resolve_source() path;
      (b) a failing smoke_test_gguf aborts via SystemExit and never prints
          PIPELINE_STAGE_COMPLETE, even through the budget dispatch path.
    """
    calls = _install_fake_v2(monkeypatch, tmp_path)

    # run()'s shared preamble imports magicquant.orchestrator unconditionally
    # (before the budget/v1 branch decision) -- must be stubbed even though
    # the budget path never instantiates it.
    fake_pkg = types.ModuleType("magicquant")
    fake_orch_mod = types.ModuleType("magicquant.orchestrator")
    fake_orch_mod.MagicQuantOrchestrator = object
    monkeypatch.setitem(sys.modules, "magicquant", fake_pkg)
    monkeypatch.setitem(sys.modules, "magicquant.orchestrator", fake_orch_mod)

    import ppl_smoke
    from core import _magicquant_entry as entry

    monkeypatch.setattr(entry, "find_llamacpp", lambda hint="": "/fake/llamacpp")

    raw_source_dir = tmp_path / "raw_src"
    raw_source_dir.mkdir()
    monkeypatch.setattr(entry, "resolve_source", lambda *a, **k: str(raw_source_dir))

    converted_source = "/converted/model-bf16.gguf"
    monkeypatch.setattr(
        entry, "_ensure_bf16_gguf",
        lambda llamacpp_dir, source, out_dir, model_name=None: converted_source,
    )

    cfg_path = tmp_path / "run_cfg.json"
    cfg_path.write_text(json.dumps(_run_cfg(tmp_path)))

    # (b) failing smoke test must abort the budget path before completion.
    monkeypatch.setattr(ppl_smoke, "smoke_test_gguf", lambda *a, **k: False)

    with pytest.raises(SystemExit):
        entry.run(str(cfg_path))

    out = capsys.readouterr().out
    assert "PIPELINE_STAGE_COMPLETE" not in out

    # (a) V2Config got the POST-conversion source, not resolve_source()'s
    # raw directory path -- proves the budget branch runs AFTER conversion.
    assert calls["v2config"]["source_model_path"] == converted_source
    assert calls["v2config"]["source_model_path"] != str(raw_source_dir)
