# Pipeline Map

Generated: 2026-04-03 | Production Hardening Pass

## Pipeline Overview

A 4-stage ML fine-tuning and quantization pipeline for AMD ROCm (Strix Halo APU, gfx1151). CPU and GPU share 128 GB system RAM via GTT, which dictates the streaming architecture throughout.

```
Training (QLoRA) → Export (LoRA merge) → MagicQuant (hybrid GGUF) → Upload (HuggingFace)
```

## Stage Map

### Stage 1: Training
- **Files**: `core/fast_train_zeroclaw.py`, `core/pipeline.py:stage_training()`, `ui/app.py:do_training()`
- **What it does**: Shard-by-shard BnB 4-bit quantized model loading on meta device, then QLoRA fine-tuning with completion-only loss masking (only assistant turns contribute to loss)
- **Input**: HuggingFace model ID + JSONL dataset
- **Output**: `{output_dir}/lora_adapters/` (adapter_config.json + adapter_model.safetensors)
- **Invocation**: Subprocess (Python script written to disk and executed)
- **Peak memory**: ~30 GB for 40B model

### Stage 2: Export
- **Files**: `core/fast_export.py`, `core/pipeline.py:stage_export()`, `ui/app.py:do_export()`
- **What it does**: Streaming shard-by-shard LoRA merge: loads one base model shard at a time, applies LoRA deltas (W + scaling * B @ A) on GPU, writes merged shard, frees memory
- **Input**: Base model (HuggingFace or local) + LoRA adapters from Stage 1
- **Output**: `{output_dir}/merged_model/` (standard HF safetensors directory)
- **Invocation**: Subprocess (Python script written to disk and executed)
- **Peak memory**: ~6 GB

### Stage 3: MagicQuant
- **Files**: `MagicQuant/magicquant/orchestrator.py`, `MagicQuant/magicquant/gguf/writer.py`, `core/pipeline.py:stage_magicquant()`, `ui/app.py:do_magicquant()`
- **What it does**: Evolutionary per-tensor-group hybrid quantization. Classifies tensors into sensitivity groups (E/H/Q/K/O/U/D/X/R/S), runs evolutionary search to find optimal quantization per group, generates tiered GGUF files (Q4/Q5/Q6)
- **Input**: Merged safetensors from Stage 2 (or standalone GGUF/safetensors)
- **Output**: `{output_dir}/magicquant/*.gguf` (multiple tier files)
- **Invocation**: Direct import (`from magicquant.orchestrator import MagicQuantOrchestrator`) in `pipeline.py`; subprocess in `ui/app.py`
- **Dependencies**: Optionally llama.cpp for perplexity measurement (auto-installed if missing)

### Stage 4: Upload
- **Files**: `core/hf_upload.py`, `core/pipeline.py:stage_upload()`, `ui/app.py:do_upload()`
- **What it does**: Creates HuggingFace repo, generates model card, uploads GGUF/LoRA/merged files with progress reporting. Supports dry-run validation.
- **Input**: Artifacts from any prior stage + HF_TOKEN env var
- **Output**: HuggingFace Hub repository with model card
- **Invocation**: Subprocess in `ui/app.py`; direct import in `pipeline.py`

## Web UI
- **Files**: `ui/app.py`, `ui/index.html`, `ui/run.sh`
- **Tech**: FastAPI + WebSocket for real-time log streaming
- **Port**: 7865 (configurable via PIPELINE_UI_PORT)
- **State**: In-memory `PipelineState` with WebSocket fan-out
- **Launch**: `source activate.sh` starts it in background

## Inter-Service Communication

| From | To | Method |
|------|----|--------|
| pipeline.py | fast_train_zeroclaw.py | Subprocess (generates Python script, executes with sys.executable) |
| pipeline.py | fast_export.py | Subprocess (generates Python script, executes with sys.executable) |
| pipeline.py | MagicQuant | Direct Python import via symlink chain |
| pipeline.py | hf_upload.py | Direct Python import |
| ui/app.py | All stages | Subprocess (generates Python script, executes with .venv/bin/python) |
| ui/app.py | Browser | WebSocket (JSON messages: log, stage_update, progress) |
| All stages | Next stage | Shared filesystem ({output_dir}/) |

## Dependency: How pipeline/ uses MagicQuant

MagicQuant is available to pipeline/ through two mechanisms:
1. **Installed package**: `magicquant==0.1.0` is installed in `unsloth-env`, which pipeline/.venv symlinks to
2. **Symlink**: `pipeline/MagicQuant/magicquant → /server/programming/MagicQuant/magicquant`

The UI's `do_magicquant()` adds `pipeline/MagicQuant` to `sys.path` before importing. The CLI `pipeline.py` relies on the installed package or PYTHONPATH.

## unsloth-env Symlink Situation

**Status**: CRITICAL FRAGILITY

pipeline/.venv is not a real virtual environment — it is a skeleton of symlinks pointing into unsloth-env:

| Symlink | Target |
|---------|--------|
| `pipeline/.venv/lib/python3.12/site-packages` | `/server/programming/unsloth-env/lib/python3.12/site-packages` |
| `pipeline/.venv/bin/uvicorn` | `/server/programming/unsloth-env/bin/uvicorn` |
| `pipeline/.venv/bin/pip` | `/server/programming/unsloth-env/bin/pip` |
| `pipeline/.venv/bin/python` | `python3.12` (→ `/usr/bin/python3.12`) |
| `pipeline/MagicQuant/magicquant` | `/server/programming/MagicQuant/magicquant` |

### Resolution Plan
1. Identify all packages from unsloth-env that pipeline/ actually imports at runtime
2. Declare them in `pipeline/pyproject.toml` with version ranges
3. Delete `pipeline/.venv` entirely
4. Create a fresh venv with `python -m venv .venv && pip install -e ".[dev]"`
5. Verify pipeline runs from the fresh venv
6. Delete `unsloth-env/` from the workspace
7. The MagicQuant symlink should be replaced by declaring `magicquant` as a dependency (installed from local path or git)

### Key packages from unsloth-env used by pipeline/
Core: `torch`, `transformers`, `peft`, `trl`, `datasets`, `bitsandbytes`, `accelerate`, `safetensors`, `huggingface_hub`
UI: `fastapi`, `uvicorn`, `pydantic`
Quantization: `magicquant` (local), `gguf`
Data generation: `anthropic`, `ebooklib`, `beautifulsoup4`, `lxml`
Utilities: `tqdm`, `psutil`, `pyyaml`, `python-dotenv`

## unsloth_compiled_cache Disposition

**Status**: UNREFERENCED — SAFE TO DELETE

Contents: Compiled Unsloth trainer modules (UnslothSFTTrainer.py, UnslothGRPOTrainer.py, etc.) with Python 3.12 bytecode cache.

References found:
- `pipeline/.gitignore` line 23: `unsloth_compiled_cache/` (gitignored)
- No import or runtime reference in either `pipeline/` or `MagicQuant/`

The pipeline migrated away from Unsloth to custom fast loaders (commits `cc18026`, `2c017f8`). This cache is a remnant.

**Action**: Delete outright.

## Uncommitted Local Changes

### MagicQuant (1 file modified)
- `magicquant/gguf/writer.py`: Improved `general.file_type` mapping. Was hardcoded to `1` for all branches; now uses a lookup table mapping quant schemes to llama_ftype enum values, with fallback to most-common group scheme.

### pipeline (11 files modified, 3 untracked)

**Modified:**
- `activate.sh` — venv activation and UI startup
- `core/fast_export.py` — streaming LoRA merge
- `core/fast_train_zeroclaw.py` — fast model loading + training
- `core/hf_upload.py` — HuggingFace upload module
- `core/pipeline.py` — pipeline orchestrator
- `data/zeroclaw_training_data.jsonl` — training dataset
- `datagen/generate.py` — data generation script
- `tests/test_training_integration.py` — integration tests
- `ui/app.py` — FastAPI backend
- `ui/index.html` — frontend
- `ui/run.sh` — UI launch script

**Untracked:**
- `datagen/generate_bulk.py` — bulk data generation
- `datagen/scenarios_generated.py` — generated scenario definitions
- `scripts/upload_dataset.py` — standalone dataset upload

### pipeline is 3 commits ahead of origin:
```
c832504 feat(gardener): add ebook-to-training-data generator using LMStudio
8544b90 refactor: rename unsloth-env to .venv
e5eb229 refactor: reorganize workspace into logical folder hierarchy
```

## Known Bugs

### MagicQuant: Unconditional tensor quantization dispatch
- **Location**: `magicquant/gguf/writer.py:205-208`
- **Issue**: `encode_to_ggml_bytes(f32, target)` is called without verifying that `f32` is actually float32. The `source.read_tensor_f32()` method nominally returns float32, but there is no dtype guard at the encoding boundary. If a source implementation returns wrong dtype data, the encoder will silently produce corrupt output.
- **Also**: The pre-quantized source validation (lines 385-404) runs in Pass 1 but the actual encoding in Pass 2 (worker thread, line 208) does not re-validate. A race condition or source bug could slip through.

### pipeline: Variable name bug in hf_upload.py dry_run
- **Location**: `core/hf_upload.py:433`
- **Issue**: References `files_to_upload` (the parameter name from `upload()`) instead of `file_tuples` (the local variable in `dry_run()`). This will cause a `NameError` whenever `dry_run()` discovers files to upload.

### pipeline: Hardcoded values in fast_train_zeroclaw.py
- **Location**: `core/fast_train_zeroclaw.py:41-57`
- **Issue**: `MODEL_ID`, `DATASET_PATH`, `OUTPUT_DIR`, all LoRA config, and training config are hardcoded as module-level constants. These are only used by `main()` but pollute the module namespace and could confuse callers who expect the function parameters to be the sole source of config.

### pipeline: DEVICE hardcoded to cuda:0
- **Location**: `core/fast_train_zeroclaw.py:58`, `core/fast_export.py:36`
- **Issue**: `DEVICE = torch.device("cuda:0")` — no runtime detection or fallback.

## Security Concerns

- HF_TOKEN is properly loaded from env var or `~/.cache/huggingface/token` file (activate.sh:26-28). No hardcoded secrets found.
- The UI serves on 0.0.0.0 (all interfaces) without authentication. Network-local only; noted as a deployment concern.
