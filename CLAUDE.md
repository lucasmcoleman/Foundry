# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

Foundry: LLM fine-tuning and hybrid quantization pipeline for AMD ROCm (Strix Halo APU, gfx1151). Three core components:
- **Custom fast QLoRA training** with shard-by-shard BnB quantization and completion-only loss masking (replaces Unsloth)
- **MagicQuant** evolutionary per-tensor hybrid quantization
- **FastAPI Web UI** for pipeline orchestration

## Directory Structure

```
core/                     # Main library modules
  pipeline.py             # Orchestrator: training → export → magicquant → upload
  fast_train_zeroclaw.py  # Shard-by-shard BnB 4-bit quantized loading + QLoRA training
  fast_export.py          # Streaming LoRA merge at ~6 GB peak
  hf_upload.py            # HuggingFace Hub upload with model card generation
  services.py             # Shared per-stage builders: build_config(JSON) + thin shim (one source of truth for CLI + UI)
  _train_entry.py         # Importable training stage body (run(cfg.json)) — shim target
  _export_entry.py        # Importable export (streaming LoRA merge) stage body
  _heretic_entry.py       # Importable heretic abliteration stage body (Optuna search)
  _reap_entry.py          # Importable REAP expert-pruning stage body
  _magicquant_entry.py    # Importable MagicQuant evolutionary-quant stage body
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

> NOTE: `datagen/` and `gardener/` have no tracked source (only stale
> `__pycache__`/logs on disk); their optional `datagen` extras were removed from
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

## Architecture

### Pipeline Stages (core/pipeline.py)
1. **Training**: Custom fast QLoRA with completion-only loss (masks system/user turns)
2. **Export**: Streaming shard-by-shard LoRA merge to safetensors (~6 GB peak)
3. **MagicQuant**: Evolutionary search → 3-tier hybrid GGUFs (Q4/Q5/Q6)
4. **Upload**: HuggingFace Hub with model card generation

### MagicQuant (MagicQuant/magicquant/)
Classifies tensors into sensitivity groups (E=Embeddings, H=Head, Q=Query, K=Key, O=Output, U=FFN Up, D=FFN Down, X=MoE Experts, R=Router), then runs evolutionary search to find optimal per-group quantization. Supports BF16, Q8_0, Q6_K, Q5_K, IQ4_NL, MXFP4.

**GGUF writer** (`gguf/writer.py`): Two-pass streaming — header pass computes sizes/offsets, data pass overlaps I/O with encoding. Has block-size compatibility check for hybrid architectures (Mamba layers with non-standard dimensions fall back to BF16).

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

Training data generators in `datagen/` (ZeroClaw tool-calls) and `gardener/` (NC gardening, uses Claude API).

## Known Issues

- **Qwen3.5 hybrid architecture** has Mamba (linear_attention) layers with 48-element rows. Quantization types with block_size > 32 are incompatible — the GGUF writer falls back to BF16 for these.
- **GGUF files from MagicQuant need chat template patching** — the source reader pulls from tokenizer_config.json, which must contain `chat_template`. The streaming merge (core/fast_export.py) copies tokenizer files but may omit the template; verify and use `scripts/patch_gguf_metadata.py` if needed.
- **`UNSLOTH_COMPILE_DISABLE=1`** may be needed for gfx1151 if training produces NaN losses (known Triton code generation issue on RDNA).
