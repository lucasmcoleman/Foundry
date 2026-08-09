# Changelog

## [Unreleased]

### Changed (behavior)
- **CLI now enforces the system-memory OOM-freeze gate (user-confirmed
  behavior change):** `core/preflight.py`'s `check_system_memory` is the
  PRIMARY GATE against the documented unified-memory OOM-freeze livelock
  (resident model pins GTT → `MemAvailable` collapses → kernel OOM-kills
  bystander processes → box livelock), but it was only ever wired into the
  UI's `_mem_preflight`. The CLI's own `_preflight_stage` only ever checked
  GPU VRAM, advisory-only (logs, never blocks -- callers discard its return
  value). Added a new `_system_memory_gate(stage, log, skip)` -- unlike
  `_preflight_stage`, this one actually blocks -- wired into
  `stage_training`/`stage_export`/`stage_heretic`/`stage_qat`/
  `stage_magicquant`/`stage_rocmfpx` (every heavy stage `_mem_preflight`
  gates on the UI side). `stage_reap` is intentionally excluded, matching
  `preflight.py`'s own documented exception and the UI's `do_reap`. Bypass
  with `--skip-preflight` or `FOUNDRY_SKIP_MEM_PREFLIGHT=1`, same as the UI.
  **This can newly block a CLI run that previously proceeded** when
  `MemAvailable` is genuinely low -- that is the intended fix, confirmed
  before implementing.

### Fixed
- **UI MagicQuant imatrix silently disabled (cleanup):** the web UI's frontend
  default for `use_imatrix` was hardcoded `false` in `ui/index.html`, overriding
  the backend's `true` default (set when imatrix became the default) on every
  UI-driven run, including a fresh browser with no saved state. CLI and
  direct-API runs were unaffected. Frontend default now matches the backend;
  a form-state migration (`LS_VERSION` 7→8) drops any saved `false` from before
  the fix so existing browsers pick up the corrected default too.
- **`_create_repo_with_retry` didn't actually retry (cleanup):** its name and
  docstring promised the same retry-on-transient-failure behavior as its two
  siblings (`_upload_with_retry`, `_whoami_with_retry`), but it was a bare
  passthrough. A transient network blip during repo creation could kill an
  entire upload stage instead of retrying. Now carries the same `@retry`
  decorator as its siblings.
- **`do_heretic`/`do_reap` crashed opaquely on a missing stage config:** both
  stage configs are `Optional[...] = None` on `RunRequest`, reachable via the
  documented headless `/api/run`. `do_qat`/`do_rocmfpx` already guarded this
  with an actionable error; `do_heretic`/`do_reap` instead hit an
  `AttributeError` on first field access, surfacing as an opaque
  `Pipeline error: 'NoneType' object has no attribute ...`. Both now fail with
  the same clear message as their siblings.
- **QAT stage skipped the system-memory preflight gate:** every other heavy UI
  stage (training/export/heretic/magicquant/rocmfpx) calls `_mem_preflight`
  before running; QAT did not, despite `preflight.py` giving it its own
  dedicated 32 GB constant ("frozen base held fake-quantized in host RAM while
  LoRA adapters train against it") -- the same risk category (`MemAvailable`
  collapse -> kernel OOM-killer livelock) the gate exists to catch. `do_qat`
  now calls it like its siblings.

### Removed
- **Dead llama.cpp auto-installer in `core/pipeline.py` (cleanup):** `_find_llamacpp`/
  `ensure_llamacpp` had zero callers -- the real, tested auto-install path is
  `core/_magicquant_entry.py`'s, which every stage that needs llama.cpp actually
  uses and which (unlike the dead copy) correctly prefers a ROCmFPX fork build
  when present. The dead copy had already silently drifted to lack that
  preference. `tests/test_stage_cleanup.py`'s supply-chain-pin tests, which
  targeted the dead copy from before the H2 entry-shim migration, now check
  `_magicquant_entry.py` directly (matching how the ROCmFPX equivalent test
  already worked); the now-inapplicable single-sourcing test was removed since
  there is no longer a second copy to keep in sync.
- **Pre-entry-shim leftovers in `core/services.py` (cleanup):** `ENTRY_MODULES`,
  `_env_preamble()`, and `_hf_cache_check()` predate the H2 entry-shim
  migration and had zero callers -- every service now unconditionally uses
  `_entry_shim()`, and the env-setup/cache-probe logic they generated as
  strings now lives as real Python in the entry modules.

### Changed
- **`hf_cache_probe()` deduplicated (cleanup):** `core/_train_entry.py` and
  `core/_export_entry.py` carried character-for-character identical copies.
  Moved to a new `core/entry_common.py` (stdlib-only, matching each entry
  module's no-heavy-deps-at-import-time constraint) and imported lazily at
  the existing call sites, same pattern already used for `dataset_format`/
  `markers`/`reap_common`.
- **`_ROCM_ENV` deduplicated into `core/entry_common.py` (cleanup):** identical
  4-key dict was copy-pasted across `_train_entry.py`, `_export_entry.py`,
  `_heretic_entry.py`, `_qat_entry.py`. `_reap_entry.py`'s copy is a 3-key
  subset (missing `UNSLOTH_SKIP_TORCHVISION_CHECK`) and was left as its own
  local dict, unchanged, with a comment explaining the divergence -- whether
  that's intentional or a copy-paste gap is a still-open question from the
  cleanup audit; this change only removes the duplication that was actually
  identical.
- **UI `validate_dataset` reimplemented instead of reused (cleanup):**
  `ui/app.py` carried its own independent JSONL pre-flight check that had
  already drifted from `core/pipeline.py`'s (missing the `system`/`assistant`
  role-coverage warnings and the file-size report). Replaced with a thin async
  wrapper delegating to `core.pipeline.validate_dataset`, using the same
  buffer-and-replay pattern `_mem_preflight` already uses to bridge the
  synchronous log callback into the WebSocket log stream. Confirmed no
  relative-path regression: both the old code's explicit `FOUNDRY_ROOT`
  resolution and core's CWD-relative resolution agree, since
  `foundry-ui.service`'s `WorkingDirectory=/server/programming/Foundry`
  guarantees the two are the same directory.
- **Enabled-stage-set computation deduplicated (cleanup):** `run_pipeline()`
  and `main()`'s `--dry-run` branch each rebuilt the same 8-line
  `if config.X is not None: enabled.add("X")` block from `PipelineConfig`
  section presence (the dry-run branch's own comment already said "same
  logic as run_pipeline"). Factored into `_compute_enabled_stages(config)`,
  called from both.
- **`HFUploadConfig` assembly deduplicated (cleanup):** `stage_upload` and
  `stage_upload_dry_run` each independently assembled the same ~50-line
  `HFUploadConfig` from `PipelineConfig` (every field, the missing-repo_id
  guard, the `_resolve_license()` call). Factored into
  `_build_hf_upload_config(config, log, enabled)`, called from both; each now
  only branches on `upload()` vs `dry_run()`.

### Performance
- **`/api/runs` no longer re-globs the log directory per log file (cleanup):**
  the `live` flag computation reran `model_dir.glob("_stage_*.log")` + an
  mtime sort *inside* the loop already iterating that same file list --
  O(N²) directory-listing + stat syscalls for a model dir with N stage logs,
  on an endpoint that walks every run directory under `output/`. Only
  triggered for the currently-active run dir (short-circuited otherwise), but
  real accumulating I/O for a long-running pipeline that grows one log per
  stage attempt. The newest-log lookup is now computed once per model dir,
  above the loop.

### Changed
- **HF-upload test fixtures deduplicated (cleanup):** `_cfg(**overrides)` was
  byte-for-byte identical across `test_hf_upload_budget_card.py`,
  `test_hf_upload_budget_regex_fallback.py`, and `test_card_repo_consistency.py`;
  `_fake_gguf`/`_96_GIB` (a sparse-file helper for a fake 96 GiB GGUF) likewise
  identical between the first two. Moved into `tests/conftest.py` as
  `hf_upload_cfg`/`fake_gguf`/`GIB_96`; all three files now import them
  (aliased to their old local names to keep call sites unchanged). Test-only,
  no production-code risk.
- **Completion-marker resume check deduplicated across all 7 stage runners
  (cleanup):** `do_training`/`do_export`/`do_heretic`/`do_reap`/`do_qat`/
  `do_magicquant`/`do_rocmfpx` each repeated the same ~10-line shape (glob or
  fixed key file, `markers.is_stage_complete`, skip-log, `COMPLETE` state,
  100% progress). Extracted to `_check_marker(stage, display_name, stage_dir,
  cfg_hash, key_glob, default_key_name)`. Each stage's own hash-field dict and
  its post-run `write_marker` logic (which genuinely varies -- some stages
  re-glob after the run and fall back to the pre-run key, `magicquant`/
  `rocmfpx` don't -- so intentionally NOT unified) are untouched. Also
  normalizes the skip-log message to one consistent format across all 7
  (`"{name} already complete (marker matches) at {dir} — skipping"`),
  fixing `do_heretic`'s stray `--` (every other stage already used `—`) and
  dropping the redundant trailing "skipping X" repetition on `export`/
  `heretic`/`reap` -- cosmetic log-text only, no test depended on the old
  wording. Two tests that source-scraped `do_magicquant`'s body via a
  `"existing_ggufs = sorted(mq_dir.glob"` string anchor to check specific
  config keys are hashed (`test_do_magicquant_hash_source_includes_new_knobs`/
  `_speed_knobs` in `test_magicquant_knobs.py`,
  `test_do_magicquant_passes_budget_gib_to_build_script` in
  `test_ui_magicquant_budget.py`) had that anchor removed by the extraction;
  updated to anchor on `"done, mq_key = await _check_marker("` instead.

### Removed
- **`core/config.py` (`FoundrySettings`) deleted (user-confirmed):** ~28
  pydantic-settings fields, of which only `.ui_port` was ever read anywhere
  (`ui/app.py`, itself behind an `os.environ.get("FOUNDRY_UI_PORT", ...)`
  fallback already reading the same env var) -- and two other fields had
  already silently drifted from `pipeline.py`'s real training defaults
  (`max_seq_length` 8192 vs 4096; `optim` `adamw_8bit` vs `paged_adamw_8bit`).
  `ui/app.py` now reads `FOUNDRY_UI_PORT` directly (default `7865`), matching
  how it already reads `FOUNDRY_API_KEY`/`FOUNDRY_REQUIRE_AUTH`. Also drops
  `pydantic-settings` and `python-dotenv` from `pyproject.toml` -- both were
  pulled in solely for this module (`env_file=".env"` support) and had no
  other consumer anywhere in the repo.

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
