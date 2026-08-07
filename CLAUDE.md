# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

Foundry: LLM fine-tuning and hybrid quantization pipeline for AMD ROCm (Strix Halo APU, gfx1151). Three core components:
- **Custom fast QLoRA training** with shard-by-shard BnB quantization and completion-only loss masking (replaces Unsloth)
- **MagicQuant** evolutionary per-tensor hybrid quantization
- **ROCmFPX** AMD-native uniform-quant GGUFs (ROCmFP3/4/6/8, straight + agent presets) — see `docs/rocmfpx.md`
- **FastAPI Web UI** for pipeline orchestration

## Directory Structure

```
core/                     # Main library modules
  pipeline.py             # Orchestrator: training → export → heretic → reap → qat → magicquant → rocmfpx → upload
  fast_train_zeroclaw.py  # Shard-by-shard BnB 4-bit quantized loading + QLoRA training
  fast_export.py          # Streaming LoRA merge at ~6 GB peak
  hf_upload.py            # HuggingFace Hub upload with model card generation
  services.py             # Shared per-stage builders: build_config(JSON) + thin shim (one source of truth for CLI + UI)
  _train_entry.py         # Importable training stage body (run(cfg.json)) — shim target
  _export_entry.py        # Importable export (streaming LoRA merge) stage body
  _heretic_entry.py       # Importable heretic abliteration stage body (Optuna search)
  _reap_entry.py          # Importable REAP expert-pruning stage body
  _qat_entry.py           # Importable QAT-LoRA stage body (run(cfg) -> magicquant.qat.run_qat)
  _magicquant_entry.py    # Importable MagicQuant evolutionary-quant stage body
  _rocmfpx_entry.py       # Importable ROCmFPX stage body (build/convert/quantize; see docs/rocmfpx.md)
  _upload_entry.py        # Importable HF-upload stage body
  dataset_format.py       # Normalize messages / {text} / {prompt,completion} / alpaca → one chat schema
  markers.py              # _stage_complete.json completion markers (resume/skip)
  preflight.py            # GPU-memory preflight checks
  reap_common.py          # Shared REAP arch list / stub block / source-priority resolver
  log.py                  # Shared print/WebSocket-callback logger
configs/                  # YAML training configs
data/                     # Training data (JSONL)
scripts/                  # Convenience scripts (run_magicquant_upload.py, patch_gguf_metadata.py)
legacy/                   # Deprecated Unsloth-based scripts (train.py, train_zeroclaw.py)
MagicQuant/               # Evolutionary per-tensor hybrid quantization (subproject)
ui/                       # FastAPI + WebSocket live log streaming UI
tests/                    # Offline pytest suite + GPU integration test (gpu/slow markers)
```

> NOTE: `datagen/` and `gardener/` were removed 2026-07-11 (the leftover
> `__pycache__`/logs directories were deleted; their tracked source had already
> been migrated out in April 2026 — see `git log -- datagen/ gardener/`,
> commit `f6ed85f`). Their optional `datagen` extras were removed from
> `pyproject.toml`. Restore from git history if you need them.

## Environment Setup

```bash
source activate.sh   # Activates venv, sets ROCm env vars + PYTHONPATH, starts UI on :7865
```

Required ROCm environment variables (set by activate.sh):
```bash
HSA_ENABLE_SDMA=0
PYTORCH_HIP_ALLOC_CONF="backend:native,expandable_segments:True"
UNSLOTH_SKIP_TORCHVISION_CHECK=1
TORCH_ROCM_AOTRITON_ENABLE_EXPERIMENTAL=1
HF_HUB_ENABLE_HF_TRANSFER=1
```

## Running the Pipeline

```bash
# Full pipeline: train → export → magicquant → upload
python core/pipeline.py --model "org/model" --dataset data/training.jsonl --upload-to "user/repo" --output-dir ./output

# MagicQuant + upload only (skip training, use existing merged model)
python scripts/run_magicquant_upload.py

# Patch chat template into GGUFs
python scripts/patch_gguf_metadata.py
```

## AMD APU / Unified Memory Constraints

This system runs on a Strix Halo APU where GPU and CPU share 124 GB of system RAM. Key implications:

- **Unsloth/transformers model loading is extremely slow** on this hardware. The default `from_pretrained` path loads tensors one at a time through Python's GIL. For 40B+ models this takes hours.
- **Foundry uses custom fast loaders instead of Unsloth**: `core/fast_train_zeroclaw.py` loads safetensors shard-by-shard with inline BnB 4-bit quantization (~2 min vs hours). `core/fast_export.py` does streaming LoRA merge at ~6 GB peak memory. Both `core/pipeline.py` stage_training() and stage_export() call these fast loaders.
- **BitsAndBytes 0.49.2 works on ROCm** — GPU quantization kernels are functional (0.011s/tensor).
- **BnB requires blocksize=128** on AMD (not the NVIDIA default of 64).
- **LM Studio models consume GPU memory from the same pool** — unload them before training.
- **GPU offload does NOT speed up perplexity measurement.** Measured twice: MoE
  (same file, controlled) CPU 3.81 s/pass vs GPU 3.90; dense 27B baseline+KL
  pass CPU 1061 s vs `-ngl 999` 1097 s. Unified memory means both engines read
  the same DRAM at the same bandwidth, and the sweep is bandwidth-bound, so
  offload changes which engine waits, not how long. **Never interrupt a running
  measurement to "enable the GPU"** — the upside is zero and the cost is hours.
  `MAGICQUANT_NGL` is only worth setting for reasons other than throughput.

## Architecture

### Pipeline Stages (core/pipeline.py)
1. **Training**: Custom fast QLoRA with completion-only loss (masks system/user turns)
2. **Export**: Streaming shard-by-shard LoRA merge to safetensors (~6 GB peak)
3. **Heretic** (optional, off by default): Optuna-optimized directional ablation
4. **REAP** (optional, MoE only): router-weighted expert pruning
5. **QAT** (optional, off by default): quantization-aware LoRA. Freezes the base,
   fake-quantizes it to MagicQuant's per-group hybrid config (read from a prior
   search's `search_results.json` via `config_source`+`tier`, or auto-detected at
   `<output>/magicquant/search_results.json`) in the forward, and trains LoRA
   adapters that compensate. Delegates to `magicquant.qat.run_qat` (needs the
   MagicQuant `[qat]` extra). Enable with `--qat --qat-dataset <chat.jsonl>` or
   the UI QAT card. Writes adapters to `<output>/qat_adapters/`. Validated
   (confound-controlled, Qwen2.5-0.5B base, aggressive hybrid — effectively
   MXFP4-attn/MXFP4-FFN: 896-wide rows can't take Q4_K, the writer's block-32
   fallback applies): **live-mode** QAT recovered **47.5% of the quantization
   loss beyond plain LoRA domain adaptation** (bf16-vs-quant PPL gap
   +3.19 → +1.67 vs a bf16+identical-LoRA control). Recovery scales with quant
   aggressiveness; the final GGUF pack is exact-ggml. **Mode matters**: frozen
   mode (the only feasible mode for fused 3-D MoE experts at scale) measured
   ~55% of live's recovery in a controlled 4-arm rerun (+13.0% vs +21.8%), and
   a frozen run's *raw* out-of-domain PPL delta can be negative even when its
   controlled recovery is positive — that signature explains the Qwen3.6-35B-A3B
   frozen-QAT −6.1% and is why that build shipped pre-QAT. Judge frozen runs
   only with a control arm or an in-domain eval. See MagicQuant's `docs/qat.md`
   + `docs/experiments/qat-frozen-mode-2026-08.md`.
6. **MagicQuant**: Evolutionary search → 3-tier hybrid GGUFs (Q4/Q5/Q6).
   **A tier name is a SIZE BAND, not a recipe** — see "Tier semantics" below.
   Prediction-only by default; `--magicquant-measured` runs the real-perplexity
   Predict→Measure→Learn loop. **`use_imatrix` now defaults to TRUE**: the
   bundled calibration corpus is ~1 MB across 18 languages plus code, math and
   agentic prompts, verified disjoint from the perplexity eval corpus, with
   capture bounded to 200 chunks (~35-40 min once per model, then cached).
   `enable_imatrix` refuses outright if the calibration and eval corpora
   resolve to the same file — calibrating on the text a run is scored against
   makes every measured loss optimistic with nothing in the output showing it. `--magicquant-rocmfpx` lets the search also
   explore AMD-native ROCmFPX fork types per group (needs a ROCmFPX build;
   output loads only on the fork). Persists `search_results.json` from both
   search paths (consumed by QAT and by ROCmFPX's mq-hybrid mode).
   **Size-target mode** (`--magicquant-budget-gib <N>`, UI: "Size Target (GiB)")
   runs MagicQuant v2's budget-constrained search instead of the tier ladder:
   an exact per-tensor knapsack under a hard byte ceiling, verified against
   real perplexity. It produces ONE file named for the size it was asked for
   (`<Model>-BUDGET-<N>GiB.gguf`) rather than a band label, and is mutually
   exclusive with `--magicquant-measured` (v2 verifies for itself; accepting
   both would misrepresent what ran). The run merges a `BUDGET-<N>GiB`
   pseudo-tier into `search_results.json` carrying BOTH a per-group projection
   (so QAT and mq-hybrid consume it unchanged) and the exact per-tensor map.
   Budget builds are exempt from BAND and DOMINANCE — they claim a size, not a
   band, and have no siblings — but must land within `BUDGET_TOLERANCE` (2%)
   of the request, one constant shared by the build-time and publish-time
   guards so they can never disagree.
   GGUF-only releases (no safetensors/BF16 published) can serve as the source
   via `--magicquant-source-model <file.gguf> --magicquant-dequant-source`
   (UI: MagicQuant card "Quantized GGUF source" toggle) — the output is then
   **double-quantized**: quality is bounded by the source quant's error floor,
   so a Q4 tier from a Q8_0 source ≈ Q4-from-BF16, while Q5/Q6 tiers are
   largely pointless. Disclose double-quantization on the model card. ROCmFPX
   from a quantized source likewise needs `--rocmfpx-allow-requantize`.
7. **ROCmFPX** (optional, off by default): AMD-native quant GGUFs via
   [ciru-ai/ROCmFPX](https://github.com/ciru-ai/ROCmFPX) (a llama.cpp fork,
   git-cloned + compiled — not a pip package). Produces ROCmFP3/4/6/8 GGUFs
   (straight + tool-calling/JSON-safe "agent" presets), targeting this box's
   Strix Halo (gfx1151) hardware specifically. Two modes: uniform presets
   (`rocmfp4-agent` …) and **MagicQuant-hybrid** (`mq-q4`/`mq-q5`/`mq-q6`) —
   the latter reproduces a MagicQuant tier's per-group precision layout in
   ROCmFPX-family types via `llama-quantize --tensor-type-file`, i.e. a
   ROCm-optimized version of a MagicQuant-optimized quant. Enable with
   `--rocmfpx` or the UI ROCmFPX card. Writes GGUFs to `<output>/rocmfpx/`.
   Experimental upstream research build — see `docs/rocmfpx.md`.
8. **Upload**: HuggingFace Hub with model card generation

## Tier Semantics (read before touching quantization)

**A tier name is a SIZE BAND, never a claim about which schemes are inside.**
A "Q5" is whatever mix of schemes landed in the Q5 size band with the lowest
measured perplexity loss — it may contain zero Q5_K tensors. Bands come from
`magicquant.quant.tiers.classify_tier` / `TIER_BOUNDARIES` as a ratio to the
BF16 baseline.

Never grade a build by whether it "contains N-bit tensors". Size and measured
quality are the only criteria. Four published models once shipped a uniform
Q6_K labelled "Q5" because the v1 Q5 band ran to ratio 0.45 and a genuine Q6
fell inside it; `tools/reselect_tiers.py` re-derives any finished run's ladder
from its stored measurements to catch exactly this.

## Publishing Criteria

Applied by `core/publish_criteria.py` (decision logic) via the publish stage.
A built tier ships only if it passes all three:

1. **BAND** — the file must land in the band its name claims, checked by size
   ratio, never by inspecting which schemes it contains.
2. **DOMINANCE** — dropped if a *smaller* shipped tier beats it on measured
   loss by more than `NOISE_MARGIN` (0.001 relative loss). Sub-floor gaps are
   coin flips, so both tiers ship and a QUESTION is raised instead: FableFusion's
   Q6 beat its Q5 by 0.000185, which is noise, and an any-margin rule would
   have deleted a good tier.
3. **SPEED** (ROCmFPX only) — ROCmFPX trades quality for throughput, so a
   ROCmFPX tier ships only if measurably faster than the MagicQuant tier of the
   same band.

Separately, the ROCmFPX **band guard** refuses to *build* a tier whose render
would land outside its claimed band. The fork has no type between 4.5 and 6.5
bpw, so schemes round to the nearest available and a tier can render out of
band (FableFusion's mq-q5 predicted into Q6; ThinkingCap's mq-q4 into Q5).
A refusal writes a structured record to `<output>/rocmfpx/_refusals.json`
(`core/_rocmfpx_entry._record_refusal`), which is the publish stage's primary
source for disclosing it; scraping the run log is a fallback for older runs
only, since cleanup deletes logs. A tier that later builds successfully clears
its own record, so a stale refusal can't outlive the condition that caused it.

**Model cards must explain every gap.** Dropped tiers, refused tiers, and files
carried over from an earlier run each get their own disclosed section.
`audit_card_against_repo` cross-checks the card against the repo's real file
list *before* the card is pushed (and again after upload), so a card that
contradicts its own repo is caught while it is still private. A ladder with a
silent gap reads as breakage and generates user questions.

### MagicQuant (MagicQuant/magicquant/)
Classifies tensors into sensitivity groups (E=Embeddings, H=Head, Q=Query, K=Key, O=Output, U=FFN Up, D=FFN Down, X=MoE Experts, R=Router), then runs evolutionary search to find optimal per-group quantization. Supports BF16, Q8_0, Q6_K, Q5_K, Q4_K_M, IQ4_NL, MXFP4, Q3_K, Q2_K, and (opt-in, fork-only) the AMD-native ROCMFP3/4/6/8 schemes.

**GGUF writer** (`gguf/writer.py`): Two-pass streaming — header pass computes sizes/offsets, data pass overlaps I/O with encoding. Has a block-size compatibility check for hybrid architectures: a row width that isn't a multiple of the requested K-quant's 256-block falls back to a block-32 quant (MXFP4 for low-bit targets, Q8_0 for high-bit); SSM/group-`S` operands and rows that aren't even 32-divisible fall back to F32. Each such downgrade is recorded in `writer._fallbacks` and summarized in a one-line warning.

### Fast Loaders (for 40B+ models on unified memory)
- `core/fast_train_zeroclaw.py`: Creates model on meta device, loads safetensors shard-by-shard, replaces nn.Linear with bnb.nn.Linear4bit, quantizes inline per-shard, frees each shard before next. Includes completion-only loss masking and checkpoint resume. Peak ~30 GB for a 40B model.
- `core/fast_export.py`: Streams LoRA merge shard-by-shard on GPU — loads shard, applies LoRA deltas (W + scaling * B @ A) on GPU, saves merged shard, frees. Peak ~6 GB.
- `legacy/train.py`, `legacy/train_zeroclaw.py`: LEGACY scripts that use Unsloth. Kept for reference/NVIDIA use.

### Web UI (ui/)
FastAPI + WebSocket live log streaming. Pydantic config models. Port 7865 (configurable via FOUNDRY_UI_PORT).

## Dataset Format

Standard HF chat template JSONL:
```json
{"messages": [{"role": "system", "content": "..."}, {"role": "user", "content": "..."}, {"role": "assistant", "content": "..."}]}
```

`core/dataset_format.py` also auto-detects and normalizes these alternative
shapes to the same chat structure before training (priority: messages > alpaca >
prompt/completion > text):
- `{"text": "..."}` — used verbatim (no chat template applied)
- `{"prompt": "...", "completion": "..."}` — user/assistant turns
- alpaca `{"instruction": "...", "input": "...", "output": "..."}` — instruction(+input) → user, output → assistant

A source whose rows disagree on shape fails loudly rather than training on a mix.

Training data generators formerly lived in `datagen/` (ZeroClaw tool-calls) and `gardener/` (NC gardening, uses Claude API); both were removed 2026-07-11 (see the NOTE above) — the generators now live at https://github.com/lucasmcoleman/training-data.

## Known Issues

- **Qwen3.5 hybrid architecture** has Mamba (linear_attention) layers with 48-element rows. Quantization types with block_size > 32 are incompatible — since 48 isn't 32-divisible either, the GGUF writer falls back to F32 for these (block-32 quants only apply to 32-divisible rows).
- **GGUF files from MagicQuant need chat template patching** — the source reader pulls from tokenizer_config.json, which must contain `chat_template`. The streaming merge (core/fast_export.py) copies tokenizer files but may omit the template; verify and use `scripts/patch_gguf_metadata.py` if needed.
- **`UNSLOTH_COMPILE_DISABLE=1`** may be needed for gfx1151 if training produces NaN losses (known Triton code generation issue on RDNA).
- **IQ4_NL is gated behind imatrix availability.** Without calibration it lost
  all 11 measured comparisons across two 27B models (3-20x worse than same-bpw
  MXFP4/Q4_K_M) despite *better* isolated weight-reconstruction error — its
  non-linear lookup places levels to minimise unweighted error, which optimises
  the wrong thing. It is now excluded from the search pool when no imatrix is
  present (`IMATRIX_DEPENDENT_SCHEME_NAMES`), and the mutation neighbour walk
  skips *over* it rather than truncating, since it sits mid-chain
  (Q5_K <-> IQ4_NL <-> MXFP4_MOE) and stopping would strand Q5_K.
- **`output/` is gitignored.** Anything durable must live in tracked code, not
  in a per-run scratch directory. Publishing criteria were once kept in
  `output/_publish_tiers.py` and were consequently untested; several model-card
  bugs reached public pages before anyone noticed. They now live in
  `core/publish_criteria.py` with tests.
