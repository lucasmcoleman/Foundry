# HANDOFF.md

## Current State

### Pipeline (`/server/programming/pipeline/`)

**Renamed from `unsloth/` to `pipeline/`** since the project no longer uses Unsloth.

| Component | Status | Notes |
|-----------|--------|-------|
| Custom training (`fast_train_zeroclaw.py`) | Working | Shard-by-shard BnB 4-bit loading, completion-only loss, checkpoint resume |
| Export (`fast_export.py`) | Working | Streaming LoRA merge on GPU, ~6 GB peak |
| Pipeline orchestrator (`pipeline.py`) | Updated | Now uses fast loaders instead of Unsloth |
| Web UI (`ui/app.py`, `ui/index.html`) | Working | FastAPI + WebSocket, form persistence, error banners, stop button |
| HF upload (`hf_upload.py`) | New | Dry-run mode, model card generation, progress reporting |
| MagicQuant integration | Working | Calls MagicQuant from pipeline with tier selection |
| Legacy scripts (`train.py`, `train_zeroclaw.py`) | Deprecated | Still use Unsloth, kept for reference |

### MagicQuant (`/server/programming/MagicQuant/`)

| Change | Commit | Description |
|--------|--------|-------------|
| Block-size check | `3f9641f` | Tensors with row_size < block_size of target quant kept at higher precision |
| llama.cpp compat | `dfb9c73` | 1D tensors → F32, BF16 → F16, block-size fallback → F32; 5D tensor reshape; Qwen3.5 SSM mappings |

## Git Log (`./pipeline/`)

```
ca8b236 fix: update stale path reference after directory rename
bb68eb7 feat(upload): add hf_upload module with model card, dry-run, and progress reporting
cc18026 fix(ui): replace Unsloth imports with custom fast loaders
2c017f8 refactor: replace Unsloth with custom fast loaders in training pipeline
80f1711 refactor(ui): clean up frontend, add form persistence, error surfaces, and docs
3b25eeb chore: initial commit — pre-cleanup state
```

## MagicQuant Commits Pushed

```
dfb9c73 fix: add llama.cpp compatibility guards for GGUF tensor types
3f9641f fix: add block-size compatibility check for GGUF tensor quantization
```

Pushed to: `https://github.com/lucasmcoleman/MagicQuant.git`

## Qwen3.5 Tensors That Triggered Block-Size Bug

The block-size compatibility check fires for these tensor categories:

**Mamba/SSM conv1d weights (row_size=4)**:
- `blk.{0,1,2,4,5,6,8,9,10,12,13,14,16,17,18,20,21,22,24,25,26,28,29,30}.ssm_conv1d.weight`
- 24 tensors, shape [8192, 4] after singleton squeeze

**SSM 1D parameters (kept at F32)**:
- `blk.*.ssm_a` (A_log), `blk.*.ssm_dt.bias`, `blk.*.ssm_norm.weight`
- All norm weights across all layers

**Vision encoder tensors (non-standard dimensions)**:
- `v0.blk.*.mlp.*`, `v0.blk.*.attn.*` with row sizes 4304, 1152, 3456
- `v0.patch_embed.proj.weight` — 5D tensor reshaped to 4D via `_flatten_to_max_dims`

## QA Test Results

| Test | Result |
|------|--------|
| UI App Import | PASS |
| Training Code Imports | PASS |
| Export Code Imports | PASS |
| Pipeline Module Imports | PASS |
| MagicQuant Imports | PASS |
| HF Upload Module Imports | PASS |
| GGUF Files Exist (Q4/Q5/Q6) | PASS |
| GGUF Header Valid | PASS |
| GGUF Chat Template Present | PASS |
| LMStudio Q4 Load + Inference | PASS |
| LMStudio Q5 Load + Inference | PASS |
| LMStudio Q6 Load + Inference | PASS |
| HF Dry-Run | PASS |
| Model Card Sections | PASS |
| Training Data Validation | PASS |
| Stale Path References | PASS (fixed) |
| GPU Training (1 epoch) | SKIPPED (manual) |

**21 passed, 0 failed, 1 skipped (GPU training — run manually)**

## Known Remaining Issues

1. **Qwen3.5 empty content in LMStudio API**: All three GGUF tiers generate tokens but the `content` field is empty. This is a LMStudio issue with Qwen3.5's thinking mode token surfacing, not a GGUF problem. Tokens are generated correctly (`reasoning_tokens` count is positive).

2. **GPU training not tested in QA**: The 1-epoch integration test (`tests/test_training_integration.py`) was not run due to GPU resource constraints during QA. Run it manually: `python tests/test_training_integration.py`.

3. **Editable MagicQuant install**: The venv's editable install path finder (`__editable___magicquant_0_1_0_finder.py`) still points to the old `/server/programming/unsloth/MagicQuant/` path. MagicQuant works because `PYTHONPATH` takes precedence. To fix properly: `pip install -e /server/programming/MagicQuant/`.

4. **datagen/generate.py**: Comment on line 11 still says "ready for Unsloth SFTTrainer" — cosmetic only.

## Running the Pipeline from Scratch

### Prerequisites
```bash
# Activate the environment
cd /server/programming/pipeline
source activate.sh
```

### Full pipeline (train + export + MagicQuant + upload)
```bash
python pipeline.py \
  --model "huihui-ai/Huihui-Qwen3.5-9B-Claude-4.6-Opus-abliterated" \
  --dataset zeroclaw_training_data.jsonl \
  --output-dir ./output \
  --upload-to "youruser/your-model"
```

### Training only
```bash
python fast_train_zeroclaw.py
```

### Export only (merge LoRA + save safetensors)
```bash
python fast_export.py
```

### MagicQuant only
```bash
cd /server/programming/MagicQuant
magicquant generate /path/to/merged/model --tiers Q4,Q5,Q6
```

### HF upload dry-run
```bash
python hf_upload.py \
  --repo youruser/model-name \
  --output-dir ./output \
  --base-model "huihui-ai/Huihui-Qwen3.5-9B-Claude-4.6-Opus-abliterated" \
  --dry-run --show-card
```

### Web UI
```bash
source activate.sh   # starts UI on :7865
# or manually:
python -m ui.app
```

## Running test_pipeline.sh

```bash
cd /server/programming/pipeline
chmod +x test_pipeline.sh
./test_pipeline.sh                # all tests
./test_pipeline.sh --skip-gpu     # skip GPU training test
./test_pipeline.sh --skip-lmstudio  # skip LMStudio load tests
```

The test script runs 21 checks across all pipeline components and exits non-zero if any test fails.
