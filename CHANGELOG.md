# Changelog

## [0.3.0] - 2026-06-09 — Audit Corrections (CLI/UI consolidation, resume markers, secure-by-default UI)

### Changed (behavior)
- **CLI/UI consolidation (H1):** `core/pipeline.py` stage functions now build their
  subprocess scripts via the shared `core/services.py` Service classes — one source
  of truth per stage. CLI and UI now generate equivalent training scripts for the
  same config (proven by `tests/test_script_equivalence.py`). This changes the
  CLI's produced adapters (manual norm-upcast kbit instead of
  `prepare_model_for_kbit_training`, `use_rslora`, unified warmup).
- **Warmup unified (M-warmup):** both CLI and UI use `warmup_ratio` (default 0.05);
  the UI no longer silently forces `warmup_steps=10`. `warmup_steps` remains an
  optional override.
- **Completion-marker resume (M-skip-marker):** stages skip on a
  `_stage_complete.json` marker that matches the config hash AND a present,
  non-empty key artifact — replacing existence-based skips that false-passed on
  partially written outputs. `--force` re-runs anyway.
- **UI secure by default (H3/M-rce):** binds `127.0.0.1`; a non-loopback bind
  (`FOUNDRY_UI_HOST=0.0.0.0`) is refused unless `FOUNDRY_API_KEY` is set.
  `FOUNDRY_REQUIRE_AUTH=1` fails closed even on loopback. API-key comparisons use
  `hmac.compare_digest` (L-timing-compare).
- **HF token scoped to upload (L-hf-token-scope):** the token is only injected into
  the upload subprocess by default (opt in for all stages with
  `FOUNDRY_HF_TOKEN_ALL_STAGES=1`).
- Reconciled CLI training defaults with the UI (max_seq_length 4096, optim
  `paged_adamw_8bit`).

### Fixed
- **Broken `foundry` entrypoint (M-entrypoint):** `core.pipeline:main` now exists.
- **Config loader (L-config-fragmentation):** `--config configs/default.yaml` is no
  longer a no-op — the loader accepts flat and nested YAML and populates all
  sections.
- **REAP arch list (L-reap-archlist):** repo-id strings replaced with `*ForCausalLM`
  class names (adds `GptOssForCausalLM`); shared once via `core/reap_common.py`.
- `tests/test_pipeline.sh` no longer hardcodes a dead `/server/programming/pipeline`
  path (derives from the script dir).
- Dockerfile healthcheck now hits `/health` (was the now-authenticated `/api/state`).
- Simplified the dead Heretic selection loop to `sorted_trials[0]` (L-heretic-deadloop).

### Added
- `core/markers.py` (completion markers), `core/preflight.py` (GPU-memory preflight,
  M-gpu-preflight), `core/reap_common.py` (shared REAP arch list / stub block /
  configurable `FOUNDRY_REAP_SRC` path / source-priority resolver), `core/log.py`
  (shared `_default_log`).
- CLI flags: `--force`, `--stage-timeout`, `--skip-preflight`. `_run` now supports a
  per-stage timeout and kills wedged subprocess groups (L-cli-timeout).
- Pinned llama.cpp auto-install ref (L-supply-chain); transformers/accelerate
  version guard for the fast_load hack (L-fast-load-hack); `POST /api/config`
  validated against a `UIConfig` (extra='forbid', L-config-post).
- Offline pytest suite under `tests/` (no GPU/network): script equivalence, UI
  security, skip markers, config load, preflight, REAP arch, source resolution,
  token scope, run timeout, version guard, stage cleanup.

### Removed
- `core/logging_config.py` (dead structlog module) and the `structlog` dependency;
  `detect_response_template` (dead) and its test callers; the `datagen` optional
  extras (no tracked source); stale `pipeline.egg-info`.

### Notes
- Out of scope for this code-corrections pass (need a real GPU / multi-GB model /
  long compute): the pre-upload quality-gate stage (M-quality-gate) and the
  FLM/Q4NX pipeline stage. The ONNX/Quark items (M-onnx-skip) live only on the
  gitignored `feat/quark-onnx-stage` worktree and are folded in when that branch
  lands.

## [0.2.0] - 2026-04-03 — API Key Authentication

### Added
- **API key authentication** for the FastAPI UI via `FOUNDRY_API_KEY` environment variable
  - Bearer token auth on all REST endpoints (Authorization header)
  - Token query parameter auth on the WebSocket endpoint (`/ws?token=...`)
  - `/health` endpoint exempt from auth (returns `auth_enabled` flag)
  - Backward compatible: when `FOUNDRY_API_KEY` is not set, everything works without auth
- `api_key` field added to `FoundrySettings` in `core/config.py`
- Frontend `authFetch()` wrapper that injects the Bearer token into all API calls
- Frontend auth flow: checks `/health` on load, prompts for the key if auth is enabled, stores key in localStorage
- Auth status button in the header to change or clear the stored API key

## [0.1.0] - 2026-04-03 — Production Hardening

### Bug Fixes
- **CRITICAL**: Fixed `NameError` in `hf_upload.py:dry_run()` — referenced `files_to_upload` instead of `file_tuples`
- Removed hardcoded module-level constants from `fast_train_zeroclaw.py` (MODEL_ID, DATASET_PATH, OUTPUT_DIR, etc.)
- Removed hardcoded module-level constants from `fast_export.py` (MODEL_ID, LORA_DIR, MERGED_DIR)
- Fixed hardcoded `VENV_PYTHON` path in `ui/app.py` — now uses runtime detection

### Added
- `core/config.py` — Pydantic-settings `FoundrySettings` for configuration via environment variables (`FOUNDRY_` prefix) and `.env` files
- `core/logging_config.py` — Structured logging via structlog with WebSocket callback support
- `core/services.py` — Service layer classes (TrainingService, ExportService, MagicQuantService, UploadService) extracted from FastAPI route handlers
- `core/__version__.py` — Package version tracking
- `pyproject.toml` — Full package definition with all runtime dependencies, dev deps, and CLI entrypoints
- Tenacity retry wrappers for HuggingFace API calls in `hf_upload.py`
- Dockerfile with multi-stage build, non-root user, ROCm env vars, health check
- `docker-compose.yml` with named volumes for model output and HF cache
- Makefile with install, test, lint, format, build, docker-build, docker-up, clean targets
- README.md with quickstart, configuration reference, architecture overview, and Docker instructions
- `FOUNDRY_MAP.md` — Detailed architecture documentation
- `OPEN_QUESTIONS.md` — Items requiring manual resolution

### Changed
- FastAPI route handlers in `ui/app.py` now delegate to service layer instead of containing inline business logic
- Device selection uses runtime detection instead of hardcoded `cuda:0`
- Updated `.gitignore` with comprehensive patterns for model artifacts, env files, and build artifacts

### Removed
- Deleted `unsloth_compiled_cache/` directory (unreferenced after Unsloth migration)

### Documentation
- `AUDIT_REPORT.md` — Full code audit findings
- `CHANGELOG.md` — This file
