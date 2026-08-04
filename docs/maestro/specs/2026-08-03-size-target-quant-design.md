# Size-Target Quantization + ds4 GGUF for DeepSeek-V4-Flash

Date: 2026-08-03. Status: approved design (this doc), pending implementation plan.
Scope spans Foundry and MagicQuant. Approved by Lucas 2026-08-03, including the
consumer-integration extension ("everything should be able to consume v2").

## Goal

1. Let a user ask Foundry for "the highest-quality quant that fits under N GB"
   (a size target), instead of only the fixed Q4/Q5/Q6 tier ladder.
2. Let downstream stages consume a size-target build the way they consume tier
   builds today — specifically a ROCmFPX rendering of a budget build (speed),
   and QAT compensation training against one.
3. Separately: produce our own ds4-loadable GGUF of DeepSeek-V4-Flash-0731 to
   run under antirez's ds4 on this box (Strix Halo, 121 GiB usable RAM).

## Background facts (verified 2026-08-03)

- MagicQuant already ships a budget-constrained search: `--algo v2
  --budget-gb <GiB>` (`magicquant/v2/search.py:run_budget_search`). It computes
  exact per-tensor byte counts with the real ggml encoder, solves an MCKP under
  a hard byte ceiling, verifies 2–3 frontier anchors with real perplexity, and
  raises `BudgetInfeasibleError` (with the achievable floor) when the budget is
  below the minimum. It writes `v2_results.json` + `frontier.json` + its own
  GGUF. It is unreachable from Foundry today.
- v1's interchange file is `search_results.json` with
  `tiered[tier].config = {group_letter: scheme_name}` (per-GROUP).
  Consumers: QAT via `magicquant.qat.config.load_hybrid_config(path, tier)`;
  ROCmFPX mq-hybrid via Foundry `core/_rocmfpx_entry.py`
  (`build_tensor_type_lines(config, group_patterns)` → `--tensor-type-file`).
- v2's allocation is per-TENSOR. `--tensor-type-file` accepts per-tensor lines,
  so ROCmFPX can reproduce a v2 layout *exactly*; QAT's contract is per-group.
- ds4 (antirez) loads only its own fixed recipes (hardcoded per-tensor-role
  types in ds4.c). Its `gguf-tools/deepseek4-quantize` (plain C, no GGML) reads
  the official HF safetensors + a template GGUF and supports `--imatrix`
  (llama.cpp legacy `.dat`). The only recipe family that fits 121 GiB is
  `IQ2XXS-w2Q2K-AProjQ8-SExpQ8-OutQ8` (~81 GiB). The Q4-expert family renders
  150–170 GB. SSD-streaming for DeepSeek is Metal-only; on ROCm the model must
  fit in GPU-visible memory.
- DeepSeek-V4-Flash-0731: 304B total / ~13B active MoE, 167 GB native FP8-attn
  / FP4-expert checkpoint, MIT. Disk free after cleanup: ~1.1 TB.

## Part 1 — Wire v2 through Foundry

New option threaded through the same four layers as every existing knob:

- CLI: `--magicquant-budget-gb <float>` (`core/pipeline.py`).
- Config: `MagicQuantConfig.budget_gb: float | None = None`.
- Service: `MagicQuantService.build_config(..., budget_gb=None)` → JSON key.
- UI: `MagicQuantCfg.budget_gb` field + input on the MagicQuant card.

Semantics:

- `budget_gb` set → the stage runs `magicquant.v2.run_budget_search` via a new
  third branch in `core/_magicquant_entry.py` (builds `V2Config`; existing v1
  knobs that don't apply to v2 are ignored *loudly* — logged, not silent).
- `budget_gb` + `--magicquant-measured` → refused at config-build time with a
  clear error (v2 does its own real-perplexity verification; "measured" would
  misrepresent what ran).
- `BudgetInfeasibleError` → caught and re-raised as a stage failure whose
  message states the minimum achievable size ("budget 8 GB is below the floor;
  minimum achievable is 11.2 GB").
- Output mapping: v2 writes one GGUF under its own naming
  (`<stem>-v2-budget-<gb>gb.gguf`); the entry renames it to the publish
  convention `<Model>-BUDGET-<N>GB.gguf` (name claims the *request*, never a
  tier label) and maps it into the files-list shape the upload stage consumes.

Publishing (`core/publish_criteria.py`):

- New `decide_budget_build(...)` path. BAND does not apply (no tier claim in
  the name — but the card MUST disclose requested budget, achieved size, and
  v2's measured perplexity). DOMINANCE does not apply (no siblings). A budget
  build in the same repo as tier builds gets its own card section.
- Guard retained: achieved size must be ≤ requested budget (v2's contract);
  if a post-hoc check finds otherwise, publish refuses and records the refusal
  (same `_refusals.json` mechanism as ROCmFPX).

## Part 1b — Everything consumes v2 (consumer integration)

Interchange (MagicQuant-side change, small): `run_budget_search` additionally
writes a `search_results.json`-compatible block so existing consumers work:

- A pseudo-tier key `BUDGET-<N>GB` under `tiered`, carrying:
  - `config`: a per-group **projection** of the per-tensor allocation
    (size-weighted dominant scheme per group) — backward-compatible with
    `load_hybrid_config` and every other v1 consumer, unchanged.
  - `tensor_config`: the exact per-tensor `{tensor_name: scheme_name}` map —
    the full-fidelity record (new field; v1 consumers ignore it).
  - Provenance fields: `algo: "v2"`, requested budget bytes, achieved bytes.
- Rationale (derive-from-upstream): the projection math lives in MagicQuant,
  one source of truth; Foundry never re-derives scheme/group semantics.

ROCmFPX budget mode (Foundry `core/_rocmfpx_entry.py`):

- New preset `mq-budget` alongside `mq-q4/5/6`. Resolves the `BUDGET-<N>GB`
  pseudo-tier; prefers `tensor_config` and emits exact per-tensor
  `--tensor-type-file` lines (escaped literal names, not group regexes);
  falls back to the per-group `config` with today's group-pattern path when
  `tensor_config` is absent.
- Guard (analog of the band guard): predict the rendered size after
  fork-type rounding; refuse to build if it exceeds the requested budget by
  more than 2% (fork has no type between 4.5 and 6.5 bpw, so rounding can
  overshoot), recording the refusal via `_record_refusal` as today.
- SPEED publish criterion: the ROCmFPX budget build ships only if measurably
  faster than the MagicQuant budget build it mirrors (same rule, peer is the
  budget build instead of the same-band tier).

QAT: consumes the per-group projection via the existing
`load_hybrid_config(path, "BUDGET-<N>GB")` — zero code change expected beyond
accepting the pseudo-tier name wherever Foundry validates the QAT `tier` config
key (exact surface confirmed during planning). Disclosed
limitation: QAT fake-quantizes the per-group *approximation* of a per-tensor
build. Per-tensor QAT is a named follow-on, built only if the approximation
measures too lossy.

## Part 2 — Our own ds4 GGUF for DeepSeek-V4-Flash-0731

"Our own" = own the calibration, not the recipe (ds4 accepts only its own
layouts; only the 2-bit family fits this box).

1. Pin + build: clone `antirez/ds4` at a commit verified against the open ROCm
   garbled-output regression (issues #364/#577); `make strix-halo`; build
   `gguf-tools`. Record the pinned SHA in the runbook.
2. Download `deepseek-ai/DeepSeek-V4-Flash-0731` (167 GB) + a template GGUF
   (metadata/tokenizer donor required by `deepseek4-quantize`).
3. Two-pass imatrix: (a) quantize with the synthetic-importance fallback;
   (b) run ds4's imatrix collection (real prefill graph) over OUR calibration
   corpus — same blend discipline as MagicQuant's (multilingual + code + math
   + agentic, disjoint from any eval text); (c) requantize with the measured
   `.dat`. Recipe: `IQ2XXS-w2Q2K-AProjQ8-SExpQ8-OutQ8` (~81 GiB).
4. System prep (each step Lucas-approved before execution): GRUB GTT params
   scaled DOWN from STRIXHALO.md's 128 GB assumptions to this box's 121 GiB
   (~105–110 GiB aperture, OS headroom preserved); reboot; LM Studio /
   llama-swap stopped while ds4 runs; earlyoom stays.
5. Validation: sane-output smoke test (the known regression manifests as
   garbled text), short perplexity sanity run, comparison against antirez's
   prebuilt Q2 on the same eval. Honest outcome rule: if ours doesn't measure
   better, say so and use his.
6. Optional follow-on: DSpark support GGUF (speculative decoding).

Placement: `scripts/` + a tracked runbook doc in Foundry. NOT a pipeline stage
(model-specific, external quantizer; durable procedure must live in tracked
code per the `output/`-gitignore lesson).

## Non-goals

- Per-tensor QAT (follow-on, gated on measured need).
- Teaching ds4 to load MagicQuant output (impossible without forking ds4).
- v2 support for ROCmFPX *search* (`--magicquant-rocmfpx` explores fork types
  inside v1's search; v2's choice set stays as-is for now).
- Any change to v1's tier semantics, bands, or publish ladder.

## Risks

- v2's `Choice.bytes` accounting must reflect the GGUF writer's per-tensor
  block-size fallbacks (non-32-divisible rows → F32 etc.). Verify with a test
  before trusting the budget guarantee on hybrid architectures (Qwen3.5-style
  Mamba rows are the known case).
- The seed-pinned v1 regression fixture: all MagicQuant-side changes must be
  strictly additive (new output fields only), never touching v1 code paths.
- ds4 is self-described beta with an active ROCm regression; every build gets
  re-verified against issue #16 state on the day it's built.
- 121 GiB < the 128 GB the ds4 docs assume: GTT sizing has less headroom than
  any community-reported config; treat first model load as an OOM-risk event
  (earlyoom armed, nothing else resident).

## Test plan (all offline, CI-runnable)

- Config threading: CLI flag → JSON → entry branch selection (both repos' CI
  stay green; Foundry CI imports MagicQuant from `.magicquant-src`).
- Entry branch with stubbed `magicquant.v2`: success path, infeasible path
  (message contains the floor), measured+budget refusal.
- Interchange: v2's pseudo-tier block round-trips through
  `load_hybrid_config`; projection is size-weighted-dominant; `tensor_config`
  preserved verbatim.
- ROCmFPX budget mode: per-tensor line emission (escaping!), fallback to
  group path, size guard refusal recorded, speed-criterion peer resolution.
- Publish: `decide_budget_build` band/dominance exemptions, disclosure fields
  present, size>budget refusal.

## Sequencing

1. Part 1 (Foundry threading) — self-contained, offline-testable.
2. Part 1b (interchange + ROCmFPX/QAT consumption) — needs Part 1's plumbing.
3. Part 2 (ds4) — independent of 1/1b; big downloads + one reboot to
   coordinate with Lucas.
