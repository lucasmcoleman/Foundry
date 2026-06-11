# Foundry

ML fine-tuning and hybrid quantization pipeline for AMD ROCm. Provides a 7-stage workflow — QLoRA training, LoRA merge (export), Heretic abliteration, REAP MoE expert pruning, QAT-LoRA (quantization-aware training), MagicQuant hybrid quantization, and HuggingFace Hub upload — with a FastAPI web UI for orchestration. Heretic, REAP, and QAT are optional stages.

Designed for AMD APU unified memory systems (Strix Halo, gfx1151) where CPU and GPU share system RAM via GTT. Uses custom streaming loaders that process models shard-by-shard to avoid the memory bottlenecks of standard HuggingFace `from_pretrained()`.

See [FOUNDRY_MAP.md](FOUNDRY_MAP.md) for detailed architecture documentation.

## Prerequisites

- Python >= 3.10
- AMD ROCm or NVIDIA CUDA
- GPU with sufficient memory (16+ GB for 9B models, 30+ GB for 40B models)
- HuggingFace account and token (for upload stage)

## Quickstart

### Install

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

### Configure

Create a `.env` file or set environment variables:

```bash
# Required for upload
HF_TOKEN=hf_...

# Optional overrides
FOUNDRY_MODEL_NAME=Tesslate/OmniCoder-9B
FOUNDRY_DATASET_PATH=data/zeroclaw_training_data.jsonl
FOUNDRY_OUTPUT_DIR=./output
```

### Run the full pipeline (CLI)

```bash
python core/pipeline.py \
    --model "org/model-name" \
    --dataset data/training_data.jsonl \
    --upload-to "user/repo-name" \
    --output-dir ./output
```

### Run individual stages

```bash
# Training only
python core/pipeline.py --model "org/model" --dataset data/training.jsonl --no-export --no-magicquant

# Export only (merge LoRA adapters)
python core/fast_export.py

# QAT-LoRA (quant-aware fine-tune; off by default). Needs a prior search's
# search_results.json (auto-detected in <output>/magicquant/, or pass
# --qat-config-source) plus a chat JSONL dataset:
python core/pipeline.py --model "org/model" --qat \
    --qat-dataset data/qat_training.jsonl --qat-tier Q4 \
    --no-export --no-magicquant

# MagicQuant + Upload
python scripts/run_magicquant_upload.py

# Dry-run upload (validate credentials)
python core/pipeline.py --upload-to "user/repo" --dry-run
```

### Run the Web UI

```bash
source activate.sh  # Sets up env vars and starts UI on port 7865 (loopback)
# Or manually (defaults to 127.0.0.1):
python ui/app.py
# or
uvicorn ui.app:app --host 127.0.0.1 --port 7865
```

The UI binds **127.0.0.1 by default**. Running the pipeline executes
operator-controlled subprocesses, so it grants shell-equivalent access to the
host. To expose the UI on a LAN you must set an API key AND opt in to a
non-loopback bind:

```bash
export FOUNDRY_API_KEY="$(openssl rand -hex 24)"
export FOUNDRY_UI_HOST=0.0.0.0   # refused without a key
python ui/app.py
```

Auth (shipped in 0.2.0): all REST endpoints require `Authorization: Bearer <key>`
and the WebSocket requires `?token=<key>` when `FOUNDRY_API_KEY` is set;
`/health` and `/` stay unauthenticated. Set `FOUNDRY_REQUIRE_AUTH=1` to fail
closed (require a key even on loopback).

### Docker

```bash
# Build
docker compose build

# Run
docker compose up -d

# Access UI at http://localhost:7865
```

The container binds `0.0.0.0` internally (the container is the boundary). The
published port is reachable on the host's LAN with no auth by default — set
`FOUNDRY_API_KEY` in the compose environment and/or access-control the published
port before exposing it. The container healthcheck hits the unauthenticated
`/health` endpoint so it stays green when a key is set.

## Configuration Reference

All settings can be configured via environment variables with `FOUNDRY_` prefix or a `.env` file.

| Variable | Default | Description |
|----------|---------|-------------|
| `FOUNDRY_MODEL_NAME` | `Tesslate/OmniCoder-9B` | HuggingFace model ID or local path |
| `FOUNDRY_DATASET_PATH` | `data/zeroclaw_training_data.jsonl` | Training data (JSONL) |
| `FOUNDRY_OUTPUT_DIR` | `./output` | Output directory for all artifacts |
| `FOUNDRY_MAX_SEQ_LENGTH` | `8192` | Maximum sequence length |
| `FOUNDRY_LORA_R` | `32` | LoRA rank |
| `FOUNDRY_LORA_ALPHA` | `64` | LoRA alpha |
| `FOUNDRY_NUM_TRAIN_EPOCHS` | `3` | Training epochs |
| `FOUNDRY_LEARNING_RATE` | `2e-4` | Learning rate |
| `FOUNDRY_PER_DEVICE_TRAIN_BATCH_SIZE` | `2` | Batch size per GPU |
| `FOUNDRY_GRADIENT_ACCUMULATION_STEPS` | `4` | Gradient accumulation steps |
| `FOUNDRY_TARGET_BASE_QUANT` | `MXFP4_MOE` | MagicQuant base quantization |
| `FOUNDRY_MQ_GENERATIONS` | `50` | Evolutionary search generations |
| `FOUNDRY_MQ_TIERS` | `["Q4","Q5","Q6"]` | Quantization tiers to generate |
| `FOUNDRY_HF_REPO_ID` | (empty) | HuggingFace repo for upload |
| `FOUNDRY_HF_PRIVATE` | `true` | Create private HF repo |
| `FOUNDRY_UI_PORT` | `7865` | Web UI port |
| `FOUNDRY_DEVICE` | `cuda:0` | GPU device |
| `HF_TOKEN` | (env/file) | HuggingFace token |

## Pipeline Stages

The pipeline has seven stages; Heretic (3), REAP (4), and QAT (5) are optional
and off by default. Enable them with `--heretic` / `--reap` / `--qat` on the CLI
or via the UI.

### 1. Training
Custom fast QLoRA with shard-by-shard BnB 4-bit quantized loading. Uses completion-only loss masking (only assistant turns contribute to loss). Peak ~30 GB for 40B models.

### 2. Export
Streaming shard-by-shard LoRA merge. Loads one base model shard, applies LoRA deltas on GPU, writes merged shard. Peak ~6 GB.

### 3. Heretic (optional)
Safety-alignment removal (abliteration) via Optuna-optimized directional ablation. Off by default; enable with `--heretic`.

### 4. REAP (optional, MoE only)
Router-weighted Expert Activation Pruning — removes a fraction of experts per MoE layer. Only runs on supported MoE architectures; otherwise it is skipped. Enable with `--reap`.

### 5. QAT-LoRA (optional)
Quantization-Aware Training. Freezes the base model, fake-quantizes it to MagicQuant's per-group hybrid config in the forward pass, and trains LoRA adapters that compensate (completion-only loss). The per-group config is read from a prior MagicQuant search's `search_results.json` (`--qat-config-source`, or auto-detected at `<output>/magicquant/search_results.json`); `--qat-tier` selects the tier to make the adapters robust to. Off by default; enable with `--qat --qat-dataset <chat.jsonl>`, or via the **QAT** card in the web UI. Runs `magicquant.qat.run_qat` (requires the MagicQuant `[qat]` extra) and writes adapters to `<output>/qat_adapters/`. Largest benefit at the aggressive tiers (Q2/Q3/MXFP4).

Validated (confound-controlled, Qwen2.5-0.5B base, aggressive Q4_K-attention/MXFP4-FFN hybrid): bf16 PPL 16.35, plain quant 19.54 (+3.19 damage), quant+QAT 15.13, bf16+identical-LoRA control 13.46. Holding the LoRA's domain adaptation fixed, the quant-vs-bf16 gap shrank +3.19 → +1.67 — **QAT recovered 47.5% of the quantization loss beyond plain LoRA domain adaptation**. The final GGUF pack is exact-ggml (byte-identical to llama.cpp); training uses a faithful torch fake-quant. Full methodology + caveats: MagicQuant's `docs/qat.md`.

### 6. MagicQuant
Evolutionary per-tensor hybrid quantization. Generates tiered GGUF files (Q4/Q5/Q6) with different size-quality tradeoffs. See [MagicQuant](../MagicQuant/) for details.

### 7. Upload
Uploads artifacts to HuggingFace Hub with auto-generated model card, progress reporting, and dry-run validation.

### Resume / re-run
Each stage writes a `_stage_complete.json` marker on success. A stage is skipped only when the marker matches the current config **and** its key artifact is present and non-empty (no more false-skips from partially written outputs). Pass `--force` to re-run regardless, and `--stage-timeout SECONDS` to bound a wedged stage.

## Dataset Format

Standard HuggingFace chat template JSONL:
```json
{"messages": [{"role": "system", "content": "..."}, {"role": "user", "content": "..."}, {"role": "assistant", "content": "..."}]}
```

## AMD APU Notes

- `HSA_ENABLE_SDMA=0`: Required to prevent DMA engine hangs on unified memory
- `PYTORCH_HIP_ALLOC_CONF=backend:native,expandable_segments:True`: Optimal memory allocation
- BitsAndBytes `blocksize=128` is required on AMD (not the NVIDIA default of 64)
- Unload LM Studio models before training — they consume GPU memory from the same shared pool

## Known Limitations

- Custom fast loaders require models in safetensors format (most HuggingFace models)
- The UI defaults to loopback; running the pipeline grants shell-equivalent host access. Set `FOUNDRY_API_KEY` before exposing it on a network (a non-loopback bind is refused without a key).
- Integration tests require GPU access and download multi-GB models

## License

MIT
