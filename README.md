# Foundry

ML fine-tuning and hybrid quantization pipeline for AMD ROCm. Provides a 4-stage workflow — QLoRA training, LoRA merge, MagicQuant hybrid quantization, and HuggingFace Hub upload — with a FastAPI web UI for orchestration.

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

# MagicQuant + Upload
python scripts/run_magicquant_upload.py

# Dry-run upload (validate credentials)
python core/pipeline.py --upload-to "user/repo" --dry-run
```

### Run the Web UI

```bash
source activate.sh  # Sets up env vars and starts UI on port 7865
# Or manually:
uvicorn ui.app:app --host 0.0.0.0 --port 7865
```

### Docker

```bash
# Build
docker compose build

# Run
docker compose up -d

# Access UI at http://localhost:7865
```

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

### 1. Training
Custom fast QLoRA with shard-by-shard BnB 4-bit quantized loading. Uses completion-only loss masking (only assistant turns contribute to loss). Peak ~30 GB for 40B models.

### 2. Export
Streaming shard-by-shard LoRA merge. Loads one base model shard, applies LoRA deltas on GPU, writes merged shard. Peak ~6 GB.

### 3. MagicQuant
Evolutionary per-tensor hybrid quantization. Generates tiered GGUF files (Q4/Q5/Q6) with different size-quality tradeoffs. See [MagicQuant](../MagicQuant/) for details.

### 4. Upload
Uploads artifacts to HuggingFace Hub with auto-generated model card, progress reporting, and dry-run validation.

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
- The UI serves without authentication — use only on trusted networks
- Integration tests require GPU access and download multi-GB models

## License

MIT
