# Pipeline Audit Report

**Date:** 2026-04-03
**Scope:** Full codebase audit of the ML fine-tuning pipeline at `/server/programming/pipeline/`
**Remote:** https://github.com/lucasmcoleman/pipeline.git (master, 3 commits ahead, 11 modified + 3 untracked uncommitted)

---

## Table of Contents

1. [Critical Symlinks](#1-critical-symlinks)
2. [Confirmed Bugs](#2-confirmed-bugs)
3. [Code Quality](#3-code-quality)
4. [Fragility and Reliability](#4-fragility-and-reliability)
5. [Architecture](#5-architecture)
6. [Packaging](#6-packaging)
7. [Security](#7-security)
8. [Summary](#8-summary)

---

## 1. Critical Symlinks

The pipeline relies on a web of symlinks into an external venv and the MagicQuant sibling project. If any target is moved, deleted, or its internal layout changes, the pipeline breaks silently or with confusing import errors.

| # | Symlink | Target | Severity |
|---|---------|--------|----------|
| S1 | `pipeline/.venv/lib/python3.12/site-packages` | `/server/programming/unsloth-env/lib/python3.12/site-packages` | **CRITICAL** |
| S2 | `pipeline/.venv/bin/uvicorn` | `/server/programming/unsloth-env/bin/uvicorn` | **CRITICAL** |
| S3 | `pipeline/.venv/bin/pip` | `/server/programming/unsloth-env/bin/pip` | **CRITICAL** |
| S4 | `pipeline/.venv/bin/python3.12` | `/usr/bin/python3.12` | MEDIUM |
| S5 | `pipeline/.venv/bin/python` | `python3.12` (relative, within .venv/bin/) | MEDIUM |
| S6 | `pipeline/.venv/bin/python3` | `python3.12` (relative, within .venv/bin/) | MEDIUM |
| S7 | `pipeline/.venv/lib64` | `lib` (relative, within .venv/) | LOW |
| S8 | `pipeline/MagicQuant/magicquant` | `/server/programming/MagicQuant/magicquant` | **CRITICAL** |

**Impact of S1:** Every Python import in the entire pipeline resolves through this single symlink. If unsloth-env is rebuilt, moved, or its Python version changes, every `import torch`, `import transformers`, etc. fails.

**Impact of S2:** The web UI launcher (`ui/run.sh` line 9-13, 23) hardcodes `$VENV/uvicorn` and will fail if this symlink breaks.

**Impact of S3:** `pip install` inside the pipeline venv actually modifies unsloth-env, which may have unintended side effects on other projects sharing that environment.

**Impact of S8:** MagicQuant stage in both `core/pipeline.py` (line 498) and `ui/app.py` (line 632) imports `magicquant.orchestrator` through this symlink. The UI also runs `git pull` on the resolved real path of this symlink (line 571).

---

## 2. Confirmed Bugs

### BUG-1: `hf_upload.py:433` -- `files_to_upload` vs `file_tuples` (REPORTED AS KNOWN, NOW FIXED)

**Severity:** N/A (already fixed)

The code at line 433 now correctly reads `for local_path, repo_path in file_tuples:`. The variable `file_tuples` is defined at line 421. This bug appears to have been resolved since the audit was requested. The `files_to_upload` name is only used in the `upload()` function (line 528), where it is correctly defined.

### BUG-2: `fast_train_zeroclaw.py:41-57` -- Hardcoded module-level constants

**Severity:** MEDIUM

**File:** `/server/programming/pipeline/core/fast_train_zeroclaw.py`

Lines 41-57 define module-level constants that are inappropriate defaults:

```python
MODEL_ID = "DavidAU/Qwen3.5-40B-Claude-4.6-Opus-Deckard-Heretic-Uncensored-Thinking"  # line 41
DATASET_PATH = "data/zeroclaw_training_data.jsonl"  # line 42
OUTPUT_DIR = "./output"  # line 43
LORA_R = 32       # line 46
LORA_ALPHA = 64   # line 47
LORA_DROPOUT = 0.05  # line 48
TARGET_MODULES = [...]  # line 49
NUM_EPOCHS = 3     # line 52
BATCH_SIZE = 1     # line 53
GRAD_ACCUM = 8     # line 54
LEARNING_RATE = 2e-4  # line 55
MAX_SEQ_LENGTH = 4096  # line 56
```

These are only used by `main()` (line 331-435), not by the library function `fast_load_quantized_model()` which accepts `model_id` as a parameter. However, `MODEL_ID` is also the default argument for `fast_load_quantized_model()` (line 61), meaning calling it without arguments silently loads a specific 40B model.

### BUG-3: `fast_export.py:29-31` -- Same hardcoded module-level constants

**Severity:** MEDIUM

**File:** `/server/programming/pipeline/core/fast_export.py`

```python
MODEL_ID = "DavidAU/Qwen3.5-40B-Claude-4.6-Opus-Deckard-Heretic-Uncensored-Thinking"  # line 29
LORA_DIR = "./output-zeroclaw-qwen40b/lora_adapters"  # line 30
MERGED_DIR = "./output-zeroclaw-qwen40b/merged_model"  # line 31
```

These are used as default arguments for `streaming_merge()` (line 107-108), meaning calling it without arguments operates on a specific previous training run's output.

### BUG-4: DEVICE hardcoded to `cuda:0` in 4 locations

**Severity:** HIGH

On a multi-GPU system or if the ROCm device numbering changes, this silently selects the wrong device. On an APU-only system it works, but the code is not portable.

| File | Line | Code |
|------|------|------|
| `core/fast_train_zeroclaw.py` | 58 | `DEVICE = torch.device("cuda:0")` |
| `core/fast_export.py` | 36 | `DEVICE = torch.device("cuda:0") if torch.cuda.is_available() else torch.device("cpu")` |
| `core/pipeline.py` | 323 | `DEVICE = torch.device("cuda:0")` (inside generated training script) |
| `ui/app.py` | 337 | `DEVICE = torch.device("cuda:0")` (inside generated training script) |

Note: `fast_export.py` at least has a CPU fallback; the others do not.

### BUG-5: `ui/app.py:29-31` -- VENV_PYTHON path hardcoded

**Severity:** MEDIUM

**File:** `/server/programming/pipeline/ui/app.py`

```python
VENV_PYTHON = str(PIPELINE_DIR / ".venv" / "bin" / "python")  # line 29
if not Path(VENV_PYTHON).exists():
    VENV_PYTHON = "/server/programming/pipeline/.venv/bin/python"  # line 31
```

The fallback at line 31 is an absolute path to this specific machine. If the pipeline is deployed elsewhere, the fallback is wrong and the primary path depends on the symlink chain (S4 -> S5 -> `/usr/bin/python3.12`).

---

## 3. Code Quality

### CQ-1: Mutable default argument

**Severity:** LOW

**File:** `/server/programming/pipeline/core/pipeline.py`, line 116

```python
def _run(cmd: list[str], log: LogFn, env_extra: dict = None, cwd: str = None) -> int:
```

`env_extra: dict = None` is acceptable (None is immutable), but the parameter lacks `Optional[dict]` annotation. The function itself is safe because it calls `env.update(env_extra)` on a copy, but the signature is misleading.

### CQ-2: No `logging` module usage anywhere

**Severity:** HIGH

**Files:** All files under `core/`, `ui/`, `scripts/`, `datagen/`, `gardener/`

The entire codebase uses `print()` for output and a custom `LogFn` callback type (`Callable[[str, str], None]`) defined independently in both `core/pipeline.py:108` and `core/hf_upload.py:18`. There is zero usage of Python's `logging` module. This means:
- No log levels, no filtering, no file handlers
- No structured logging for production use
- Pipeline subprocess output goes only to stdout/WebSocket; if WebSocket disconnects, logs are lost (except to the `.log` file written by `ui/app.py:153-154`)

### CQ-3: Duplicate `LogFn` type alias

**Severity:** LOW

**Files:**
- `core/pipeline.py:108`: `LogFn = Callable[[str, str], None]`
- `core/hf_upload.py:18`: `LogFn = Callable[[str, str], None]`

Same type alias defined in two files. Should be in a shared `core/types.py` or `core/__init__.py`.

### CQ-4: Duplicate `_default_log` function

**Severity:** LOW

**Files:**
- `core/pipeline.py:111-113`
- `core/hf_upload.py:21-23`

Identical default log functions defined in both modules.

### CQ-5: Functions exceeding 60 lines

**Severity:** MEDIUM

| Function | File | Lines | Length |
|----------|------|-------|--------|
| `fast_load_quantized_model()` | `core/fast_train_zeroclaw.py` | 61-265 | 204 lines |
| `stage_training()` | `core/pipeline.py` | 290-402 | 112 lines |
| `generate_model_card()` | `core/hf_upload.py` | 105-315 | 210 lines |
| `dry_run()` | `core/hf_upload.py` | 339-465 | 126 lines |
| `upload()` | `core/hf_upload.py` | 470-612 | 142 lines |
| `streaming_merge()` | `core/fast_export.py` | 107-236 | 129 lines |
| `do_training()` | `ui/app.py` | 280-411 | 131 lines |
| `do_export()` | `ui/app.py` | 414-543 | 129 lines |
| `do_magicquant()` | `ui/app.py` | 546-708 | 162 lines |
| `main()` | `core/fast_train_zeroclaw.py` | 331-435 | 104 lines |
| `patch_gguf()` | `scripts/patch_gguf_metadata.py` | 72-163 | 91 lines |
| `stage_export()` | `core/pipeline.py` | 411-469 | 58 lines |

The stage functions in `ui/app.py` are especially long because they embed multi-line Python scripts as f-strings.

### CQ-6: os.path.join used instead of pathlib

**Severity:** LOW

**File:** `core/fast_export.py` -- lines 46, 51, 52, 138, 139, 163, 170, 174, 183
**File:** `core/fast_train_zeroclaw.py` -- lines 91, 92, 160, 430

These files mix `os.path.join()` with `pathlib.Path` in the same functions. `pipeline.py` and `hf_upload.py` use pathlib consistently.

### CQ-7: Magic numbers

**Severity:** LOW

| File | Line | Value | Purpose |
|------|------|-------|---------|
| `core/fast_train_zeroclaw.py` | 125 | `128` | BnB blocksize for AMD GPUs |
| `core/pipeline.py` | 226 | `10` | Minimum dataset examples threshold |
| `core/pipeline.py` | 273 | `1` | Git clone depth |
| `ui/app.py` | 163 | `1024 * 1024` | subprocess line buffer size |

The BnB blocksize of 128 (line 125) is the most dangerous: it is AMD-specific and the comment explains why, but it should be a named constant.

### CQ-8: Missing type annotations on cross-module functions

**Severity:** LOW

| Function | File | Missing |
|----------|------|---------|
| `fast_load_quantized_model()` | `core/fast_train_zeroclaw.py:61` | Return type not annotated (returns `tuple[Model, Tokenizer]`) |
| `detect_response_template()` | `core/fast_train_zeroclaw.py:268` | Return type not annotated (returns `str`) |
| `find_latest_checkpoint()` | `core/fast_train_zeroclaw.py:311` | Return type not annotated (returns `Optional[str]`) |
| `streaming_merge()` | `core/fast_export.py:107` | Return type not annotated (returns `None`) |
| `load_lora_weights()` | `core/fast_export.py:39` | Return type not annotated |
| `build_lora_map()` | `core/fast_export.py:69` | Return type not annotated |
| `main()` functions | Multiple files | All missing return type annotations |

### CQ-9: Dead code -- legacy/ directory

**Severity:** LOW

**Files:**
- `legacy/train.py` (156+ lines) -- Unsloth-based training script
- `legacy/train_zeroclaw.py` (51+ lines) -- Unsloth-based ZeroClaw training

Both files import `unsloth` which is not used by the current pipeline. They are marked with `LEGACY` docstrings and kept for reference, which is reasonable, but they are never imported or called by any active code.

### CQ-10: Inline Python script generation via f-strings

**Severity:** HIGH

**Files:**
- `core/pipeline.py:305-386` (stage_training generates ~80 lines of Python as an f-string)
- `core/pipeline.py:436-452` (stage_export generates ~15 lines)
- `ui/app.py:302-404` (do_training generates ~100 lines)
- `ui/app.py:480-537` (do_export generates ~55 lines)
- `ui/app.py:597-702` (do_magicquant generates ~105 lines)
- `ui/app.py:732-764` (do_upload generates ~30 lines)

Each pipeline stage writes a Python script to disk as a string, then executes it in a subprocess. This pattern:
- Defeats IDE support (no syntax checking, no refactoring)
- Makes debugging difficult (tracebacks reference temporary `_stage_*.py` files)
- Duplicates logic between `pipeline.py` and `ui/app.py`
- Is fragile with respect to quoting and escaping (f-string interpolation of user-provided paths)

### CQ-11: Broad except blocks

**Severity:** MEDIUM

Multiple locations catch `Exception` without specific handling:

| File | Line | Context |
|------|------|---------|
| `core/pipeline.py` | 536 | `except Exception as e:` in stage_magicquant -- catches everything, logs traceback |
| `core/hf_upload.py` | 381 | `except Exception as e:` -- token validation |
| `core/hf_upload.py` | 398 | `except Exception:` -- repo access check (no variable, exception swallowed) |
| `core/hf_upload.py` | 509 | `except Exception as e:` -- auth failure |
| `core/hf_upload.py` | 523 | `except Exception as e:` -- repo creation |
| `core/hf_upload.py` | 570 | `except Exception as e:` -- dataset upload |
| `core/hf_upload.py` | 588 | `except Exception as e:` -- model card upload |
| `core/hf_upload.py` | 607 | `except Exception as e:` -- file upload |
| `ui/app.py` | 61 | `except Exception:` -- WebSocket send (exception swallowed) |
| `ui/app.py` | 193 | `except Exception:` -- subprocess reading (exception triggers kill) |
| `ui/app.py` | 883 | `except Exception as e:` -- pipeline runner |
| `ui/app.py` | 990 | `except (WebSocketDisconnect, ..., Exception):` -- WebSocket handler |

The `hf_upload.py:398` case is the worst: it catches any exception during repo access check and swallows it entirely with no logging of the error itself, only assuming the repo does not exist.

---

## 4. Fragility and Reliability

### FR-1: No timeout on subprocess.Popen in pipeline.py

**Severity:** HIGH

**File:** `core/pipeline.py:127-136`

```python
proc = subprocess.Popen(
    cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    env=env, cwd=cwd, text=True, bufsize=1,
)
for line in proc.stdout:
    ...
proc.wait()
```

`proc.wait()` has no timeout. If a training run hangs (e.g., GPU lockup, deadlock in ROCm), the pipeline blocks forever. The `ui/app.py` version (`run_script`, line 157) uses `asyncio.create_subprocess_exec` which also has no timeout, but at least has a stop mechanism via `state.running` and `os.killpg()`.

### FR-2: No timeout on subprocess.Popen in ensure_llamacpp

**Severity:** MEDIUM

**File:** `core/pipeline.py:259-274`

The `_run()` calls for `git clone` and `cmake` have no timeout. A hung `git clone` (network issues) will block indefinitely. The `ui/app.py` version (lines 614-622) uses bare `subprocess.run()` also without timeout, except for the git pull at line 574 which has `timeout=30`.

### FR-3: No retry logic for HuggingFace API calls

**Severity:** MEDIUM

**File:** `core/hf_upload.py:376-384, 394-418, 505-511, 515-525, 560-572, 596-609`

Every HF API call (`api.whoami()`, `api.repo_info()`, `api.create_repo()`, `api.upload_file()`) is called once with no retry. Transient network errors or HF rate limits cause immediate failure.

### FR-4: No retry logic for model downloads

**Severity:** MEDIUM

**Files:**
- `core/fast_train_zeroclaw.py:85` -- `snapshot_download(model_id)` with no retry
- `core/fast_export.py:134` -- `snapshot_download(model_id)` with no retry

These download multi-GB models. A transient network error loses all progress.

### FR-5: Silent corruption possible in LoRA merge

**Severity:** MEDIUM

**File:** `core/fast_export.py:191-208`

The merge loop iterates over tensor names but does not verify that all LoRA targets were actually applied. If a LoRA weight key does not match any base model key (e.g., due to a model architecture mismatch), the merge silently skips it. The `merged_count` is printed at the end (line 235) but not validated against `len(lora_map)`.

### FR-6: No validation of merged model output

**Severity:** MEDIUM

**File:** `core/fast_export.py:226-235`

After merging, the code writes the safetensors index and prints stats, but does not:
- Verify the total number of tensors matches the original
- Check that all shards are complete (no partial writes despite the atomic rename at line 215)
- Verify the merged model can load successfully

### FR-7: GGUF patch script may corrupt files

**Severity:** HIGH

**File:** `scripts/patch_gguf_metadata.py:141-163`

The comment at lines 141-158 acknowledges uncertainty about GGUF tensor data offsets:
```python
# The tensor info section uses absolute offsets from the start of the file.
# Since we added KV data, the tensor data offsets need adjustment.
# BUT: GGUF tensor data offset is relative to the END of the header...
```

The code writes `rest_data` (tensor info + padding + tensor data) verbatim after the expanded KV section. If the tensor info contains absolute offsets (which it does in some GGUF versions), the resulting file has corrupted offsets. The code then replaces the original with `os.replace()` (line 162), destroying the only copy.

### FR-8: Hardcoded paths in scripts/

**Severity:** MEDIUM

| File | Line | Hardcoded Value |
|------|------|-----------------|
| `scripts/run_magicquant_upload.py` | 13 | `MODEL_ID = "DavidAU/Qwen3.5-40B-..."` |
| `scripts/run_magicquant_upload.py` | 14 | `OUTPUT_DIR = "./output-zeroclaw-qwen40b"` |
| `scripts/run_magicquant_upload.py` | 15 | `HF_REPO = "lmcoleman/Qwen3.5-40B-..."` |
| `scripts/patch_gguf_metadata.py` | 169 | `'DavidAU/Qwen3.5-40B-...'` (tokenizer source) |
| `scripts/patch_gguf_metadata.py` | 173 | `"/server/ai/models/lmcoleman/..."` (absolute path) |
| `scripts/patch_gguf_metadata.py` | 192 | `"/server/programming/pipeline/output-zeroclaw-qwen40b/magicquant"` |
| `scripts/upload_dataset.py` | 21 | `repo_id = "lmcoleman/zeroclaw-tool-use-training"` |

These scripts are single-purpose convenience wrappers for a specific model run. They are not reusable without editing.

### FR-9: File I/O without error handling

**Severity:** LOW

| File | Line | Issue |
|------|------|-------|
| `core/fast_train_zeroclaw.py` | 94 | `open(idx_path)` -- no try/except for corrupted JSON |
| `core/fast_export.py` | 47 | `open(config_path)` -- no try/except |
| `core/fast_export.py` | 143 | `open(idx_path)` -- no try/except |
| `core/pipeline.py` | 183 | `open(path)` -- dataset validation does handle JSONDecodeError |
| `core/pipeline.py` | 684 | `open(args.config)` -- no try/except for missing/corrupted YAML |

---

## 5. Architecture

### AR-1: Full stage implementations duplicated in pipeline.py and ui/app.py

**Severity:** HIGH

**Files:**
- `core/pipeline.py` lines 290-402 (`stage_training`) vs `ui/app.py` lines 280-411 (`do_training`)
- `core/pipeline.py` lines 411-469 (`stage_export`) vs `ui/app.py` lines 414-543 (`do_export`)
- `core/pipeline.py` lines 474-540 (`stage_magicquant`) vs `ui/app.py` lines 546-708 (`do_magicquant`)
- `core/pipeline.py` lines 545-580 (`stage_upload`) vs `ui/app.py` lines 711-770 (`do_upload`)

Both modules generate Python scripts as strings and execute them in subprocesses. The scripts are similar but not identical -- they have diverged:
- `ui/app.py` `do_training` uses manual `prepare_model_for_kbit_training` replacement (lines 340-348) while `pipeline.py` uses the standard `prepare_model_for_kbit_training` (line 327)
- `ui/app.py` `do_training` supports `use_rslora` (line 355) while `pipeline.py` does not
- `ui/app.py` `do_training` uses `warmup_steps` (line 387) while `pipeline.py` uses `warmup_ratio` (line 359)
- `ui/app.py` `do_export` has smarter routing with source model detection (lines 443-476) while `pipeline.py` only reads from adapter_config.json

This means bug fixes in one file do not propagate to the other.

### AR-2: Dataset validation duplicated

**Severity:** MEDIUM

**Files:**
- `core/pipeline.py:164-242` (`validate_dataset` -- synchronous)
- `ui/app.py:213-269` (`validate_dataset` -- async)

Same logic implemented twice, with minor differences (pipeline.py has warning messages for missing roles; app.py does not).

### AR-3: Business logic in FastAPI route handlers

**Severity:** MEDIUM

**File:** `ui/app.py`

The `do_training()`, `do_export()`, `do_magicquant()`, and `do_upload()` functions contain all pipeline business logic (script generation, subprocess management, artifact checking). These should be in a service layer that both `pipeline.py` and `app.py` call, rather than being duplicated.

### AR-4: Global mutable state in ui/app.py

**Severity:** MEDIUM

**File:** `ui/app.py:79`

```python
state = PipelineState()
```

A single module-level `PipelineState` instance holds all runtime state including WebSocket client list. This:
- Prevents running multiple pipelines concurrently
- Makes testing difficult (state leaks between tests)
- Has no thread safety (though FastAPI's async model makes this less dangerous)

### AR-5: Config not validated at startup

**Severity:** MEDIUM

**File:** `ui/app.py:892-906`

The persistent config (`config.json`) is read/written as an untyped `dict`. No validation is performed. The `POST /api/config` endpoint (line 933-939) accepts arbitrary JSON and merges it in:

```python
@app.post("/api/config")
async def set_config(body: dict):
    cfg = load_config()
    cfg.update(body)
    save_config(cfg)
```

### AR-6: Missing graceful shutdown

**Severity:** MEDIUM

**File:** `ui/app.py:960-973`

The stop endpoint sends SIGTERM to the process group (line 968), but:
- Does not wait for the process to actually terminate
- Does not clean up temporary `_stage_*.py` files
- Does not clean up partial output (e.g., half-merged safetensors shards)
- The `state.running = False` flag may race with `run_pipeline()` checking it

### AR-7: No progress reporting for long operations in pipeline.py

**Severity:** LOW

**File:** `core/pipeline.py:127-136`

The `_run()` helper streams stdout but has no concept of progress percentage. The `ui/app.py` version parses tqdm output (lines 178-184) to extract progress; the CLI pipeline.py does not.

### AR-8: YAML config loading is incomplete

**Severity:** LOW

**File:** `core/pipeline.py:682-688`

The `--config` YAML loader only handles the `training` section:
```python
if "training" in data:
    for k, v in data["training"].items():
        setattr(cfg.training, k, v)
```

It ignores `export`, `magicquant`, and `upload` sections. Also, `import yaml` is inside the `if` block (line 683) -- if yaml is not installed, the error only appears when `--config` is used, not at import time.

### AR-9: Circular import risk

**Severity:** LOW

**File:** `core/pipeline.py:551, 588`

`from hf_upload import HFUploadConfig, upload` is a lazy import inside `stage_upload()` and `stage_upload_dry_run()`. This works because `hf_upload.py` does not import `pipeline.py`. However, if hf_upload ever needed pipeline types, the lazy import would mask the circular dependency.

### AR-10: Generated scripts use sys.path.insert(0, ...) for imports

**Severity:** MEDIUM

**Files:**
- `core/pipeline.py:320` -- `sys.path.insert(0, str(Path("{Path.cwd()}") / "core"))`
- `ui/app.py:330, 516, 632` -- `sys.path.insert(0, ...)`

Every generated script manipulates sys.path at runtime. This is fragile and depends on the CWD at execution time. If `Path.cwd()` changes between script generation and execution, imports fail.

---

## 6. Packaging

### PK-1: No pyproject.toml

**Severity:** HIGH

**File:** (missing)

The pipeline has no `pyproject.toml`, `setup.py`, or `setup.cfg`. The only `pyproject.toml` in the tree belongs to the MagicQuant subproject (`MagicQuant/pyproject.toml`). This means:
- No declarative dependency list
- No installable package (`pip install -e .` fails)
- No version metadata
- No CLI entrypoint registration

### PK-2: No `__version__`

**Severity:** LOW

No version identifier exists anywhere in the codebase. `core/__init__.py` contains only a comment (line 1).

### PK-3: No CLI entrypoint

**Severity:** LOW

The pipeline can only be run via `python core/pipeline.py` or `source activate.sh`. There is no registered console_script entrypoint.

### PK-4: All dependencies satisfied only by symlinks into unsloth-env

**Severity:** HIGH

There is no `requirements.txt`, no `pyproject.toml`, and no dependency pinning. The implicit dependency list (inferred from imports) includes at minimum:
- `torch` (ROCm build)
- `transformers`
- `peft`
- `trl`
- `datasets`
- `bitsandbytes` (0.49.2+, ROCm-compatible)
- `safetensors`
- `huggingface_hub`
- `accelerate`
- `fastapi`
- `uvicorn`
- `pydantic`
- `psutil`
- `PyYAML` (optional, for `--config`)

All of these resolve through symlink S1. If unsloth-env is updated (e.g., `pip install --upgrade transformers`), the pipeline may break with no warning and no way to pin versions.

---

## 7. Security

### SEC-1: HF_TOKEN sourced safely from environment

**Severity:** N/A (positive finding)

`core/hf_upload.py:9` explicitly notes "Token is sourced from HF_TOKEN env var -- never hardcoded." The `activate.sh` script (line 26-28) reads it from `~/.cache/huggingface/token` only if `HF_TOKEN` is not already set. No tokens appear in source code.

### SEC-2: No input sanitization in f-string script generation

**Severity:** MEDIUM

**Files:** `core/pipeline.py:305-386`, `ui/app.py:302-404`

User-provided values (model names, paths) are interpolated directly into generated Python scripts via f-strings. While the scripts are executed locally (not remotely), a malicious model name like `"; import os; os.system("rm -rf /"); "` would be escaped by `repr()` in most locations (e.g., `ui/app.py:310, 338`). However, some interpolations are bare (e.g., `pipeline.py:320` uses `Path("{Path.cwd()}")` which would break on paths containing `"`).

### SEC-3: `POST /api/config` accepts arbitrary JSON

**Severity:** LOW

**File:** `ui/app.py:933-939`

The config endpoint accepts any JSON body and persists it to `config.json`. While this is a local-only UI, it could be used to inject unexpected keys that affect behavior if `load_config()` return values are used unsanitized elsewhere.

---

## 8. Summary

### Issue Count by Severity

| Severity | Count |
|----------|-------|
| **CRITICAL** | 4 (all symlink-related: S1, S2, S3, S8) |
| **HIGH** | 6 (BUG-4, CQ-2, CQ-10, FR-1, FR-7, AR-1, PK-1, PK-4) |
| **MEDIUM** | 16 |
| **LOW** | 12 |

### Top 5 Recommended Actions (by impact)

1. **Eliminate the unsloth-env symlink dependency.** Create a proper `pyproject.toml` with pinned dependencies and install into the pipeline's own venv. This resolves CRITICAL issues S1-S3 and HIGH issue PK-4.

2. **Unify stage implementations.** Extract the script-generation logic into shared functions in `core/` that both `pipeline.py` and `ui/app.py` call. This resolves HIGH issue AR-1 and MEDIUM issue AR-2, and reduces the maintenance burden of CQ-10.

3. **Add timeouts to all subprocess calls.** The `_run()` helper in `pipeline.py` and `run_script()` in `app.py` should accept a timeout parameter. This resolves HIGH issue FR-1.

4. **Replace hardcoded `cuda:0` with configurable device.** Add a `device` field to `TrainingConfig` and `ExportConfig`, defaulting to `"cuda:0"` but overridable. This resolves HIGH issue BUG-4.

5. **Adopt Python `logging` module.** Replace the custom `LogFn` callbacks and `print()` calls with structured logging. The WebSocket log streaming in `app.py` can be implemented as a logging handler. This resolves HIGH issue CQ-2.

### Files Audited

| File | Lines | Issues Found |
|------|-------|-------------|
| `core/pipeline.py` | 721 | 12 |
| `core/fast_train_zeroclaw.py` | 439 | 7 |
| `core/fast_export.py` | 244 | 7 |
| `core/hf_upload.py` | 686 | 8 |
| `ui/app.py` | 998 | 14 |
| `ui/run.sh` | 23 | 1 |
| `ui/index.html` | 812 | 0 |
| `activate.sh` | 51 | 1 |
| `tests/test_training_integration.py` | 338 | 0 |
| `scripts/run_magicquant_upload.py` | 50 | 3 |
| `scripts/patch_gguf_metadata.py` | 209 | 3 |
| `scripts/upload_dataset.py` | 119 | 1 |
| `datagen/generate.py` | 212 | 0 |
| `legacy/train.py` | 156+ | 1 (dead code) |
| `legacy/train_zeroclaw.py` | 51+ | 1 (dead code) |
| `core/__init__.py` | 1 | 0 |

---

*Generated 2026-04-03 by audit agent.*
