# Design: MagicQuant × ROCmFPX Composition

**Date:** 2026-07-01
**Status:** Approved by user (Layer 1 + Layer 2 + measured-search flag + persistence fix)
**Repos touched:** Foundry (this repo) + sibling MagicQuant (`/server/programming/MagicQuant`)

## Goal

Produce **ROCm-optimized versions of MagicQuant-optimized quants**: MagicQuant's
evolutionary search decides the per-tensor-group precision layout; ROCmFPX
provides the AMD-native (gfx1151-tuned) tensor formats. Today the two stages are
disjoint — ROCmFPX only applies uniform presets to the BF16 base.

## Verified grounding facts

- ROCmFPX's `llama-quantize` supports `--tensor-type <regex>=<type>` and
  `--tensor-type-file`; patterns are compiled regexes matched first-match-wins
  against tensor names (`src/llama-quant.cpp:974-986`).
- The ROCmFPX formats are first-class ggml types in the fork:
  `Q4_0_ROCMFP4=100`, `Q4_0_ROCMFP4_FAST=101`, `Q6_0_ROCMFPX=102`,
  `Q8_0_ROCMFPX=103`, `Q3_0_ROCMFPX=104` (`ggml/include/ggml.h:432-436`).
- MagicQuant's `TensorGroupClassifier` maps GGUF tensor names → groups with
  regexes (reversible into override patterns), and its `QuantizationScheme`
  registry + ctypes libggml binding (`MAGICQUANT_LIBGGML_DIR` override) are
  clean extension points.
- **Bug:** `search_results.json` is only written at the end of
  `run_measured_search` (orchestrator.py:438). `run_full_search` — the path
  Foundry's stage uses — never persists it, so the QAT stage's auto-detect and
  this composition have nothing to read.
- The Foundry-default search is prediction-only (heuristic noise factors, no
  real perplexity); `run_measured_search` (Predict→Measure→Learn) exists but
  is not exposed by the Foundry stage.

## Part 0 — persistence fix (MagicQuant)

Factor the `search_results.json` save into `_save_search_results(tiered)`
called from **both** `run_measured_search` and `run_full_search`. Same schema.
Fixes QAT auto-detect for the standard pipeline path too.

## Part 1 — MagicQuant-aware ROCmFPX hybrids (Foundry)

New format specs accepted by `ROCmFPXConfig.formats`: `mq-q4`, `mq-q5`,
`mq-q6` (any tier present in the search results). For these, `_rocmfpx_entry`:

1. Reads `<output>/magicquant/search_results.json`, takes
   `tiered[TIER]["config"]` (group → MagicQuant scheme name).
2. Translates each scheme to the nearest ROCmFPX-family type, **rounding up
   in quality** so MagicQuant's sensitivity intent is preserved:
   - `BF16`/`F16` → `bf16` (unchanged)
   - `Q8_0` → `q8_0_rocmfpx`
   - `Q6_K`, `Q5_K` → `q6_0_rocmfpx`
   - `Q4_K*`, `IQ4_NL`, `MXFP4*` → `q4_0_rocmfp4`
   - `Q3_K`, `Q2_K` → `q3_0_rocmfpx`
3. Emits a `--tensor-type-file`: one `regex=type` line per group, ordered
   specific-first (E/H exact names, then S/R/V/X, then Q/K/O/U/D, then a
   `.*=f16` catch-all mirroring MagicQuant's UNKNOWN→F16 behavior). Group
   regexes come from MagicQuant's classifier (lazy import; clear error if the
   `magicquant` package or the search results are absent).
4. Runs `llama-quantize` with base type = the tier's dominant translated type,
   the type-file, and optional imatrix. Output:
   `<model>-ROCMFPX-MQ-<TIER>.gguf` in `<output>/rocmfpx/`.

CLI (`--rocmfpx-formats mq-q4 ...`), UI format tags, docs, and stdlib-only
unit tests (translation table, ordering, file emission) follow the existing
stage conventions.

## Part 2 — ROCmFPX types inside MagicQuant's search

- `ggml_binding.GGML_TYPE_IDS` += the five fork type IDs; block sizes/bytes
  are queried from the loaded lib at runtime (`ggml_blck_size`/`ggml_type_size`)
  rather than hardcoded; `_verify_type_ids` treats fork-only types as
  *unavailable* (not fatal) when the loaded libggml doesn't know them.
- `schemes.py`: add `ROCMFP3` (3.5 bpw), `ROCMFP4` (4.5), `ROCMFP6` (6.5),
  `ROCMFP8` (8.25) — new `rocmfpx` category, noise factors slotted between
  K-quant neighbors, upgrade/downgrade neighbors within the family.
- Search-space gating: rocmfpx schemes join the candidate pool **only** when
  explicitly enabled AND the bound libggml supports them. Enable via
  orchestrator flag surfaced as Foundry `MagicQuantConfig.rocmfpx_schemes`
  (CLI `--magicquant-rocmfpx`) — the entry points `MAGICQUANT_LIBGGML_DIR` at
  the ROCmFPX build when the flag is set.
- MagicQuant's own writer then packs AMD-native hybrid GGUFs directly (no
  translation step). Documented caveat: such GGUFs load only on the ROCmFPX
  fork, not stock llama.cpp.

## Part 3 — measured-search flag (Foundry)

`MagicQuantConfig.measured: bool = False` (+ `measurement_rounds: int = 3`),
CLI `--magicquant-measured`, UI checkbox. Entry dispatches to
`run_measured_search` instead of `run_full_search`. When `rocmfpx_schemes` is
on, the entry prefers the ROCmFPX build as the llama.cpp path so perplexity
measurement can load rocmfpx-typed candidates.

## Validation

1. Both repos' unit suites pass; new tests for translation/emission (Foundry)
   and scheme registration/binding gating + encode-length checks against the
   fork's libggml (MagicQuant).
2. Real run: regenerate `search_results.json` for the existing AgentWorld-35B
   output (prediction-only search, seconds), then produce one real
   `...-ROCMFPX-MQ-Q4.gguf` via the new mode.

## Follow-on (same session, after the above)

Comprehensive MagicQuant capability audit (multi-agent) → ranked improvement
proposals → implement the high-value, clearly-safe ones; report the rest.
