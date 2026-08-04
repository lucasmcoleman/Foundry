# Size-Target Quantization: MagicQuant v2 Through Foundry + Consumers

Date: 2026-08-03. Status: approved design, revised after fresh-context spec
review (all blocking issues addressed). Spans Foundry and MagicQuant.
The ds4/DeepSeek work is a separate spec:
`2026-08-03-ds4-deepseek-gguf-design.md`.

## Goal

1. Let a user ask Foundry for "the highest-quality quant that fits under
   N GiB" (a size target), instead of only the fixed Q4/Q5/Q6 tier ladder.
2. Let downstream stages consume a size-target build the way they consume
   tier builds — a ROCmFPX rendering of a budget build (speed), and QAT
   compensation training against one.

## Background facts (verified 2026-08-03, spot-checked by an independent reviewer)

- MagicQuant ships a budget-constrained search: `--algo v2 --budget-gb <GiB>`
  (`magicquant/v2/search.py:run_budget_search`). Exact per-tensor byte counts
  via the real ggml encoder (`v2/resolve.py` imports the writer's own fallback
  helpers — block-32 and SSM/F32 fallbacks are already priced, with parity
  tests), MCKP under a hard byte ceiling (`v2/allocate.py`), 2–3 frontier
  anchors verified with real perplexity, `BudgetInfeasibleError` (carries
  `min_bytes`) when the budget is below the floor. Writes `v2_results.json`,
  `frontier.json`, and a GGUF named `<stem>-v2-budget-<gb>gb.gguf`.
- **The budget bounds allocatable-tensor bytes, not file size.** Passthrough
  tensors with unknown byte counts are skipped by `_build_units`, and GGUF
  metadata/alignment padding is uncounted; v2 itself records `predicted_bytes`
  and `actual_bytes` separately because they diverge. Every guard in this spec
  therefore compares with an explicit tolerance (below).
- Units: `budget_gb` is GiB (`int(budget_gb * 1024**3)`). All user-facing
  naming in this feature says **GiB**.
- v1's interchange is `search_results.json`:
  `tiered[tier].config = {group_letter: scheme_name}` (per-group). Consumers:
  QAT via `magicquant.qat.config.load_hybrid_config(path, tier)` (tier is an
  unvalidated free string end-to-end in Foundry, so a pseudo-tier key needs no
  plumbing change); ROCmFPX mq-hybrid via `core/_rocmfpx_entry.py`, which
  hard-codes the path `<out>/magicquant/search_results.json`, as does QAT
  auto-detect in `core/pipeline.py`.
- v2 already computes a per-group dominant-scheme projection:
  `_dominant_group_schemes` (`v2/search.py`), parameter-count-weighted,
  skipping `fixed` tensors, published as `results["group_summary"]`.
- The ROCmFPX fork's `--tensor-type-file` accepts per-tensor lines. Matching
  is unanchored `std::regex_search` and the parser tokenizes on whitespace —
  per-tensor lines must be `^…$`-anchored, regex-escaped, and contain no
  whitespace. The fork applies its own `tensor_type_fallback` AFTER an
  explicit per-tensor override, so a rendered type can still differ from the
  requested one; the size guard must price what will actually render.
- `_magicquant_entry.run()`'s shared tail: `generate_tiered_models` → hard-
  failing PPL smoke gate (`ppl_smoke.smoke_test_gguf`) → stage-complete
  marker. Publish orchestration currently lives in the gitignored
  `output/_publish_tiers.py` (untested — the known-bad pattern); decision
  logic is tracked in `core/publish_criteria.py`, card rendering in
  `core/hf_upload.py` (`audit_card_against_repo` warns on any repo GGUF the
  card doesn't mention).

## The one tolerance rule

A budget build's **final file size** may exceed the requested budget by at
most **2%** (covers uncounted metadata/padding/passthrough for MagicQuant
builds and fork-type rounding for ROCmFPX builds). One constant, defined once
in `core/publish_criteria.py` (`BUDGET_TOLERANCE = 0.02`), used by:
- the ROCmFPX build-time guard (refuse to build a render predicted over it),
- the publish-time guard (refuse to publish a file over it),
so build-time and publish-time can never disagree. The card always disclose
both requested budget and achieved size; landing *under* budget is never a
violation.

## Part 1 — Wire v2 through Foundry

New option threaded through the same four layers as every existing knob:

- CLI: `--magicquant-budget-gib <float>` (`core/pipeline.py`).
- Config: `MagicQuantConfig.budget_gib: float | None = None`.
- Service: `MagicQuantService.build_config(..., budget_gib=None)` → JSON key.
- UI: `MagicQuantCfg.budget_gib` field + input on the MagicQuant card.

Semantics:

- `budget_gib` set → third branch in `core/_magicquant_entry.py`: build
  `V2Config`, call `run_budget_search`. v1 knobs that don't apply to v2 are
  ignored *loudly* (logged per knob, not silent).
- `budget_gib` + `--magicquant-measured` → refused at config-build time (v2
  does its own real-perplexity verification; "measured" would misrepresent
  what ran).
- `BudgetInfeasibleError` → stage failure whose message states the floor from
  `min_bytes` ("budget 8 GiB is below the floor; minimum achievable is
  11.2 GiB").
- Output: the entry renames v2's GGUF to `<Model>-BUDGET-<N>GiB.gguf` (the
  name claims the request, never a tier label) and maps it into the files-list
  shape the upload stage consumes.
- **The budget branch routes through the same PPL smoke gate** as the tier
  path before emitting the stage-complete marker. No budget build reaches
  upload without passing it.

Publishing:

- New pure function `decide_budget_build(...)` in `core/publish_criteria.py`:
  BAND and DOMINANCE do not apply (no tier claim, no siblings); the size guard
  applies per the tolerance rule. Refusals are recorded (schema below).
- **All budget-publish orchestration and card rendering live in tracked code**
  (`core/hf_upload.py` + `core/publish_criteria.py`) — nothing goes in
  `output/`. The card gets a dedicated budget-build section (requested budget,
  achieved size, v2 measured perplexity); `audit_card_against_repo` must
  recognize `-BUDGET-<N>GiB` files so an undisclosed one fails the audit.

## Part 1b — Consumers

### Interchange (MagicQuant-side, small and strictly additive)

`run_budget_search` additionally writes/updates
`<out>/search_results.json` (the path consumers hard-code) by **merging**:

- Load the existing file if present; add or replace ONLY the
  `tiered["BUDGET-<N>GiB"]` key; never touch other tiers. A budget run in a
  directory holding a v1 Q4/Q5/Q6 search leaves those tiers intact. If no
  file exists, create one containing just the budget block.
- The block carries:
  - `config`: the per-group projection — **reuse `_dominant_group_schemes`
    as-is** (parameter-count weighting; deviating to byte-weighting is not
    worth forking the math). Groups whose tensors are all `fixed` are absent;
    absent groups are simply not fake-quantized by QAT, which matches their
    full-precision semantics.
  - `tensor_config`: exact per-tensor `{tensor_name: scheme_name}` (new
    field; v1 consumers ignore it).
  - Provenance: `algo: "v2"`, `budget_bytes`, `predicted_bytes`,
    `actual_bytes`, and `tier_scheme_version: CURRENT_TIER_SCHEME_VERSION`
    (without the stamp, `load_hybrid_config` emits a misleading legacy-bands
    warning on a file that makes no band claim).

### ROCmFPX budget mode (Foundry `core/_rocmfpx_entry.py`)

- New preset `mq-budget` alongside `mq-q4/5/6`. Resolution: scan `tiered` for
  `BUDGET-*` keys — exactly one → use it; zero or multiple → loud refusal
  listing what was found (explicit form `mq-budget=BUDGET-<N>GiB` selects
  among multiple).
- Prefers `tensor_config`: emits per-tensor `--tensor-type-file` lines,
  `^…$`-anchored and regex-escaped, refusing any tensor name containing
  whitespace. Falls back to per-group `config` via the existing group-pattern
  path when `tensor_config` is absent.
- Build-time size guard: price the render from v2's per-tensor byte records
  mapped through fork-type bpw (NOT `predict_rendered_tier`, which is
  per-group by construction), accounting for the fork's fallback-after-
  override behavior; refuse if predicted size exceeds the tolerance rule.
- Output name: `<Model>-ROCMFPX-MQ-BUDGET-<N>GiB.gguf`.
- SPEED publish criterion: ships only if measurably faster than the
  MagicQuant budget build it mirrors (same rule as tiers; the peer is the
  budget build).

### Refusal record extension

`_record_refusal`'s schema is band-shaped (`predicted_band`/`claimed_band`).
Add a `rule: "budget"` record variant carrying `requested_budget_bytes` and
`predicted_bytes`, and extend the card-side refusal renderer to phrase budget
refusals as size overshoot, not band prose. This is budgeted work, not free.

### QAT

Consumes the per-group projection via existing
`load_hybrid_config(path, "BUDGET-<N>GiB")`. No code change expected (tier is
a free string end-to-end); the plan verifies rather than assumes. Disclosed
limitation: QAT trains against the per-group *approximation* of a per-tensor
build. Per-tensor QAT is a named follow-on, built only if the approximation
measures too lossy.

## Non-goals

- Per-tensor QAT (follow-on, gated on measured need).
- v2 support for ROCmFPX *search* (v1's `--magicquant-rocmfpx` unchanged).
- Any change to v1 tier semantics, bands, or the existing publish ladder.
- Migrating v1 publish orchestration out of `output/_publish_tiers.py`
  (worth doing, separate task — only the NEW budget path is required to live
  in tracked code).

## Risks

- Uncounted bytes (metadata, padding, skipped passthrough tensors) are the
  live gap between v2's `budget_bytes` and the file on disk — covered by the
  tolerance rule, but a model with an unusually large uncounted fraction
  (>2%) would make v2 builds unpublishable; the refusal message must say the
  overshoot came from uncounted bytes so it reads as diagnosis, not mystery.
- Strictly-additive discipline on MagicQuant: the seed-pinned v1 regression
  fixture must stay byte-identical; the interchange change adds output only.
- Fork `tensor_type_fallback` after per-tensor override: the guard prices
  renders, but a fallback the pricing missed shows up as a size/quality
  surprise — the existing `writer._fallbacks`-style disclosure pattern
  applies.

## Test plan (offline, CI-runnable in both repos)

- Config threading: flag → JSON → branch selection; measured+budget refusal;
  GiB naming end-to-end.
- Entry branch with stubbed `magicquant.v2`: success (rename + files-list +
  smoke gate invoked), infeasible (message contains floor), loud-ignore of
  inapplicable v1 knobs.
- Interchange: merge preserves existing v1 tiers; block round-trips through
  `load_hybrid_config`; `tensor_config` verbatim; version stamp present;
  projection equals `_dominant_group_schemes` output.
- ROCmFPX budget mode: per-tensor line emission (anchoring + escaping),
  whitespace-name refusal, zero/multiple BUDGET-key refusal, group-path
  fallback, size-guard refusal recorded with the budget-shaped schema,
  speed-peer resolution.
- Publish: `decide_budget_build` exemptions + tolerance guard both sides of
  the boundary; card discloses requested/achieved/PPL;
  `audit_card_against_repo` flags an undisclosed budget file.

## Sequencing

1. Part 1 (threading + publish path).
2. Part 1b (interchange, then ROCmFPX budget mode, then QAT verification).
Each lands with its tests; both repos' CI green at every step.
