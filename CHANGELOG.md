# Changelog

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
