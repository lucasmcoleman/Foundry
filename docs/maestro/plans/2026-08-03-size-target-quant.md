# Size-Target Quantization (MagicQuant v2 → Foundry + Consumers) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use maestro:subagent-driven-development (recommended) or maestro:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking; if your harness has a native task system, mirror one task per plan task there as well — the plan file remains the durable record.

**Goal:** Expose MagicQuant v2's budget-constrained search (`run_budget_search`) through Foundry end to end — CLI, config, entry shim, UI, publish criteria, model cards — and make ROCmFPX + QAT able to consume a budget build.

**Architecture:** Zero changes to v1 code paths (seed-pinned fixture must stay byte-identical). MagicQuant gains one strictly-additive module (`magicquant/v2/interchange.py`) that merges a `BUDGET-<N>GiB` pseudo-tier into `search_results.json`. Foundry gains a `budget_gib` knob threaded through its four standard layers, a third entry-shim branch, a budget publish path, per-tensor ROCmFPX rendering, and card disclosure.

**Tech Stack:** Python 3.12, pytest (offline; heavy imports guarded with `pytest.importorskip`), MagicQuant imported as plain `magicquant.*` (Foundry CI provides it via `.magicquant-src` on PYTHONPATH).

## Global Constraints

- Spec: `docs/maestro/specs/2026-08-03-size-target-quant-design.md` — read it before implementing.
- Units are **GiB** everywhere user-facing: flag `--magicquant-budget-gib`, field `budget_gib`, filename `-BUDGET-<N>GiB.gguf`. (`V2Config.budget_gb` is already GiB internally: `int(budget_gb * 1024**3)`.)
- Tolerance: `BUDGET_TOLERANCE = 0.02`, defined ONCE in `core/publish_criteria.py`, imported everywhere else.
- Budget tier key format: `budget_tier_key(budget_gib)` → `f"BUDGET-{budget_gib:g}GiB"` (`100.0` → `BUDGET-100GiB`, `12.5` → `BUDGET-12.5GiB`). Defined ONCE in MagicQuant (`magicquant/v2/interchange.py`); Foundry imports it.
- MagicQuant changes must be strictly additive: no edit to any existing function's behavior, only new module + one call added at the end of `run_budget_search` + `__init__` exports.
- v1's `search_results.json` `tiered` entries must survive a budget merge byte-identical.
- All new tests run offline (no GPU, no llama.cpp binary, no network). Foundry CI command: `python -m pytest tests/ --ignore=tests/test_training_integration.py -m "not slow and not gpu" -q`. MagicQuant CI runs `pytest` per its `.github/workflows/ci.yml`.
- Both repos' `make lint` must pass. Commit per task in the repo the task touches.
- After ALL Foundry code changes land: `sudo systemctl restart foundry-ui` (stale long-lived service re-runs old behavior — standing ops rule).

---

### Task 1: MagicQuant interchange module (`budget_tier_key` + merge-write)

**Dispatch:** INDEPENDENT

**Files:**
- Create: `/server/programming/MagicQuant/magicquant/v2/interchange.py`
- Modify: `/server/programming/MagicQuant/magicquant/v2/search.py` (one call at end of `run_budget_search`, before the final `return results`)
- Modify: `/server/programming/MagicQuant/magicquant/v2/__init__.py` (exports)
- Test: `/server/programming/MagicQuant/tests/test_v2_interchange.py`

**Interfaces:**
- Consumes: `run_budget_search`'s `results` dict (keys verified 2026-08-03: `algo`, `budget_gb`, `group_summary` ({group: scheme_name}), `allocation` (from `Allocation.to_json()`: `assignment` {tensor: scheme}, `actual_types`, `total_bytes`, `budget_bytes`), `anchors` (list; `anchors[0]` is the budget point, has `actual_bytes`), `baseline_ppl`); `CURRENT_TIER_SCHEME_VERSION` (= 2) from `magicquant.quant.tiers`.
- Produces: `budget_tier_key(budget_gib: float) -> str`; `write_interchange_block(search_results_path: Path, results: dict) -> str` (returns the tier key written). Both exported from `magicquant.v2`. The written block shape (consumed by Tasks 3, 5, 6, 8):

```python
tiered[key] = {
    "config": results["group_summary"],                  # {group: scheme_name}
    "tensor_config": results["allocation"]["assignment"],  # {tensor: scheme_name}
    "tensor_actual_types": results["allocation"]["actual_types"],
    "algo": "v2-budget",
    "budget_bytes": results["allocation"]["budget_bytes"],
    "predicted_bytes": results["allocation"]["total_bytes"],
    "actual_bytes": <anchors[0]["actual_bytes"] if present else None>,
    "ppl": <anchors[0]["ppl"] if present else None>,   # measured PPL of the shipped build
    "baseline_ppl": results["baseline_ppl"],
}
```

(The `ppl` field is what Task 5's card renders — sourcing it from the
interchange block means the card never depends on run-scratch files
(`v2_results.json`/`frontier.json`) that cleanup deletes.)

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_v2_interchange.py
"""The interchange block is the on-disk contract letting v1 consumers (QAT,
ROCmFPX mq-hybrid, Foundry publish) read a v2 budget build. Reader and writer
live in different repos and never run in the same interpreter — pin the shape.
"""
import json
from pathlib import Path

from magicquant.quant.tiers import CURRENT_TIER_SCHEME_VERSION
from magicquant.v2.interchange import budget_tier_key, write_interchange_block

RESULTS = {
    "algo": "v2-budget",
    "budget_gb": 12.5,
    "baseline_ppl": 6.01,
    "group_summary": {"U": "MXFP4_MOE", "Q": "Q5_K"},
    "allocation": {
        "assignment": {"blk.0.ffn_up.weight": "MXFP4_MOE",
                       "blk.0.attn_q.weight": "Q5_K"},
        "actual_types": {"blk.0.ffn_up.weight": "MXFP4",
                         "blk.0.attn_q.weight": "Q5_K"},
        "total_bytes": 13_000_000_000,
        "budget_bytes": 13_421_772_800,
    },
    "anchors": [{"tag": "budget", "actual_bytes": 13_100_000_000, "ppl": 6.11}],
}


def test_key_format_trims_trailing_zeros():
    assert budget_tier_key(100.0) == "BUDGET-100GiB"
    assert budget_tier_key(12.5) == "BUDGET-12.5GiB"


def test_creates_file_with_stamped_version(tmp_path):
    path = tmp_path / "search_results.json"
    key = write_interchange_block(path, RESULTS)
    data = json.loads(path.read_text())
    assert key == "BUDGET-12.5GiB"
    assert data["tier_scheme_version"] == CURRENT_TIER_SCHEME_VERSION
    block = data["tiered"][key]
    assert block["config"] == {"U": "MXFP4_MOE", "Q": "Q5_K"}
    assert block["tensor_config"]["blk.0.attn_q.weight"] == "Q5_K"
    assert block["predicted_bytes"] == 13_000_000_000
    assert block["actual_bytes"] == 13_100_000_000
    assert block["ppl"] == 6.11


def test_merge_preserves_existing_v1_tiers_byte_identical(tmp_path):
    path = tmp_path / "search_results.json"
    v1 = {"tier_scheme_version": 2,
          "tiered": {"Q4": {"config": {"U": "MXFP4_MOE"}, "size_gb": 14.2}}}
    path.write_text(json.dumps(v1))
    write_interchange_block(path, RESULTS)
    data = json.loads(path.read_text())
    assert data["tiered"]["Q4"] == v1["tiered"]["Q4"]     # untouched
    assert "BUDGET-12.5GiB" in data["tiered"]


def test_merge_into_legacy_file_does_not_relabel_it(tmp_path):
    """A pre-version-stamp v1 file must NOT gain a version=2 stamp — that
    would falsely relabel its old wide-band tiers as current-semantics."""
    path = tmp_path / "search_results.json"
    path.write_text(json.dumps({"tiered": {"Q5": {"config": {"U": "Q6_K"}}}}))
    write_interchange_block(path, RESULTS)
    data = json.loads(path.read_text())
    assert "tier_scheme_version" not in data
    assert data["tiered"]["Q5"]["config"] == {"U": "Q6_K"}


def test_rerun_replaces_own_key_only(tmp_path):
    path = tmp_path / "search_results.json"
    write_interchange_block(path, RESULTS)
    updated = dict(RESULTS, baseline_ppl=6.02)
    write_interchange_block(path, updated)
    data = json.loads(path.read_text())
    assert len(data["tiered"]) == 1
    assert data["tiered"]["BUDGET-12.5GiB"]["baseline_ppl"] == 6.02


def test_missing_anchor_bytes_reads_none(tmp_path):
    path = tmp_path / "search_results.json"
    write_interchange_block(path, dict(RESULTS, anchors=[]))
    data = json.loads(path.read_text())
    assert data["tiered"]["BUDGET-12.5GiB"]["actual_bytes"] is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /server/programming/MagicQuant && .venv/bin/python -m pytest tests/test_v2_interchange.py -q` (use the repo's test invocation; plain `python -m pytest` if no venv)
Expected: FAIL — `ModuleNotFoundError: No module named 'magicquant.v2.interchange'`

- [ ] **Step 3: Implement `magicquant/v2/interchange.py`**

```python
"""Bridge a v2 budget build into the v1 ``search_results.json`` interchange.

v1 consumers (QAT's ``load_hybrid_config``, Foundry's ROCmFPX mq-hybrid mode,
the publish stage) read ``tiered[<key>].config`` as ``{group: scheme}``. A v2
budget build is per-tensor; this module writes BOTH the per-group projection
(``config``, reusing v2's own ``group_summary``) and the exact per-tensor map
(``tensor_config``) under a ``BUDGET-<N>GiB`` pseudo-tier key.

MERGE-ONLY: an existing file's other tiers are never touched, and a legacy
(pre-version-stamp) file is never given a version stamp — that would falsely
relabel its old wide-band tier names as current-semantics.
"""
from __future__ import annotations

import json
from pathlib import Path

from magicquant.quant.tiers import CURRENT_TIER_SCHEME_VERSION


def budget_tier_key(budget_gib: float) -> str:
    """Canonical pseudo-tier key for a budget build. One format, one place."""
    return f"BUDGET-{budget_gib:g}GiB"


def write_interchange_block(search_results_path: Path | str, results: dict) -> str:
    """Merge this budget run's block into ``search_results.json``.

    Returns the tier key written. Never raises for a missing file (creates
    it); a corrupt existing file is replaced (consistent with the rest of the
    pipeline's treat-corrupt-as-absent policy) after printing a warning.
    """
    path = Path(search_results_path)
    key = budget_tier_key(results["budget_gb"])
    alloc = results["allocation"]
    anchors = results.get("anchors") or []
    block = {
        "config": results["group_summary"],
        "tensor_config": alloc["assignment"],
        "tensor_actual_types": alloc["actual_types"],
        "algo": "v2-budget",
        "budget_bytes": alloc["budget_bytes"],
        "predicted_bytes": alloc["total_bytes"],
        "actual_bytes": anchors[0].get("actual_bytes") if anchors else None,
        "ppl": anchors[0].get("ppl") if anchors else None,
        "baseline_ppl": results["baseline_ppl"],
    }

    data: dict = {}
    if path.exists():
        try:
            data = json.loads(path.read_text())
        except (ValueError, OSError):
            print(f"Warning: {path} unreadable; rewriting with budget block only",
                  flush=True)
            data = {}
    fresh = not data
    data.setdefault("tiered", {})[key] = block
    if fresh:
        data["tier_scheme_version"] = CURRENT_TIER_SCHEME_VERSION

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)
    return key
```

- [ ] **Step 4: Wire the call + exports**

In `magicquant/v2/search.py`, immediately after `_atomic_write_json(out_dir / "v2_results.json", results)` (line ~394), add:

```python
    from magicquant.v2.interchange import write_interchange_block
    write_interchange_block(out_dir / "search_results.json", results)
```

In `magicquant/v2/__init__.py`, add `budget_tier_key` and `write_interchange_block` to the imports/`__all__` (match the file's existing export style; `V2Config` and `run_budget_search` are already exported there — verify `BudgetInfeasibleError` is too, and add it if not: Task 3 imports it from `magicquant.v2`).

- [ ] **Step 5: Run tests to verify they pass, then the full MagicQuant suite**

Run: `cd /server/programming/MagicQuant && python -m pytest tests/test_v2_interchange.py -q` → all PASS
Run: `python -m pytest tests/ -q -m "not slow and not gpu"` → no new failures (v1 fixture untouched)

- [ ] **Step 6: Commit (MagicQuant repo)**

```bash
cd /server/programming/MagicQuant
git add magicquant/v2/interchange.py magicquant/v2/search.py magicquant/v2/__init__.py tests/test_v2_interchange.py
git commit -m "feat(v2): merge budget builds into search_results.json interchange"
```

---

### Task 2: Foundry config threading (`budget_gib` flag → JSON)

**Dispatch:** INDEPENDENT

**Files:**
- Modify: `/server/programming/Foundry/core/pipeline.py` (MagicQuantConfig ~line 98; argparse block ~1618; `stage_magicquant`'s `svc.build_script(...)` call ~1207)
- Modify: `/server/programming/Foundry/core/services.py` (`MagicQuantService.build_config` ~line 315)
- Test: `/server/programming/Foundry/tests/test_magicquant_budget_config.py`

**Interfaces:**
- Produces: `MagicQuantConfig.budget_gib: Optional[float] = None`; CLI `--magicquant-budget-gib` (float, default None); `build_config(..., budget_gib=None)` → JSON key `"budget_gib"`; `build_config` raises `ValueError` when `budget_gib is not None and measured` (message contains both flag names). Tasks 3 and 7 consume the JSON key name `budget_gib`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_magicquant_budget_config.py
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
```

NOTE for implementer: if `core/pipeline.py` has no importable `build_arg_parser` (the parser may be built inline in `main()`), factor the `argparse.ArgumentParser` construction into a module-level `build_arg_parser()` used by `main()` — a mechanical, behavior-preserving extraction — rather than skipping the CLI test. Adjust the required `--model`/positional args in the test to whatever the parser actually requires (read the parser first).

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /server/programming/Foundry && .venv/bin/python -m pytest tests/test_magicquant_budget_config.py -q`
Expected: FAIL — `AttributeError: budget_gib` / `TypeError: unexpected keyword argument 'budget_gib'`

- [ ] **Step 3: Implement**

In `core/pipeline.py` `MagicQuantConfig` (after `allow_dequant_source`):

```python
    # Size-target mode: run MagicQuant v2's budget-constrained search instead
    # of the v1 tier ladder. Units are GiB (v2 computes budget_gb * 1024**3).
    # Mutually exclusive with `measured` -- v2 verifies with real perplexity
    # itself, so "measured" would misrepresent what ran (refused in
    # services.build_config, the single choke point for CLI and UI).
    budget_gib: Optional[float] = None
```

In the argparse block (next to `--magicquant-measured`):

```python
    parser.add_argument("--magicquant-budget-gib", type=float, default=None,
                        help="Size-target mode: run MagicQuant v2's budget search "
                             "for the best mix under this many GiB (instead of the "
                             "Q4/Q5/Q6 tier ladder). Mutually exclusive with "
                             "--magicquant-measured.")
```

Wire arg → config where the other `magicquant_*` args populate `MagicQuantConfig` (grep `magicquant_measured` in pipeline.py for the construction site), and add `budget_gib=mc.budget_gib` to the `svc.build_script(...)` call in `stage_magicquant`.

**Also (reviewer-caught, one line, load-bearing):** add `"budget_gib": mc.budget_gib` to the magicquant stage's completion-marker `config_hash({...})` dict (`core/pipeline.py:1180-1197`, the dict that enumerates every other knob). Without it, an output dir holding a completed v1 tier run reports "MagicQuant already complete — skipping" when the user adds `--magicquant-budget-gib`, and the budget search never runs.

In `core/services.py` `build_config`: add kwarg `budget_gib: Optional[float] = None`, and at the top of the function body:

```python
        if budget_gib is not None and measured:
            raise ValueError(
                "--magicquant-budget-gib and --magicquant-measured are mutually "
                "exclusive: the v2 budget search verifies with real perplexity "
                "itself, so 'measured' would misrepresent what ran."
            )
```

and `"budget_gib": budget_gib,` in the returned dict. `build_script` passes `**kwargs` through to `build_config` (verify — if it names kwargs explicitly, add `budget_gib` there too).

- [ ] **Step 4: Run tests to verify they pass, then the offline suite**

Run: `python -m pytest tests/test_magicquant_budget_config.py -q` → PASS
Run: `python -m pytest tests/ --ignore=tests/test_training_integration.py -m "not slow and not gpu" -q` → no new failures

- [ ] **Step 5: Commit (Foundry repo)**

```bash
cd /server/programming/Foundry
git add core/pipeline.py core/services.py tests/test_magicquant_budget_config.py
git commit -m "feat(magicquant): thread budget_gib size-target option through CLI/config/service"
```

---

### Task 3: Entry-shim budget branch

**Dispatch:** DEPENDS-ON Task 1, Task 2

**Files:**
- Modify: `/server/programming/Foundry/core/_magicquant_entry.py`
- Test: `/server/programming/Foundry/tests/test_magicquant_entry_budget.py`

**Interfaces:**
- Consumes: JSON key `budget_gib` (Task 2); `magicquant.v2.V2Config` (fields verified: `source_model_path`, `output_dir`, `budget_gb`, `llamacpp_path`, `use_imatrix`, `imatrix_corpus`, `measurement_chunks`, `enable_rocmfpx`, `model_name`), `run_budget_search` (returns dict with `final_model`), `BudgetInfeasibleError` (attrs `budget_bytes`, `min_bytes`), `budget_tier_key` (Task 1).
- Produces: `_run_budget(cfg: dict, source: str, llamacpp: str | None) -> str` in `core/_magicquant_entry.py` — runs the v2 search against the RESOLVED source and llama.cpp path, renames the output GGUF to `f"{cfg['model_name']}-{budget_tier_key(b)}.gguf"` inside `<out_abs_str>/magicquant/`, returns the renamed path.

  **Dispatch placement (reviewer-caught, critical):** `run()` dispatches when `cfg.get("budget_gib")` is set — but the dispatch goes **just before `orch = MagicQuantOrchestrator(...)` (~line 435), NOT before line 379**. Lines 381–433 do three load-bearing things the budget path needs identically: `resolve_source()` (reap > heretic > merged > bf16 priority; `mq_source_override` is `""` for UI runs with export enabled and a raw *safetensors dir* in the CLI path), the mandatory safetensors→BF16-GGUF conversion (`should_convert_source_to_gguf` — the guard added after the qwen3_5 silent-garbage-quant incident), and mmproj export. `_run_budget` receives the post-resolution `source` and the resolved `llamacpp` variable (line ~345), never the raw cfg hints. The returned file then routes through the SAME PPL-smoke-gate tail as v1 (`valid = [renamed_path]`) before `PIPELINE_STAGE_COMPLETE=magicquant`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_magicquant_entry_budget.py
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_magicquant_entry_budget.py -q`
Expected: FAIL — `AttributeError: module 'core._magicquant_entry' has no attribute '_run_budget'`

- [ ] **Step 3: Implement `_run_budget` + dispatch in `run()`**

Add to `core/_magicquant_entry.py` (module level, near `run()`):

```python
# v1 config keys that have no meaning under the v2 budget search. Listed
# explicitly so a user who set one sees it acknowledged, not swallowed.
_BUDGET_IGNORED_KEYS = (
    "generations", "population_size", "target_base_quant", "measured",
    "measurement_rounds", "iq_schemes", "seed", "enable_kl", "kl_weight",
    "enable_speed_bench", "stream_aware", "head_aggressive", "speed_aware",
    "speed_metric", "speed_weight", "use_bytes_tps", "calibration_source",
    "write_calibration", "tiers_json", "verify",
)

_BUDGET_KEY_DEFAULTS = {
    "generations": 50, "population_size": 100, "measurement_rounds": 3,
    "target_base_quant": "MXFP4_MOE", "kl_weight": 0.1,
    "speed_metric": "bytes", "tiers_json": '["Q4","Q5","Q6"]',
}


def _run_budget(cfg: dict, source: str, llamacpp) -> str:
    """Size-target mode: MagicQuant v2 budget search instead of the tier
    ladder. `source` and `llamacpp` are the RESOLVED values from run()'s
    shared preamble (source priority + safetensors->BF16 conversion + mmproj
    export all apply to budget runs identically). Returns the renamed output
    GGUF path (publish naming)."""
    from magicquant.v2 import (BudgetInfeasibleError, V2Config,
                               budget_tier_key, run_budget_search)

    budget_gib = float(cfg["budget_gib"])
    for key in _BUDGET_IGNORED_KEYS:
        val = cfg.get(key)
        default = _BUDGET_KEY_DEFAULTS.get(key)
        if val not in (None, False, "", default):
            print(f"  note: {key}={val!r} is ignored under "
                  f"--magicquant-budget-gib (v2 search)", flush=True)

    out_dir = Path(cfg["out_abs_str"]) / "magicquant"
    v2cfg = V2Config(
        source_model_path=source,
        output_dir=str(out_dir),
        budget_gb=budget_gib,
        llamacpp_path=llamacpp or None,
        use_imatrix=cfg.get("use_imatrix", True),
        imatrix_corpus=cfg.get("imatrix_corpus"),
        measurement_chunks=cfg.get("measurement_chunks"),
        enable_rocmfpx=cfg.get("rocmfpx_schemes", False),
        model_name=cfg["model_name"],
    )
    try:
        results = run_budget_search(v2cfg)
    except BudgetInfeasibleError as e:
        print(
            f"Error: budget {e.budget_bytes / 1024**3:.2f} GiB is below the "
            f"floor; minimum achievable with the enabled schemes is "
            f"{e.min_bytes / 1024**3:.2f} GiB. Raise --magicquant-budget-gib.",
            flush=True,
        )
        sys.exit(1)

    if not results.get("final_model"):
        print(
            "Error: v2 budget search produced no final model (the budget "
            "anchor's build or measurement failed; a neighbor anchor may have "
            f"succeeded). See {out_dir / 'v2_results.json'} -> 'failures'.",
            flush=True,
        )
        sys.exit(1)

    final = Path(results["final_model"])
    target = final.parent / f"{cfg['model_name']}-{budget_tier_key(budget_gib)}.gguf"
    final.rename(target)
    print(f"  budget build: {target.name} "
          f"({target.stat().st_size / 1024**3:.2f} GiB)", flush=True)
    return str(target)
```

In `run()`, add the dispatch **just before `orch = MagicQuantOrchestrator(...)` (~line 435)** — after `resolve_source()`, the safetensors→BF16 conversion, and mmproj export have all run (they apply to budget runs identically; see the Interfaces block) — and route the result through the existing smoke-gate tail:

```python
    if cfg.get("budget_gib"):
        valid = [_run_budget(cfg, source, llamacpp)]
    else:
        ... existing v1 body (orchestrator + search + generate_tiered_models)
        ... producing `valid`, unchanged ...
    # existing smoke-gate tail (lines ~541-559) runs unchanged for both paths
```

(`source` and `llamacpp` are whatever local names run()'s preamble actually binds the resolved source path and llama.cpp path to — read the function and use its real variable names.)

Do NOT change any v1 behavior — the v1 body moves under `else:` unmodified (indentation-only change) or the budget branch early-computes and falls through; pick whichever produces the smaller diff, but the smoke-gate tail MUST be shared, not duplicated.

- [ ] **Step 4: Run tests to verify they pass, then the offline suite**

Run: `python -m pytest tests/test_magicquant_entry_budget.py -q` → PASS
Run: `python -m pytest tests/ --ignore=tests/test_training_integration.py -m "not slow and not gpu" -q` → no new failures

- [ ] **Step 5: Commit (Foundry repo)**

```bash
git add core/_magicquant_entry.py tests/test_magicquant_entry_budget.py
git commit -m "feat(magicquant): entry-shim budget branch calling v2 run_budget_search"
```

---

### Task 4: Publish criteria — `BUDGET_TOLERANCE`, `decide_budget_build`, `decide_rocmfpx_budget`

**Dispatch:** INDEPENDENT

**Files:**
- Modify: `/server/programming/Foundry/core/publish_criteria.py`
- Test: `/server/programming/Foundry/tests/test_publish_criteria.py` (append)

**Interfaces:**
- Produces (consumed by Tasks 5, 6):
  - `BUDGET_TOLERANCE = 0.02`
  - `BUDGET_FILE_RE = re.compile(r"-BUDGET-([\d.]+)GiB\.gguf$")`
  - `decide_budget_build(*, name: str, actual_gib: float, budget_gib: float) -> dict` — returns `{"ship": bool, "rule": "budget", "reason": str, "overshoot_frac": float}`; refuses (`ship=False`) iff `actual_gib > budget_gib * (1 + BUDGET_TOLERANCE)`; the refusal reason must name uncounted bytes as the likely cause (diagnosis, not mystery).
  - `decide_rocmfpx_budget(*, fx_tg: float | None, mq_tg: float | None) -> dict` — SPEED rule for a ROCmFPX budget build against its MagicQuant budget peer: ship iff `fx_tg >= mq_tg * SPEED_MARGIN` (reuse the existing `SPEED_MARGIN = 1.15`); missing measurements → `{"ship": False, "rule": "speed", "reason": "<names the missing measurement>"}` (a speed-justified family never ships unmeasured).

- [ ] **Step 1: Write the failing tests** (append to `tests/test_publish_criteria.py`, matching its existing style — see `test_dominance_fires_when_margin_exceeds_floor` for the house pattern of pinning `rule` and `reason` content)

```python
# --- budget builds -----------------------------------------------------------
from core.publish_criteria import (BUDGET_FILE_RE, BUDGET_TOLERANCE,
                                   decide_budget_build, decide_rocmfpx_budget)


def test_budget_tolerance_value_pinned():
    assert BUDGET_TOLERANCE == 0.02


def test_budget_file_regex_extracts_the_number():
    m = BUDGET_FILE_RE.search("Model-BUDGET-12.5GiB.gguf")
    assert m and m.group(1) == "12.5"
    assert not BUDGET_FILE_RE.search("Model-Q4_K_M.gguf")


def test_budget_build_ships_under_budget():
    d = decide_budget_build(name="M-BUDGET-100GiB.gguf",
                            actual_gib=99.0, budget_gib=100.0)
    assert d["ship"] is True and d["rule"] == "budget"


def test_budget_build_ships_inside_tolerance():
    d = decide_budget_build(name="M-BUDGET-100GiB.gguf",
                            actual_gib=101.9, budget_gib=100.0)
    assert d["ship"] is True


def test_budget_build_refused_beyond_tolerance_names_uncounted_bytes():
    d = decide_budget_build(name="M-BUDGET-100GiB.gguf",
                            actual_gib=102.1, budget_gib=100.0)
    assert d["ship"] is False
    assert "uncounted" in d["reason"]
    assert d["overshoot_frac"] == pytest.approx(0.021, abs=1e-4)


def test_rocmfpx_budget_speed_rule_ships_only_when_faster():
    assert decide_rocmfpx_budget(fx_tg=12.0, mq_tg=10.0)["ship"] is True
    slow = decide_rocmfpx_budget(fx_tg=10.0, mq_tg=10.0)
    assert slow["ship"] is False and slow["rule"] == "speed"
    assert "tok/s" in slow["reason"] or "faster" in slow["reason"]


def test_rocmfpx_budget_missing_measurement_refuses_and_says_which():
    d = decide_rocmfpx_budget(fx_tg=None, mq_tg=10.0)
    assert d["ship"] is False and "measure" in d["reason"].lower()
```

- [ ] **Step 2: Run to verify failure** — `python -m pytest tests/test_publish_criteria.py -q` → ImportError on the new names.

- [ ] **Step 3: Implement** in `core/publish_criteria.py` (append; import `re` at top if absent):

```python
# --- Budget (size-target) builds --------------------------------------------
# A budget build claims a SIZE, not a tier band. BAND and DOMINANCE do not
# apply (no band claim, no siblings); the one rule is the tolerance below.
# v2's budget bounds allocatable-tensor bytes, not file size -- metadata,
# alignment padding, and passthrough tensors with unknown sizes are uncounted,
# and ROCmFPX fork-type rounding can overshoot. 2% covers both; one constant
# so build-time and publish-time guards can never disagree.
BUDGET_TOLERANCE = 0.02

BUDGET_FILE_RE = re.compile(r"-BUDGET-([\d.]+)GiB\.gguf$")


def decide_budget_build(*, name: str, actual_gib: float, budget_gib: float) -> dict:
    """SHIP/REFUSE for a MagicQuant budget build (publish-time size guard)."""
    limit = budget_gib * (1 + BUDGET_TOLERANCE)
    overshoot = actual_gib / budget_gib - 1.0
    if actual_gib <= limit:
        return {"ship": True, "rule": "budget", "overshoot_frac": overshoot,
                "reason": (f"{name}: {actual_gib:.2f} GiB fits the requested "
                           f"{budget_gib:g} GiB budget "
                           f"(tolerance {BUDGET_TOLERANCE:.0%}).")}
    return {"ship": False, "rule": "budget", "overshoot_frac": overshoot,
            "reason": (f"{name}: {actual_gib:.2f} GiB exceeds the requested "
                       f"{budget_gib:g} GiB budget by {overshoot:.1%} "
                       f"(> {BUDGET_TOLERANCE:.0%} tolerance) -- likely "
                       f"uncounted bytes (GGUF metadata, alignment padding, "
                       f"passthrough tensors) or fork-type rounding.")}


def decide_rocmfpx_budget(*, fx_tg, mq_tg) -> dict:
    """SPEED rule for a ROCmFPX budget build vs its MagicQuant budget peer.

    Same principle as decide_rocmfpx_tiers: ROCmFPX trades quality for
    throughput, so it ships only when measurably faster. Unmeasured never
    ships -- speed is the family's entire justification.
    """
    if fx_tg is None or mq_tg is None:
        which = "ROCmFPX build" if fx_tg is None else "MagicQuant peer"
        return {"ship": False, "rule": "speed",
                "reason": f"no throughput measurement for the {which}; "
                          f"a speed-justified family never ships unmeasured."}
    if fx_tg >= mq_tg * SPEED_MARGIN:
        return {"ship": True, "rule": "speed",
                "reason": (f"{fx_tg:.2f} tok/s vs MagicQuant peer "
                           f"{mq_tg:.2f} tok/s (>= {SPEED_MARGIN:g}x).")}
    return {"ship": False, "rule": "speed",
            "reason": (f"{fx_tg:.2f} tok/s is not >= {SPEED_MARGIN:g}x the "
                       f"MagicQuant peer's {mq_tg:.2f} tok/s -- not faster, "
                       f"so the quality trade buys nothing.")}
```

- [ ] **Step 4: Run** `python -m pytest tests/test_publish_criteria.py -q` → PASS (old tests included).

- [ ] **Step 5: Commit (Foundry repo)**

```bash
git add core/publish_criteria.py tests/test_publish_criteria.py
git commit -m "feat(publish): budget-build criteria (tolerance guard + speed peer rule)"
```

---

### Task 5: Card rendering + repo audit for budget builds

**Dispatch:** DEPENDS-ON Task 4

**Files:**
- Modify: `/server/programming/Foundry/core/hf_upload.py`
- Test: `/server/programming/Foundry/tests/test_hf_upload_budget_card.py` (new)

**Interfaces:**
- Consumes: `BUDGET_FILE_RE`, `BUDGET_TOLERANCE` from `core.publish_criteria` (Task 4); interchange block fields (Task 1) when `search_results.json` is available.
- Produces: budget-aware card generation — a file matching `BUDGET_FILE_RE` in the upload set gets a "Size-target build" card section with: requested budget (from the filename), achieved size (from file bytes / repo metadata `known_sizes`), and measured perplexity + baseline (from the interchange block's `baseline_ppl` and the v2 anchors data via `search_results.json`, when present — omit the PPL row when absent, never fabricate). `audit_card_against_repo` must treat a budget file as disclosed when the card contains its exact filename (same rule as tier files — verify this already holds by reading `audit_card_against_repo` before writing code; if it keys on tier-name patterns, extend it).

- [ ] **Step 1: Read first, then write the failing tests.** Read `core/hf_upload.py`'s `generate_model_card` signature and the existing per-family table construction, plus `audit_card_against_repo` (~line 1589), and write tests in their established style (see `tests/test_card_repo_consistency.py` for fixtures). Required test cases:

```python
# tests/test_hf_upload_budget_card.py — exact fixture shapes to be adapted to
# generate_model_card's real signature after reading it. The BEHAVIORAL
# contract each test pins (do not weaken):

def test_budget_file_gets_size_target_section():
    """Card for a repo containing M-BUDGET-100GiB.gguf includes: the literal
    filename, the requested '100 GiB' figure, and the achieved size."""

def test_budget_ppl_row_from_interchange_block(tmp_path):
    """With <out>/magicquant/search_results.json carrying the Task-1 block
    (fields `ppl` and `baseline_ppl` — fixture shape pinned verbatim in
    Task 1's Interfaces block; build it locally, no cross-task import), the
    section shows measured PPL and the delta vs baseline. The interchange
    block is the ONLY PPL source — never v2_results.json/frontier.json,
    which cleanup deletes."""

def test_budget_ppl_row_omitted_when_no_records(tmp_path):
    """No search_results.json -> the section renders WITHOUT a PPL row.
    Absent data is omitted, never invented."""

def test_audit_flags_undisclosed_budget_file():
    """audit_card_against_repo(card_without_mention, repo_files_with_budget_gguf)
    reports the budget file as undisclosed."""

def test_audit_passes_when_disclosed():
    """Same repo, card WITH the section -> no findings for that file."""

def test_budget_refusal_rendered_as_size_overshoot():
    """A _refusals.json record with rule == "budget" (Task 6's schema:
    requested_budget_gib + predicted_gib present, bands empty) renders in the
    card's refusals section phrased as size overshoot vs the requested
    budget — never the band prose ('would land in band X') used for tier
    refusals. Spec: 'Refusal record extension' — this rendering lives in
    core/hf_upload.py (tracked), NOT in output/_publish_tiers.py."""
```

- [ ] **Step 2: Run to verify failure** — new test file fails on missing section logic.

- [ ] **Step 3: Implement** in `core/hf_upload.py`: detect budget files in the upload set via `BUDGET_FILE_RE`; render the section into the card next to the existing per-family tables; source PPL from the interchange block ONLY (`tiered[key]["ppl"]` and `["baseline_ppl"]`, locating `search_results.json` via `_find_measured_losses`' existing directory-walking pattern) — omit the row when absent. Extend the existing refusals rendering to the `rule: "budget"` record shape (size-overshoot phrasing per the test above). `audit_card_against_repo`'s filename-mention rule already covers budget files (verified by plan review) — add the test, expect no code change there.

- [ ] **Step 4: Run** the new file + `tests/test_card_repo_consistency.py` + full offline suite → PASS / no new failures.

- [ ] **Step 5: Commit (Foundry repo)**

```bash
git add core/hf_upload.py tests/test_hf_upload_budget_card.py
git commit -m "feat(upload): size-target card section + audit coverage for budget builds"
```

---

### Task 6: ROCmFPX budget mode (`mq-budget`)

**Dispatch:** DEPENDS-ON Task 1, Task 4

**Files:**
- Modify: `/server/programming/Foundry/core/_rocmfpx_entry.py`
- Test: `/server/programming/Foundry/tests/test_rocmfpx_budget_mode.py` (new)

**Interfaces:**
- Consumes: interchange block (`tensor_config`, `budget_bytes`) under `BUDGET-*` keys (Task 1); `BUDGET_TOLERANCE` (Task 4); existing helpers `translate_scheme`, `_record_refusal`, `parse_mq_spec`, `_load_mq_tier_config` (verified signatures above).
- Produces:
  - `parse_mq_spec` recognizes `mq-budget` → returns `"BUDGET"`, and `mq-budget=BUDGET-100GiB` → returns `"BUDGET-100GIB"`-normalized… **No.** Keep it simple and case-exact: extend `parse_mq_spec` so `mq-budget` → `"BUDGET"` sentinel and `mq-budget=<KEY>` → the literal `<KEY>` (no case mangling — budget keys are case-sensitive as written by `budget_tier_key`). Document in the docstring.
  - `_resolve_budget_key(out_dir: Path, requested: str) -> str` — `"BUDGET"` sentinel + exactly one `BUDGET-*` key in `tiered` → that key; zero → `FileNotFoundError`-style loud error listing available tiers; multiple → error listing the `BUDGET-*` keys and instructing `mq-budget=<KEY>`; explicit key present → it; explicit key absent → error listing candidates.
  - `build_tensor_type_lines_per_tensor(tensor_config: dict) -> list[str]` — for each `{tensor_name: scheme}`: refuse (raise `ValueError` naming the tensor) if the name contains whitespace or `=`; emit `f"^{re.escape(name)}$={translate_scheme(scheme)}"`.
  - `predict_rendered_budget(tensor_config: dict, bf16_gguf: str) -> tuple[float, float]` — per-tensor analog of `predict_rendered_tier`: sum over the bf16 GGUF's tensors of `n_elems * bpw(translated_type)` for tensors in `tensor_config`, `n_elems * 32.0` bits for tensors not in it (untouched norms etc. — same convention as `predict_rendered_tier`); returns `(predicted_gib, baseline_gib)` — the guard's `_record_refusal` call needs `baseline_gib` too, same as `predict_rendered_tier` computes it (`total * 16.0 / 8.0 / 2**30`). Reuse `predict_rendered_tier`'s `_bpw` logic (factor it out to module level as `_type_bpw(ggml_type) -> float` so both use one copy).
  - Fork-binary validation: `validate_types_supported`/`pick_base_type` currently derive required types from the per-group `config`; on the budget path derive them from the UNION of `config` values and `tensor_config` values — a scheme appearing only in `tensor_config` must not reach the fork unvalidated.
  - Build-time guard in the `mq-budget` build path: `predicted_gib > budget_gib * (1 + BUDGET_TOLERANCE)` → `_record_refusal(..., tier=key, family="rocmfpx", reason=..., predicted_gib=..., baseline_gib=..., predicted_band="", claimed_band="", rule="budget", requested_budget_gib=budget_gib)` — extend `_record_refusal` with optional kwargs `rule: str = "band"` and `requested_budget_gib: float | None = None`, added to the record dict (additive; existing callers unchanged — `tests/test_rocmfpx_refusal_record.py` must still pass).
  - Output naming: `f"{model_name}-ROCMFPX-MQ-{key}.gguf"` (key = `BUDGET-<N>GiB`), which the existing mq naming line already produces given `tier=key` — verify, don't duplicate.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_rocmfpx_budget_mode.py
"""mq-budget renders a v2 per-tensor allocation in ROCmFPX types. The
per-tensor path must emit anchored, escaped, whitespace-free lines (the fork
parses `<regex>=<TYPE>` tokens split on whitespace and matches with unanchored
regex_search); the size guard shares BUDGET_TOLERANCE with publish so
build-time and publish-time can never disagree."""
import json
import re

import pytest

from core._rocmfpx_entry import (build_tensor_type_lines_per_tensor,
                                 parse_mq_spec, _resolve_budget_key)
from core.publish_criteria import BUDGET_TOLERANCE


def test_parse_mq_spec_budget_sentinel_and_explicit_key():
    assert parse_mq_spec("mq-budget") == "BUDGET"
    assert parse_mq_spec("mq-budget=BUDGET-12.5GiB") == "BUDGET-12.5GiB"
    assert parse_mq_spec("mq-q4") == "Q4"          # unchanged
    assert parse_mq_spec("rocmfp4-agent") is None  # unchanged


def _write_results(tmp_path, keys):
    d = tmp_path / "magicquant"
    d.mkdir(parents=True, exist_ok=True)
    (d / "search_results.json").write_text(json.dumps(
        {"tier_scheme_version": 2,
         "tiered": {k: {"config": {"U": "MXFP4_MOE"},
                        "tensor_config": {"blk.0.ffn_up.weight": "MXFP4_MOE"}}
                    for k in keys}}))


def test_resolve_single_budget_key(tmp_path):
    _write_results(tmp_path, ["Q4", "BUDGET-12.5GiB"])
    assert _resolve_budget_key(tmp_path, "BUDGET") == "BUDGET-12.5GiB"


def test_resolve_zero_keys_is_loud(tmp_path):
    _write_results(tmp_path, ["Q4"])
    with pytest.raises(Exception) as e:
        _resolve_budget_key(tmp_path, "BUDGET")
    assert "Q4" in str(e.value)          # lists what IS available


def test_resolve_multiple_requires_explicit(tmp_path):
    _write_results(tmp_path, ["BUDGET-10GiB", "BUDGET-20GiB"])
    with pytest.raises(Exception) as e:
        _resolve_budget_key(tmp_path, "BUDGET")
    assert "BUDGET-10GiB" in str(e.value) and "mq-budget=" in str(e.value)
    assert _resolve_budget_key(tmp_path, "BUDGET-20GiB") == "BUDGET-20GiB"


def test_per_tensor_lines_are_anchored_and_escaped():
    lines = build_tensor_type_lines_per_tensor(
        {"blk.0.ffn_up.weight": "MXFP4_MOE"})
    assert len(lines) == 1
    pattern, _, ggml = lines[0].partition("=")
    assert pattern.startswith("^") and pattern.endswith("$")
    assert re.search(pattern, "blk.0.ffn_up.weight")
    assert not re.search(pattern, "blk.10.ffn_up.weight")  # '.' escaped
    assert " " not in lines[0]


def test_per_tensor_lines_refuse_hostile_names():
    with pytest.raises(ValueError):
        build_tensor_type_lines_per_tensor({"bad name": "Q5_K"})
    with pytest.raises(ValueError):
        build_tensor_type_lines_per_tensor({"bad=name": "Q5_K"})
```

Plus a guard test using a monkeypatched `predict_rendered_budget` asserting the refusal record carries `rule == "budget"` and `requested_budget_gib` (adapt to `_record_refusal`'s file I/O by pointing it at `tmp_path` and reading `_refusals.json` back — mirror the existing style in `tests/test_rocmfpx_refusal_record.py`), and a regression run of that existing file proving old callers still write valid records (`rule` defaults to `"band"`).

- [ ] **Step 2: Run to verify failure** — ImportError on the new helpers.

- [ ] **Step 3: Implement** per the Interfaces block above. `parse_mq_spec`: the bare `mq-budget` form already yields `"BUDGET"` through the existing generic path (verified) — only the `mq-budget=<KEY>` form needs new code, and the key MUST be sliced from the ORIGINAL `spec` argument, not the lowercased local `s` (line 99 lowercases; budget keys are case-sensitive as written by `budget_tier_key`). In the `run()` dispatch loop, a `"BUDGET"`/`BUDGET-*` tier routes to `_quantize_mq_hybrid` with the resolved key; inside `_quantize_mq_hybrid` (or a thin `_quantize_mq_budget` wrapper if cleaner), when the tier key starts with `BUDGET-`: load the block, prefer `tensor_config` → `build_tensor_type_lines_per_tensor`, fall back to `config` → existing `build_tensor_type_lines`; run the size guard before invoking `llama-quantize`; budget from the block's `budget_bytes / 1024**3`.

- [ ] **Step 4: Run** new file + `tests/test_rocmfpx_entry.py` + `tests/test_rocmfpx_refusal_record.py` + `tests/test_rocmfpx_tier_band.py` + full offline suite → PASS / no new failures.

- [ ] **Step 5: Commit (Foundry repo)**

```bash
git add core/_rocmfpx_entry.py tests/test_rocmfpx_budget_mode.py
git commit -m "feat(rocmfpx): mq-budget preset renders v2 per-tensor allocations"
```

---

### Task 7: UI card field

**Dispatch:** DEPENDS-ON Task 2

**Files:**
- Modify: `/server/programming/Foundry/ui/app.py` (`MagicQuantCfg` ~242; `do_magicquant`'s `svc.build_script(...)` call ~1074-1105)
- Modify: `/server/programming/Foundry/ui/index.html` (MagicQuant card form ~1483 area; defaults object ~691)
- Test: `/server/programming/Foundry/tests/test_ui_magicquant_budget.py` (new)

**Interfaces:**
- Consumes: `build_script(budget_gib=...)` kwarg (Task 2).
- Produces: `MagicQuantCfg.budget_gib: Optional[float] = None`, passed through `do_magicquant` → `build_script`; an `<input type="number" data-key="magicquant.budget_gib">` following the exact pattern of the existing `magicquant.measurement_chunks` input (quoted in the house style at `ui/index.html:1483`); default `budget_gib: null` in the page defaults object.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_ui_magicquant_budget.py
"""The UI model must accept budget_gib and hand it to the service layer —
the measured+budget refusal then fires in build_config for UI runs too
(single choke point, tested in test_magicquant_budget_config.py)."""
import pytest

fastapi = pytest.importorskip("fastapi")

from ui.app import MagicQuantCfg


def test_cfg_accepts_budget_gib():
    assert MagicQuantCfg().budget_gib is None
    assert MagicQuantCfg(budget_gib=12.5).budget_gib == 12.5


def test_index_html_has_the_input_and_default():
    html = open("ui/index.html", encoding="utf-8").read()
    assert 'data-key="magicquant.budget_gib"' in html
    assert "budget_gib: null" in html
```

Plus: grep `do_magicquant`'s `build_script` call and add an assertion-style test only if the UI test suite already has a pattern for exercising `do_magicquant` without a subprocess (read `tests/` for an existing `do_magicquant` test; if none exists, the two tests above + Task 9's manual smoke cover it — do not build new UI-test infrastructure for one field).

- [ ] **Step 2: Run to verify failure** — `budget_gib` unknown on `MagicQuantCfg` (pydantic may silently ignore unknown kwargs depending on model config — the `is None` assertion still fails), HTML greps fail.

- [ ] **Step 3: Implement** — one field on `MagicQuantCfg` (comment matching neighbors: `# size-target mode: v2 budget search under this many GiB; None = tier ladder`), `budget_gib=cfg.budget_gib` in `do_magicquant`'s `build_script` call, and in `ui/index.html`:

```html
<div class="form-group">${L('Size Target (GiB)','Run MagicQuant v2\'s budget search: best quality mix under this size instead of the Q4/Q5/Q6 tier ladder. Mutually exclusive with Measured search. Blank = tier ladder.')}<input class="form-input" type="number" step="0.1" data-key="magicquant.budget_gib" value="${c.budget_gib ?? ''}" placeholder="(tier ladder)"></div>
```

and `budget_gib: null,` in the defaults object next to `measurement_chunks: null`.

**Also (reviewer-caught):** add `'mq-budget'` to the `rcFormats` list at `ui/index.html:696` (the array populating the ROCmFPX card's format tags — without it the spec's "new preset alongside mq-q4/5/6" ships CLI-only). Add to the Step-1 test: `assert "'mq-budget'" in html`.

- [ ] **Step 4: Run** the new test + offline suite → PASS.

- [ ] **Step 5: Commit (Foundry repo)**

```bash
git add ui/app.py ui/index.html tests/test_ui_magicquant_budget.py
git commit -m "feat(ui): size-target (budget GiB) field on the MagicQuant card"
```

---

### Task 8: QAT consumes the budget pseudo-tier (verification tests)

**Dispatch:** DEPENDS-ON Task 1

**Files:**
- Test: `/server/programming/Foundry/tests/test_qat_budget_tier.py` (new; expected to be test-only — QAT's `tier` is an unvalidated free string end-to-end, verified 2026-08-03)

**Interfaces:**
- Consumes: interchange block shape (Task 1); `magicquant.qat.config.load_hybrid_config`.

- [ ] **Step 1: Write the tests**

```python
# tests/test_qat_budget_tier.py
"""QAT consumes a budget build through the same load_hybrid_config path as a
tier build. This is the spec's 'QAT consumes v2' requirement: expected to
pass with ZERO code changes (tier is a free string end-to-end). If any of
these fail, that assumption was wrong — fix the plumbing, don't delete the
test."""
import json

import pytest

qat_config = pytest.importorskip("magicquant.qat.config")


def _results(tmp_path):
    p = tmp_path / "search_results.json"
    p.write_text(json.dumps({
        "tier_scheme_version": 2,
        "tiered": {"BUDGET-12.5GiB": {
            "config": {"U": "MXFP4_MOE", "Q": "Q5_K"},
            "tensor_config": {"blk.0.ffn_up.weight": "MXFP4_MOE"},
            "algo": "v2-budget",
        }}}))
    return p


def test_load_hybrid_config_resolves_budget_key(tmp_path):
    got = qat_config.load_hybrid_config(_results(tmp_path), "BUDGET-12.5GiB")
    assert got["U"] == "MXFP4"      # scheme -> ggml type name mapping applied
    assert "Q" in got


def test_missing_budget_key_error_lists_available(tmp_path):
    with pytest.raises(KeyError) as e:
        qat_config.load_hybrid_config(_results(tmp_path), "BUDGET-99GiB")
    assert "BUDGET-12.5GiB" in str(e.value)
```

NOTE: `MXFP4_MOE` → ggml name mapping — read `magicquant/qat/config.py::_to_ggml_type_name` and adjust the expected string (`"MXFP4"`) to what the registry actually returns for `MXFP4_MOE` before finalizing the assertion; the behavioral point is that scheme→ggml mapping is applied, not the specific string.

- [ ] **Step 2-4: Run** — these should pass immediately (that IS the verification). If a failure reveals plumbing that rejects non-`Q*` tier strings, fix that plumbing minimally in the failing layer and re-run.

- [ ] **Step 5: Commit (Foundry repo)**

```bash
git add tests/test_qat_budget_tier.py
git commit -m "test(qat): pin budget pseudo-tier consumption via load_hybrid_config"
```

---

### Task 9: Integration sweep, docs, CI, push, service restart

**Dispatch:** DEPENDS-ON Task 1, Task 2, Task 3, Task 4, Task 5, Task 6, Task 7, Task 8

**Files:**
- Modify: `/server/programming/Foundry/CLAUDE.md` (pipeline stage 6 paragraph: add two sentences on `--magicquant-budget-gib` + `mq-budget`), `/server/programming/Foundry/README.md` (feature mention, matching its existing MagicQuant section style), `/server/programming/Foundry/docs/rocmfpx.md` (the canonical ROCmFPX reference — document `mq-budget` next to the existing mq-* spec docs at lines ~51/~100), `/server/programming/MagicQuant/README.md` (one line: v2 budget builds now interoperate with v1 consumers via `search_results.json`)

- [ ] **Step 1: Full suites + lint, both repos**

Run: `cd /server/programming/MagicQuant && python -m pytest tests/ -q -m "not slow and not gpu" && make lint`
Run: `cd /server/programming/Foundry && .venv/bin/python -m pytest tests/ --ignore=tests/test_training_integration.py -m "not slow and not gpu" -q && make lint`
Expected: all green.

- [ ] **Step 2: CI-parity check (Foundry).** Foundry CI installs a minimal dep set — re-run the offline suite inside the clean CI-sim venv if it still exists from the 2026-08-03 CI fix (`ls /tmp/claude-1000/*/ci-venv 2>/dev/null || echo rebuild per .github/workflows/ci.yml`); otherwise rebuild it exactly from `.github/workflows/ci.yml`'s pip install lines with `PYTHONPATH=/server/programming/MagicQuant`. New tests must skip cleanly (importorskip) where heavy deps are absent, not error.

- [ ] **Step 3: Docs edits** described in Files above; keep each to a few sentences, matching surrounding prose (notably: tier names are size bands; a budget build claims a SIZE — keep that distinction crisp in CLAUDE.md).

- [ ] **Step 4: Commit docs; push both repos; watch CI to green**

```bash
# ORDER IS LOAD-BEARING: Foundry CI checks out lucasmcoleman/MagicQuant's
# DEFAULT BRANCH onto PYTHONPATH — pushing Foundry first turns Foundry CI red
# because the interchange module wouldn't exist there yet.
cd /server/programming/MagicQuant && git push origin master
cd /server/programming/Foundry && git add CLAUDE.md README.md docs/rocmfpx.md && git commit -m "docs: size-target quantization (budget builds)" && git push origin master
# then: gh run watch --repo <each> (or poll gh run list) until BOTH are green.
# A red run is NOT done — diagnose and fix before proceeding.
```

- [ ] **Step 5: Restart the UI service (standing ops rule after Foundry edits)**

Run: `sudo systemctl restart foundry-ui && systemctl is-active foundry-ui`
Expected: `active`. Then `curl -sf localhost:7865 >/dev/null && echo UI OK`.

---

## Capability / Roadmap

- **Per-tensor QAT** — *Value:* QAT currently trains against the per-group approximation of a budget build; per-tensor fidelity should recover more quality on aggressive budgets. *Approach:* extend `magicquant/qat/fake_quant.py` dispatch to accept a `{tensor_name: type}` map (the interchange block already carries `tensor_config`). *Feasibility:* the interchange data exists after Task 1; the fake-quant wrap layer keys on modules today, so it's a mapping change, not an architecture change — but only worth building if a real budget-QAT run measures the group approximation as lossy.
- **Budget frontier publishing** — *Value:* v2 computes a whole quality/size frontier per solve; publishing 2-3 frontier points as siblings would give users a ladder for free. *Approach:* v2's `keep_anchors=True` already writes neighbor GGUFs; add publish criteria treating anchor siblings as a disclosed set. *Feasibility:* mostly publish/card work; the search cost is already paid.
