# Decision: ROCmFPX Stage Failure Must Not Block Upload

**Date:** 2026-08-13
**Status:** **ACCEPTED 2026-08-14 by Lucas** — Option D with the disclosure
predicate, exactly as recommended below. Implementation is its own change with
its own verification (see "Acceptance criteria"); it is not folded into any
other work. The quarantine hazard named under "What this does and does not fix"
was filed first, as a precondition of this acceptance:
[Foundry #2](https://github.com/lucasmcoleman/Foundry/issues/2).
**Issue:** Foundry #7
**Scope:** `core/_rocmfpx_entry.py`, `core/services.py`, `core/pipeline.py`, `ui/app.py`
**Recommendation:** Option D (run-level `--allow-partial`), **gated on a disclosure predicate** — not a blanket "continue past any failure"

---

## The problem

An optional, experimental, research-fork stage aborted the run and took the
primary deliverable down with it.

A ROCmFPX `mq-q4` build failed, the stage exited non-zero, and both
orchestrators' generic `if not ok: break` stopped the pipeline **before**
`upload`. The MagicQuant tiers for that run were already fully built, already
smoke-tested, and already publishable. They shipped nothing.

ROCmFPX is off by default, is documented upstream as an "experimental research
build… hardware-, driver-, model-, and prompt-sensitive"
(`docs/maestro/specs/2026-07-01-rocmfpx-stage-design.md`), and produces GGUFs
that only load on a fork. It should not have veto power over a stock-llama.cpp
deliverable that is finished and correct.

---

## Verified code facts

Every claim below was read in the tree at `de3e5ad` plus the uncommitted
issue-#5/#6 fix currently in the working tree. Where the working-tree line
numbers differ from `HEAD`, both are given — the #5/#6 fix shifted
`core/pipeline.py` (+49) and `ui/app.py` (+78/−37); `core/_rocmfpx_entry.py` is
untouched, so its numbers are the same in both.

### The abort chain

1. **`core/_rocmfpx_entry.py:890-892`** — the abort under discussion:
   ```
   if not produced:
       print("Error: no ROCmFPX GGUF files produced", flush=True)
       sys.exit(1)
   ```
   `produced` is appended to only when a per-format helper returns a non-`None`
   path (`:887-888`).

2. **`core/_rocmfpx_entry.py:895-909`** — the post-generation PPL smoke gate.
   A **separate** exit, deliberately hard-failing. **Out of scope; unchanged.**
   See "What this does and does not fix" below — this distinction matters more
   than it first appears.

3. **`ui/app.py:1144-1156`** — `do_rocmfpx`: `rc = await run_script(...)`,
   `ok = rc == 0`, stage set to `FAILED`, `return ok`.
   **`core/pipeline.py:1341-1343`** — `stage_rocmfpx` does the same via
   `_run_stage_script`.

4. The generic loop, in both orchestrators, byte-identical in intent:
   - `ui/app.py:1428-1437` (HEAD `1420-1429`)
   - `core/pipeline.py:1508-1523` (HEAD `1463-1478`)

   ```
   ok = ...
   if not ok:
       log("Pipeline stopped at {stage_name}", "error")
       break
   ```

5. Both orchestrators order `rocmfpx` immediately before `upload`:
   `ui/app.py:162` (`ALL_STAGES`) and `core/pipeline.py:1456-1465` (`STAGES`).

### The disclosure machinery

`<out>/rocmfpx/_refusals.json`, owned by `core/_rocmfpx_entry.py`:
`REFUSALS_FILENAME` (`:1118`), `_rewrite_refusals` (`:1126`), `_record_refusal`
(`:1179`), `_clear_refusal` (`:1252`). Read back by
`core/publish_records.read_refusals`, called from
`core/hf_upload._find_refused_tiers` (`:335-362`), which filters strictly on
`family == parent.name` — the binding per-family-dir contract documented at
`_rocmfpx_entry.py:1204-1217`. A tier that later builds clears its own record
(`_run_ttf_quantize:1295`), so a stale refusal cannot outlive its cause.

This is the channel a public model card renders refusals from. It is the right
thing to key a "was this drop disclosed?" test on, precisely because it is what
the card reads.

---

## The refinement: `produced == []` is not synonymous with "cleanly refused"

This is the load-bearing part of the memo. Every option below is wrong if it
treats an empty `produced` list as a single, uniform condition.

Eleven distinct code paths shorten `produced`. **Three** write a refusal
record. **Eight** return `None` with nothing on disk to disclose:

| # | Site | Trigger | Record written? |
|---|---|---|---|
| 1 | `_quantize_mq_hybrid:1350-1360` | Band guard — rendered config lands in a different size band than the tier claims | **Yes** (`rule` omitted ⇒ `"band"`) |
| 2 | `_quantize_mq_budget:1504-1523` | Rendered size could not be predicted at all (fail-closed) | **Yes** (`rule="budget"`) |
| 3 | `_quantize_mq_budget:1525-1547` | Predicted size exceeds `budget_gib × (1 + BUDGET_TOLERANCE)` | **Yes** (`rule="budget"`) |
| 4 | `_quantize_preset:967-971` | `parse_format_spec` `ValueError` — unknown/typo'd format spec | No |
| 5 | `_quantize_preset:973-977` | `validate_types_supported` `RuntimeError` — type absent from this fork build | No |
| 6 | `_quantize_preset:985-988` | `llama-quantize` non-zero exit, or output file missing | No |
| 7 | `_quantize_mq_hybrid:1307-1315` | `magicquant` not importable | No |
| 8 | `_quantize_mq_hybrid:1369-1371` | `FileNotFoundError` / `KeyError` / `ValueError` / `RuntimeError` from `_load_mq_tier_config`, `build_tensor_type_lines`, `pick_base_type`, `translate_scheme`, `validate_types_supported` | No |
| 9 | `_quantize_mq_budget:1433-1441` | `magicquant` not importable | No |
| 10 | `_quantize_mq_budget:1550-1552` | Outer `except` — missing/malformed `search_results.json`, unresolvable or ambiguous `BUDGET-*` key, missing `budget_bytes`, unsupported type | No |
| 11 | `_run_ttf_quantize:1289-1292` (both mq paths) | `llama-quantize` non-zero exit, or output file missing | No |

Rows 1-3 are the system working: a deliberate, predicted, reasoned refusal with
a card-voice sentence already stored for the model card to render. Rows 4-11 are
breakage: a typo in a formats list, a stale fork build missing a type, a
quantizer crash, a missing `search_results.json`.

**A blanket "continue past any ROCmFPX failure" converts rows 4-11 into
silence.** The operator gets a green run, an upload, and a model card that says
nothing at all about the tier that isn't there — which is the exact failure
shape (`a disclosure that exists nowhere`) that `_refusals.json` was built to
close, rebuilt one layer up.

### Therefore, the rule

> Continue past a ROCmFPX stage failure **only when every requested format that
> did not build has a matching entry in `<out>/rocmfpx/_refusals.json`**.
> Anything else requires the flag to be irrelevant: it aborts.

Stated as a computation at the end of `_rocmfpx_entry.run()`:

```
requested    = formats from cfg
built        = specs whose helper returned a path
missing      = requested − built
disclosed    = { s ∈ missing : refusal_key(s) has an entry in _refusals.json
                               with family == "rocmfpx" }
undisclosed  = missing − disclosed
```

- `undisclosed` non-empty → **`sys.exit(1)`, regardless of the flag.**
  `--allow-partial` is a licence to ship an incomplete ladder, never a licence
  to ignore errors. (The alternative — letting the flag cover undisclosed
  failures too — was considered and rejected: it is the blanket rule above.)
- `undisclosed` empty, `built` non-empty → proceed. **This is already today's
  behaviour** and needs no flag: a partial ladder whose every gap reaches the
  card is exactly what rows 1-3 were built for.
- `undisclosed` empty, `built` empty → **the only case the flag governs.**
  Without it: exit 1, as today. With it: log loudly and return success.

So the flag's entire effect is to turn one `sys.exit(1)` into a warning, under
one precondition.

### Two mechanical consequences of that rule

**A spec→key mapping is needed, and `run()` currently discards it.** `mq-q4` →
`"Q4"` via `parse_mq_spec` (`:92-123`); `mq-budget` → whatever
`_resolve_budget_key` (`:1049-1091`) resolves — and that function can itself
raise, so an unresolvable budget spec has no key at all and correctly falls into
`undisclosed`; `mq-budget=<KEY>` → the literal, case-exact key. Every
`_quantize_*` helper returns only `Optional[Path]`, so the reason is thrown
away. **Preferred approach: have `run()` re-read `_refusals.json` after the loop
and match on keys**, rather than plumbing reason codes back through three
helpers. Testing the disclosure channel itself is stronger than testing a
parallel in-memory signal — anything else could pass the gate while the card
still had a silent gap.

**Uniform presets can never satisfy the predicate today.** `_record_refusal` is
keyed on `(tier, family)` and every call site is an `mq-*` path; a
`rocmfp4-agent` refusal writes no record and has no key. For an all-preset
formats list, `undisclosed` can never be empty, so the flag will never apply.
That is the *correct* conservative behaviour under the rule, but it must be
written down, tested, and not later mistaken for a bug. Giving presets their own
refusal records is a reasonable follow-up; it is not part of this decision.

---

## Option analysis

### A — Per-stage `required` / `optional` flag

Add `required: bool = True` to each stage config (or a set in the orchestrator);
`if not ok and stage_required: break`.

- **For:** declarative, general, and it names the real semantic — ROCmFPX *is*
  an optional stage. One concept, both orchestrators.
- **Against (fatal as stated):** it is a *standing* property. It is on for every
  run of that stage, including the run where the ROCmFPX file **is** the
  deliverable. That workflow is explicitly supported —
  `hf_upload.plan_gguf_repos:194-200` returns `[(repo_id, "rocmfpx")]` when
  ROCmFPX GGUFs exist and MagicQuant's do not. Marking `rocmfpx` permanently
  optional makes a ROCmFPX-only run upload nothing and report success. Fixing
  that needs an "…unless it is the only quant stage enabled" carve-out — an
  implicit rule that will be wrong the first time a third quant family lands.
- **Against:** as stated it makes no distinction between rows 1-3 and rows 4-11.
- **Cost:** the same config plumbing as D, spread over eight stages' worth of
  semantics instead of one, *plus* an edit to the shared loop in two files.

**Verdict: second choice.** Right vocabulary, wrong lifetime — a per-run
decision expressed as a permanent property.

### B — Hardcode `rocmfpx` best-effort

`if not ok and stage_name != "rocmfpx": break`, or make the stage always return
`True`.

- **For:** one line per orchestrator, zero config surface, closes the reported
  incident immediately.
- **Against (fatal):** it permanently removes the operator's ability to get a
  failing signal from this stage. A ROCmFPX-only run reports success having
  produced nothing. All eleven failure modes go silent, including a genuine
  `llama-quantize` crash. This is the blanket rule the refinement rules out.
- **Against:** it hardcodes one stage's name into the shared loop in two
  separate files — the drift shape this repo has already paid for. `core/services.py`
  exists specifically because `ui/app.py` and `core/pipeline.py` diverged, and
  the #5/#6 fix landed *this week* for the same reason.

**Verdict: rejected.** The fastest fix and the one most likely to author the
next incident.

### C — Reorder / always-attempt-upload

Two different proposals under one heading; they fail differently.

- **Reorder (`upload` before `rocmfpx`)** is self-defeating: a *successful*
  ROCmFPX build then never gets published in the same run. It relocates the
  unpublished artifact from one family to the other. It would need a second
  upload pass, which is a bigger change than D.
- **Always-attempt-upload** (hoist upload out of the `break`) is far broader
  than the problem: a failed `export` or a failed `magicquant` would also
  proceed to upload. Export failing and then uploading is precisely how a repo
  gets a card describing tiers that do not exist — the condition
  `audit_card_against_repo` was built to catch. It would start firing routinely
  instead of never.

**One merit worth carrying into D, and verified:** the upload stage already
degrades correctly on an empty ROCmFPX directory.
`plan_gguf_repos` (`hf_upload.py:180-201`) computes `has_fpx` from
`glob("rocmfpx/*.gguf")`; with none present the two-repo split collapses to the
single MagicQuant repo. There is no half-formed `-ROCmFPX-GGUF` repo to clean
up. "Continue to upload with zero ROCmFPX files" is a well-defined state today
and needs no downstream change.

**Verdict: rejected as the mechanism, but its degradation property is a
precondition D relies on, and it holds.**

### D — Run-level `--allow-partial` (recommended)

One opt-in flag. When set, a ROCmFPX stage failure whose every gap is disclosed
is logged and the run continues to upload.

- **Matches the house idiom exactly.** Three existing precedents share one
  grammar — the unsafe-but-sometimes-right thing is *possible*, off by default,
  named for what it permits, and the risk judgement stays with the human:
  - `--rocmfpx-allow-requantize` (`core/pipeline.py:1744-1749`)
  - `allow_dequant_source` (`core/pipeline.py:195`, `ui/app.py:270`,
    propagated via `_magicquant_entry.apply_dequant_env:38-53`)
  - MagicQuant's `--allow-partial-probes` (`magicquant/__main__.py:986`,
    `v2/search.py:69`)
- **Default behaviour is untouched.** Anyone who has not thought about this
  keeps today's abort.
- **No orchestrator change at all.** The `if not ok: break` in both loops stays
  byte-for-byte identical, because the stage decides its own exit code. This is
  the decisive cost argument over A, B and C, all of which require editing the
  same shared loop in two files that have already drifted once.
- **Marker semantics already correct — verified, no change needed.**
  `do_rocmfpx` (`ui/app.py:1146-1152`) writes the completion marker only when
  `ok and rc_dir.exists()` **and** `sorted(rc_dir.glob("*.gguf"))` is non-empty.
  A stage that produced nothing therefore writes no marker even when it now
  returns success, so the next run re-attempts it rather than skipping it.

**Its known weakness — "the operator must opt in beforehand" — is largely
neutralised, and this is now verifiable rather than hypothetical.** The
issue-#5 fix in the working tree makes `derive_model_short_name`
(`core/services.py:131-185`) fall through to `_existing_run_basename(output_dir)`
when no *enabled* stage carries a source model. Re-running with
`enabled_stages: ["upload"]` alone now resolves to the completed run's own
directory instead of creating a fresh empty `output/ThinkingCap-Qwen3.6-27B`.
Recovery from a stalled pipeline is a config edit and a re-run, not a rebuild.
That downgrades D's weakness from "you lose the run" to "you re-click upload".

**Plumbing (for costing).** The flag is *run-level* in the CLI, but the value
must reach `_rocmfpx_entry.run()` through the per-stage config, exactly as
`allow_requantize` already does — one added field in each of four places, all of
which already carry the identical neighbour:
`core/pipeline.py:205-220` (`ROCmFPXConfig`), `ui/app.py:290-301`
(`ROCmFPXCfg`), `core/services.py:588-611` (`ROCmFPXService.build_config`), and
the two stage call sites (`core/pipeline.py:1330-1339`, `ui/app.py:1134-1143`).
Plus one argparse flag and one UI checkbox mirroring the existing
`allow_requantize` control.

**Verdict: recommended, with the disclosure predicate. Ranking: D > A > C > B.**

---

## What this does and does not fix

This needs saying plainly rather than being left to inference.

The incident report states the `mq-q4` build **failed its smoke test**. The
smoke gate is a *different* exit — `core/_rocmfpx_entry.py:895-909` — from the
`produced == []` abort at `:890-892` that this memo scopes. The gate is
intentional and stays: `core/ppl_smoke.py`'s module docstring records the
2026-07-27 incident it closes (MagicQuant's `SafetensorsSource` silently
produced garbage `qwen3_5` quants, caught only by an operator running perplexity
by hand after the fact). Shipping a pathological file is worse than shipping
nothing.

So: **if the reported run died at the smoke gate, the change recommended here
would not by itself have unblocked it.** It closes the adjacent abort. Unblocking
the smoke-gate case is a separate decision, and the honest answer there is
*quarantine the bad file and continue* — never *ship it anyway*.

Which leads to a hazard that is **already live today**, independent of this
decision:

> A GGUF that fails the PPL smoke gate is **not deleted**.
> `ppl_smoke.smoke_test_gguf` (`:187-238`) returns a bool; nothing unlinks the
> file, and `_rocmfpx_entry.py:900-909` only exits. Upload discovers GGUFs by
> `glob("rocmfpx/*.gguf")` (`hf_upload.discover_upload_files:143-145`), and the
> publish criteria are BAND / DOMINANCE / SPEED / BUDGET only
> (`core/publish_criteria.py`) — none of them is "passed the smoke gate".
> **A smoke-failed file left on disk is publishable by any later upload-only
> run** — including the `["upload"]` resume path the #5 fix has just made
> convenient.

This is the strongest reason not to extend `--allow-partial` to cover
smoke-gate failures, and it should be filed as its own issue: quarantine a
smoke-failed GGUF (rename to `.failed-smoke`, or move to
`<out>/rocmfpx/_quarantine/`) so the upload glob cannot reach it.

---

## Risks of the recommendation

1. **Requires foresight.** Mitigated by the `["upload"]`-alone resume path
   above; recovery is cheap and no longer creates a wrong directory.
2. **A fourth `allow_*` flag.** Flag proliferation is real, but all four share
   one grammar and each names a distinct hazard. A generic `--force`-style
   catch-all would be strictly worse — it would license exactly the blanket
   behaviour this memo rejects.
3. **The predicate is only as good as `_refusals.json`.** Two verified soft
   spots, both worth an explicit test:
   - `_rewrite_refusals` (`:1126-1176`) is advisory by design — an unwritable
     directory, a full disk, or a hand-mangled file logs a warning and
     continues. A refusal that failed to persist is indistinguishable from one
     that never happened, so the gate would treat that format as *undisclosed*
     and abort. That is the fail-safe direction, but it should be pinned.
   - `_quantize_mq_hybrid`'s band prediction is wrapped in `except Exception` at
     `:1361` and is **advisory**: a prediction failure prints
     `(tier-band prediction unavailable: …)` and the build proceeds *unguarded*.
     The budget path deliberately fails closed at `:1495-1523`, with a comment
     recording the incident that forced it (228 of 753 tensors in a real budget
     block are F32, which raised inside `predict_rendered_budget` and was
     swallowed every time). This asymmetry is out of scope here, but it means
     the refusal record is a best-effort disclosure, not a completeness
     guarantee — nobody should read it as the latter.
4. **Naming.** `--allow-partial` reads run-level but is threaded per-stage. If a
   second stage later wants the same semantics, the flag must fan out.
   Acceptable, and better than inventing a per-stage name now.

---

## Acceptance criteria for an implementation

No code is proposed here. An implementation should have to prove all of:

1. Every requested format refused *with a record*, flag set → exit 0, **no
   completion marker written**, upload runs, card discloses each refusal.
   (Extends `tests/test_rocmfpx_refusal_record.py`.)
2. The same scenario without the flag → exit 1. Default behaviour unchanged.
3. **The refinement's regression pin:** one format refused with a record, one
   failing on a bad spec (`parse_format_spec` `ValueError`, row 4), flag set →
   **exit 1**.
4. A `llama-quantize` non-zero exit (rows 6/11) with the flag set → exit 1.
5. A partially-successful run (some built, one cleanly refused) → exit 0 with
   *and* without the flag. Pins today's behaviour against accidental change.
6. `plan_gguf_repos` with an empty `rocmfpx/` returns the single-repo plan —
   pins the degradation property D depends on.
7. An all-preset formats list, all failing, flag set → exit 1, because presets
   write no refusal records. Pins the documented limitation so it cannot later
   be mistaken for a bug.

---

## Out of scope

- **The PPL smoke gate** (`core/_rocmfpx_entry.py:895-909`) — a separate,
  intentional "don't ship garbage" gate. Unchanged.
- Issues #5 and #6 — fixed; the change is in the working tree, uncommitted.
- Refusal records for uniform presets — a follow-up that would widen where the
  disclosure predicate can apply.
- Quarantining smoke-failed GGUFs — a follow-up, and it should be its own
  issue; the hazard exists today regardless of this decision.

---

## Test baseline

```
.venv/bin/python -m pytest tests/ -q --ignore=tests/test_training_integration.py -m "not slow and not gpu"
```

- **Before this memo (2026-08-13 10:20): `747 passed, 1 skipped`.**
- **After (10:26): collection error, 0 tests run — for an unrelated,
  environmental reason that appeared in between.**

This memo adds one Markdown file and modifies no source, so it cannot affect
the suite. The regression is external: MagicQuant's master merged `eca4dec` at
10:24, which raised its `gguf` floor to `>=0.19.0` and added `NVFP4` to
`ggml_facts.REQUIRED_STOCK_NAMES`. Foundry's `.venv` still carries
`gguf==0.18.0`, so `magicquant.quant.ggml_facts._check_required_stock_names_present()`
now raises `ImportError` at import time. Foundry reaches MagicQuant through the
`MagicQuant/magicquant` symlink and `core/publish_criteria.py:54` imports
`magicquant.quant.tiers` at module scope, so all 744 collected tests fail to
collect behind that one import.

**Fix is environmental, not code:** upgrade Foundry's venv to `gguf>=0.19.0` to
match MagicQuant's new floor. Deliberately not done here — it is outside this
task's scope and the shared venv has other consumers.
