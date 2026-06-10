# Trusted Fix-Plan — Foundry

Verified against current code by an independent pass (every audit finding re-confirmed or refuted with file:line evidence). This is the authoritative implementation plan.

## Findings

### [H1-cli-divergence] HIGH — CONFIRMED
**Claim:** CLI (core/pipeline.py) carries a ~1500-line parallel copy of every stage that has diverged from core/services.py (used by the UI); pipeline.py does not import services.py, so the same config yields different adapters via CLI vs UI.

**Evidence:** core/pipeline.py:406-558 (stage_training emits its own script), 567-629 (stage_export), 638-965 (stage_heretic), 1006-1181 (stage_reap), 1186-1258 (stage_magicquant) all build script strings inline; no `import services` anywhere in pipeline.py (grep: zero). Divergences: warmup — pipeline.py:515 `warmup_ratio={tc.warmup_ratio}` (default 0.05, pipeline.py:42) vs services.py:245 `warmup_steps={warmup_steps}` (UI default 10, ui/app.py:127). kbit — pipeline.py:447 `prepare_model_for_kbit_training(...)` vs services.py:122-130 hand-rolled (requires_grad=False; upcast only name-contains-'norm'/'layernorm' to float32; enable_input_require_grads). LoRA — services.py:139 `use_rslora=`, 257-258 packing/completion_only_loss; pipeline.py:448-452 hard-codes bias='none', no rslora/packing. Defaults — max_seq_length 8192 (pipeline.py:31) & optim adamw_8bit (pipeline.py:43) vs 4096 (ui/app.py:116) & paged_adamw_8bit (ui/app.py:128). GPT-OSS prefix only in pipeline.py:783-784.

**Fix:** Consolidate onto services.py. (1) Port pipeline.py-only behavior INTO the Service classes first: add the GPT-OSS `<|channel|>analysis` prefix branch to HereticService.build_script (services.py ~437); keep services' manual norm-upcast kbit approach and delete pipeline.py's prepare_model_for_kbit_training path; add an optional warmup_ratio param to TrainingService.build_script that emits warmup_ratio= when set. (2) Rewrite each pipeline.py stage_* to instantiate the matching Service with FOUNDRY_ROOT and sys.executable, call svc.build_script(...), write to _stage_*.py, run via _run(). Keep CLI dataclasses but map onto Service kwargs. (3) Reconcile TrainingConfig vs TrainingCfg defaults (pick one set deliberately). End state: one shared Pydantic config (see L-config-fragmentation).

**Test:** tests/test_script_equivalence.py (offline): build CLI and UI training scripts with identical config; assert byte-identical (or differ only in a whitelist). Asserts: warmup_ratio XOR warmup_steps consistent across CLI/UI; 'prepare_model_for_kbit_training' not in script; 'use_rslora=' present.

**Risk:** High regression surface: script generation is the core of the program. Changing CLI kbit/warmup will change produced adapters (intended). Long heretic/reap scripts make porting typos latent bugs. Mitigate by landing the equivalence test before refactoring.


### [H2-codegen-as-strings] HIGH — RESOLVED (2026-06-09)
Each stage body was extracted into an importable `core/_<stage>_entry.py`
exposing `run(cfg.json)` (train/export/heretic/reap/magicquant/upload). Each
`Service.build_script` now emits a ~10-line config-driven shim that writes JSON
and calls the entry module (`import _<stage>_entry; _<stage>_entry.run(...)`).
Heavy imports (torch/transformers/optuna/reap) are deferred into `run()` so the
modules import GPU-free; config parsing and the pure helpers
(`dataset_format.normalize_*`, `_heretic_entry.select_best_trial` /
`normalize_response_prefix`, `_reap_entry.build_argv`,
`_magicquant_entry.resolve_source`) are unit-tested in
`tests/test_stage_entries.py` (+ relocated assertions in
`test_script_equivalence.py` / `test_reap_service.py`). Original behavior was
transcribed verbatim (anchor-checked against HEAD services.py).

### [H2-codegen-as-strings] HIGH — CONFIRMED
**Claim:** Stage bodies are Python emitted as escaped f-strings/concatenated strings; no IDE/lint/type support; tracebacks point at anonymous temp files.

**Evidence:** pipeline.py:425-542 (training f-string), 596-612 (export), 661-950 (heretic ~290 lines), 1054-1166 (reap); services.py:101-268 (training), 290-303 (export), 331-575 (heretic ~244 lines), 613-723 (reap), 747-874 (magicquant), 914-951 (upload). Written to _stage_*.py and executed (pipeline.py:544-548, ui/app.py:188-190). The repr()/!r burden exists only because of this pattern.

**Fix:** Follow the module-plus-shim pattern proven by .worktrees/quark-onnx/core/onnx_quark.py (typed build_quark_argv; ~10-line shim script). Extract each stage body into importable core/_<stage>_entry.py exposing def main(cfg_path) reading a JSON config; reduce Service.build_script to a fixed launcher that writes JSON and emits `python -m core._<stage>_entry cfg.json`. Do AFTER H1 so it is done once.

**Test:** tests/test_stage_entries.py: import each entry module, call its config/argv builder with a temp JSON, assert parsed config + constructed SFTConfig/LoraConfig kwargs match expected (mirrors test_onnx_quark.py). No GPU.

**Risk:** Large mechanical refactor; must preserve chat-template fallback, token-length analysis, checkpoint resume, MoE norm handling, and the ROCm env preamble before `import torch` in the entry module. Mitigate with the GPU integration test + config-builder unit tests.


### [H3-ui-bind-auth] HIGH — CONFIRMED
**Claim:** Web UI binds 0.0.0.0 with authentication OFF by default; verify_api_key is a no-op when the key is empty; / and /health are unauthenticated.

**Evidence:** ui/app.py:38 `API_KEY = os.environ.get('FOUNDRY_API_KEY', '')`; 40-45 verify_api_key returns early when not API_KEY; 1202 `uvicorn.run(app, host='0.0.0.0', port=port)`; ui/run.sh:24 `--host 0.0.0.0`; Dockerfile CMD --host 0.0.0.0; README.md:71 documents 0.0.0.0. /health (963-966) and / (969-972) lack Depends(verify_api_key). config.py:71 api_key='' default.

**Fix:** Default to loopback. In ui/app.py __main__ (1199-1202): host=os.environ.get('FOUNDRY_UI_HOST','127.0.0.1'); if host non-loopback AND not API_KEY, sys.exit with a clear message (or fall back to 127.0.0.1 + loud warning); pass host= to uvicorn.run. Mirror in ui/run.sh (--host ${FOUNDRY_UI_HOST:-127.0.0.1}). Add ui_host: str='127.0.0.1' to FoundrySettings. Keep Docker on 0.0.0.0 (container is the boundary) but document published-port access control.

**Test:** tests/test_ui_security.py (FastAPI TestClient): with key set, /api/state -> 401 without Bearer, 200 with; /health and / -> 200 unauthenticated and /health reports auth_enabled=true. Unit-test the host selector: 127.0.0.1 default, raise/warn on 0.0.0.0 without key.

**Risk:** LAN/reverse-proxy deployments break until FOUNDRY_UI_HOST=0.0.0.0 (+ key) is set; document in CHANGELOG. Low code risk.


### [H4-broken-tests] HIGH — CONFIRMED
**Claim:** Primary test suite is broken: tests/test_pipeline.sh hardcodes a dead path; core logic has zero offline coverage.

**Evidence:** tests/test_pipeline.sh:19 `PIPELINE_DIR='/server/programming/pipeline'`, :20 VENV_DIR from it, :77 source activate, :82-83 PYTHONPATH/cd from it. Real repo is /server/programming/Foundry. `make test` runs `pytest tests/ --ignore=tests/test_training_integration.py`, but the only other test module IS test_training_integration.py (GPU + ~5 GB per its docstring), so make test collects zero tests. test_pipeline.sh:128 imports detect_response_template (see L-dead-detect).

**Fix:** tests/test_pipeline.sh:19 -> `PIPELINE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"`. Fix stale unsloth messages (115, 479-481). Port the worktree offline mock pattern (unittest.mock.patch + pytest.importorskip) into master via new pytest modules. Mark test_training_integration.py's heavy path @pytest.mark.slow/gpu and register markers in pyproject.

**Test:** Establish the offline pytest suite (this finding IS the infra fix). Add an import smoke test: `from core.pipeline import PipelineConfig, run_pipeline, main` imports cleanly.

**Risk:** Low. Removing detect_response_template imports must be coordinated (L-dead-detect) or Test 2 / test_response_template break.


### [M-skip-marker] MEDIUM — CONFIRMED
**Claim:** Stage skip/resume keys on raw artifact existence, not a completion marker; a crash after adapter_config.json (PEFT writes it early) but before adapter_model.safetensors false-passes the skip.

**Evidence:** ui/app.py:393-398 do_training skips on lora_adapters/adapter_config.json existence; 447-462 do_export on any merged_model/*.safetensors or model-bf16.gguf; 528-533 do_heretic on any heretic_model/*.safetensors; 683-691 do_magicquant on any magicquant/*.gguf. Documented but unfixed (389-392, 444-446, 526-527, 681-682). CLI has skip logic ONLY for REAP (pipeline.py:1026-1029); training/export/heretic/magicquant have none.

**Fix:** Add a shared completion-marker helper (core/markers.py or services.py). After exit 0 AND key artifact non-empty, write <stage_dir>/_stage_complete.json {stage,timestamp,config_hash,key_file,size}; gate every skip on marker-exists AND config_hash-matches AND key-file-non-empty (not raw globbing). Key files: lora_adapters/adapter_model.safetensors; merged_model/*.safetensors+index; heretic_model/*.safetensors; reap_model/*.safetensors; magicquant/*.gguf. Wire into CLI stage_* too. Write markers atomically (tmp+os.replace).

**Test:** tests/test_skip_markers.py: (a) adapter_config.json without adapter_model.safetensors -> skip False; (b) valid marker, matching hash -> skip True; (c) marker with different config_hash -> skip False.

**Risk:** Runs that previously false-skipped now re-run (correct but slower). Intentional config changes trigger full re-run — add a --force/--resume-anyway override.


### [M-rce-auth] MEDIUM — CONFIRMED
**Claim:** /api/run executes operator-controlled Python subprocesses by design; with auth off (default) and the port reachable, a caller gets shell-equivalent capability.

**Evidence:** ui/app.py:1137-1156 start_pipeline -> run_pipeline -> Services build scripts that load_dataset/snapshot_download/sys.path.insert and (MagicQuant) git clone+cmake (services.py:763-783) from operator-supplied values. run_script (182-217) runs as the host user. repr() blocks f-string breakout but values still drive execution. Severity gated by H3.

**Fix:** Primary mitigation is H3 (loopback default + required key for non-loopback). Document in README and the UI that running the pipeline grants shell-equivalent host access. Optionally add FOUNDRY_REQUIRE_AUTH=1 making verify_api_key raise even when API_KEY is empty.

**Test:** tests/test_ui_security.py: POST /api/run -> 401 without Bearer when API_KEY set.

**Risk:** Hard-requiring auth forces every localhost workflow to set a key; keep opt-in. Otherwise neutralized by H3.


### [M-entrypoint] MEDIUM — CONFIRMED
**Claim:** Broken `foundry` console entrypoint: pyproject declares core.pipeline:main but pipeline.py has no main().

**Evidence:** pyproject.toml:70 `foundry = 'core.pipeline:main'`; pipeline.py has no def main — the argparse CLI is under `if __name__=='__main__':` at 1430-1525. (pyproject:71 foundry-upload='core.hf_upload:main' IS valid: hf_upload.py:765 main(), :833 guard.)

**Fix:** Refactor pipeline.py:1430-1525 into `def main():` (move argparse + dry-run + run_pipeline into the body), then `if __name__=='__main__': main()`. Makes the CLI importable/testable. No logic change.

**Test:** Import smoke test asserts `from core.pipeline import main` and callable(main); optionally invoke main() under monkeypatched argv with hf_upload.dry_run mocked, asserting clean exit.

**Risk:** Minimal pure extraction; verify parse_args still reads sys.argv (it does inside main). After pip install -e ., `foundry --help` must work.


### [S-already-fixed-critical] LOW — ALREADY_FIXED
**Claim:** Prior in-repo docs flag three CRITICALs — repr()/!r injection, always-True validate_dataset, did_training flag — all already fixed at HEAD 3c9bd03.

**Evidence:** Injection: pipeline.py:444 {tc.model_name!r}, :509 {config.output_dir!r}, :607 {base_model_id!r}; services.py:102/114 repr(model_name). validate_dataset: pipeline.py:334 all_ok=True, :346/:351 set False on failure, :358 return all_ok. did_training: pipeline.py:1309 `did_training='training' in _enabled`; ui/app.py:773 same. HEAD=3c9bd03.

**Fix:** Docs hygiene only: date-stamp AUDIT_REPORT.md/HANDOFF.md/LOGIC_AUDIT.md/OPEN_QUESTIONS.md as historical (or move to docs/history/). No code change.

**Test:** Optional grep test: no bare f-string interpolation of model_name/source_model into generated scripts (every occurrence !r or repr()).

**Risk:** None.


### [M-quality-gate] MEDIUM — CONFIRMED
**Claim:** Missing pre-upload output-quality gate: nothing validates the merged/quantized model is loadable/complete before stage_upload pushes multi-GB artifacts.

**Evidence:** STAGES (pipeline.py:1377-1384) and ALL_STAGES (ui/app.py:72) go magicquant -> upload with no validation between. fast_export counts tensors but never reloads; test_export (test_training_integration.py:209-244) checks file/index presence only. No load+generate smoke check in core/ or ui/.

**Fix:** Add stage_quality_gate + QualityGateService/do_quality_gate before upload: load the final artifact (merged_model or top GGUF via llama.cpp), run one short apply_chat_template+generate (~16 tokens), assert non-empty output (catches the Qwen3.5 empty-content issue), optional perplexity delta; fail before upload on empty output. Register in STAGES/ALL_STAGES/STAGE_RUNNERS; skippable via --no-quality-gate.

**Test:** tests/test_quality_gate.py (mock model.generate): passes on non-empty output, fails on empty; assert wired before upload in ALL_STAGES.

**Risk:** Adds GPU/runtime cost and a new failure mode; keep threshold lenient (non-empty) and opt-out. Loading a 30B GGUF needs llama.cpp (already auto-installed for MagicQuant).


### [M-gpu-preflight] MEDIUM — CONFIRMED
**Claim:** Missing GPU-memory preflight despite OOM-from-shared-memory being the documented dominant failure.

**Evidence:** No torch.cuda.mem_get_info / rocm-smi at any stage start (grep: none). OPEN_QUESTIONS item 3 and CLAUDE.md (unload LM Studio; shared pool) document OOM as the main failure. _run (pipeline.py:191-211) just launches.

**Fix:** Add core/preflight.py check_gpu_memory(estimated_gb, log) reading torch.cuda.mem_get_info() (free,total) with a rocm-smi fallback; abort early when free < estimate with free-vs-needed. Estimate per stage (~30 GB train, ~6 GB merge for 40B, scaled by param count from config.json). Call at the top of stage_training/stage_export (CLI) and do_training/do_export (UI). Surface on /health optionally.

**Test:** tests/test_preflight.py (monkeypatch mem_get_info): ok when free>needed, abort when free<needed; rocm-smi fallback parser handles a sample string.

**Risk:** mem_get_info is a snapshot; another process can grab memory after the check — advisory, not a guarantee. Keep estimates conservative and overridable (--skip-preflight).


### [M-warmup] MEDIUM — CONFIRMED
**Claim:** UI training silently ignores warmup_ratio and always uses warmup_steps=10; CLI uses warmup_ratio=0.05 — same config yields different LR schedules.

**Evidence:** ui/app.py:127 warmup_steps:int=10; :423 passes warmup_steps=tc.warmup_steps; services.py:245 emits warmup_steps=. CLI pipeline.py:42 warmup_ratio:float=0.05, :515 emits warmup_ratio=; config.py:47 warmup_ratio=0.05. No warmup_ratio field in TrainingCfg and no warmup_steps in TrainingConfig — neither side can express the other.

**Fix:** Unify (part of H1). Recommended: support warmup_ratio everywhere — add warmup_ratio to TrainingCfg and TrainingService.build_script emitting warmup_ratio=; keep warmup_steps as optional override (ratio precedence). Log the effective warmup (computed steps). Reconcile default.yaml (warmup_ratio:0.05 already present).

**Test:** Extend test_script_equivalence.py: same config -> same warmup parameterization in CLI and UI; warmup_ratio present, warmup_steps absent/consistent.

**Risk:** Changes UI effective schedule (10 steps -> 5% of steps); for tiny datasets 5% may be <10 steps. Mitigated by logging effective value.


### [M-onnx-skip] LOW — CONFIRMED
**Claim:** ONNX-only re-run keys its export skip on a GGUF artifact that path never creates (worktree only).

**Evidence:** .worktrees/quark-onnx/ui/app.py:457-476: do_export skip from mq_enabled only; with ONNX on + MagicQuant off it checks model-bf16.gguf, which core export (safetensors only) never writes. .worktrees/ is gitignored (.gitignore:48) — lives only on feat/quark-onnx-stage.

**Fix:** When landing the ONNX branch, change the predicate to safetensors_needed = mq or onnx or heretic or reap, check merged_model/*.safetensors in that case, fall back to model-bf16.gguf only for pure GGUF export. Once M-skip-marker lands, the marker gate supersedes this guess.

**Test:** When ONNX lands: tests/test_export_skip.py — onnx on + mq off checks merged_model/*.safetensors, not model-bf16.gguf.

**Risk:** Worktree-only; merging the branch must fold this in. Superseded by M-skip-marker.


### [L-config-fragmentation] LOW — CONFIRMED
**Claim:** Config fragmented across three schemas; CLI loader reads only data['training'] and configs/default.yaml is flat (no training: wrapper), so --config configs/default.yaml is a silent no-op.

**Evidence:** Schemas: config.py:22-84 FoundrySettings (used only for ui_port, ui/app.py:1201), pipeline.py:27-146 dataclasses, ui/app.py:113-177 Pydantic. Loader pipeline.py:1462-1464 `if 'training' in data: ... setattr(cfg.training,k,v)`. configs/default.yaml is FLAT (top-level model_name/lora_r/... no training: key — confirmed by cat), so --config default.yaml sets nothing; configs/bf16-zeroclaw.yaml nests under training: and works. Defaults conflict (8192 vs 4096; adamw_8bit vs paged_adamw_8bit).

**Fix:** Promote one Pydantic model imported by both entrypoints. Make the loader populate all sections (export/heretic/reap/magicquant/upload) and accept flat or nested YAML (no training: key -> treat top-level training keys as training). Rewrite configs/default.yaml to nest under training: (+ export:/magicquant:) OR accept its flat form — pick one and make it load. Do alongside H1.

**Test:** tests/test_config_load.py: for each configs/*.yaml, build PipelineConfig and assert the training section is populated (e.g. lora_r reflects the file); default.yaml no longer a no-op.

**Risk:** Changing default.yaml parsing could alter behavior for anyone relying on its current no-op; low since it does nothing now. Validate by loading every configs/ file.


### [L-logging-dead] LOW — CONFIRMED
**Claim:** core/logging_config.py (structlog) is dead code, never imported; pipeline.py and hf_upload.py each define their own LogFn/_default_log.

**Evidence:** grep for logging_config/configure_logging/ws_callback_factory across core/ ui/ (excluding the file) returns zero importers; structlog appears only in logging_config.py. Duplicate LogFn/_default_log: pipeline.py:183-188 and hf_upload.py:26-29. pyproject.toml:38 ships structlog>=24.0.0.

**Fix:** Decide its fate. Recommended (lowest churn): delete core/logging_config.py and drop structlog from pyproject, since logging is the print/WebSocket-callback model. Or wire configure_logging() at ui/app.py + pipeline.py startup and replace the two _default_log defs. Either way dedupe LogFn/_default_log into one shared helper (core/log.py) imported by pipeline.py and hf_upload.py.

**Test:** Grep test: no module imports logging_config (until wired in or removed); after dedupe assert pipeline._default_log and hf_upload._default_log share behavior.

**Risk:** Deleting structlog is safe (no importers). Dedupe must preserve the prefix mapping (pipeline.py:187).


### [L-supply-chain] LOW — CONFIRMED
**Claim:** Auto-clone/auto-install of llama.cpp and amd/Quark without pinned SHAs or wheel hashes.

**Evidence:** pipeline.py:375 `git clone --depth 1 https://github.com/ggml-org/llama.cpp.git` (ensure_llamacpp); services.py:767-769 same clone in the MagicQuant script; .worktrees/quark-onnx/core/onnx_quark.py auto-installs amd-quark/onnxruntime-genai and clones Quark. No commit SHA pin; no wheel hashes.

**Fix:** Pin llama.cpp to a known-good commit (clone without --depth then `git checkout <SHA>`, or --branch <tag>); store the SHA in a constant/config. Pin amd-quark/onnxruntime-genai to exact versions+hashes when the ONNX stage lands. Improves reproducibility too.

**Test:** Static test: clone commands include a pinned ref (regex for SHA/tag, not a bare default-branch clone).

**Risk:** Pinned llama.cpp may lag upstream; bump deliberately. Low.


### [L-timing-compare] LOW — CONFIRMED
**Claim:** API key compared with non-constant-time != (REST and WebSocket).

**Evidence:** ui/app.py:44 `if authorization != f'Bearer {API_KEY}':`; :1186 `if API_KEY and token != API_KEY:`. No hmac import (grep: zero). REST compares 'Bearer '+KEY, WS compares raw KEY (inconsistent).

**Fix:** import hmac; REST `if not hmac.compare_digest(authorization, f'Bearer {API_KEY}')`; WS `if API_KEY and not hmac.compare_digest(token, API_KEY)`. Optionally normalize both to compare the raw key.

**Test:** Covered by tests/test_ui_security.py: valid/invalid key -> 200/401.

**Risk:** Negligible hardening.


### [L-heretic-deadloop] LOW — CONFIRMED
**Claim:** Heretic Pareto best_trials loop is dead; best is always sorted_trials[0]; the KL threshold the comment implies does not exist.

**Evidence:** pipeline.py:912-925 sorts by (refusals,kl) then appends to best_trials only when kl strictly decreases; since already sorted ascending, the first appended == sorted_trials[0] and best=best_trials[0] (:925) is always sorted_trials[0]. services.py:545-552 identical. Comment at pipeline.py:924 'acceptable KL divergence' implies an unimplemented threshold.

**Fix:** Simplify to best = sorted_trials[0] and drop the min_divergence/best_trials loop in BOTH pipeline.py (912-925) and services.py (545-552), OR implement a real acceptance (filter kl<=threshold then min refusals). After H1 this exists in one place.

**Test:** tests/test_heretic_selection.py: fabricated (refusals,kl) list -> selector returns expected trial under simplified and (if added) threshold policies.

**Risk:** None to output (current == sorted_trials[0]); a real threshold WOULD change the selected trial — gate behind a config flag.


### [L-dead-detect] LOW — PARTIAL
**Claim:** detect_response_template() is dead in production but NOT fully dead — the tests still import/call it, so removal must update tests in the same change.

**Evidence:** fast_train_zeroclaw.py:303 defines it; no production caller (pipeline.py/services.py set completion_only_loss=True, never call it). BUT tests/test_pipeline.sh:128 imports it and tests/test_training_integration.py:100-116 (test_response_template) imports and asserts on it.

**Fix:** Remove detect_response_template (fast_train_zeroclaw.py:303-343) AND simultaneously: drop it from the import at tests/test_pipeline.sh:128, and delete test_response_template + its call in tests/test_training_integration.py (lines 100-116 and the call ~280). Or wire it deliberately into the training script. Do not remove without touching tests or Test 2 / the integration test fail at import.

**Test:** After removal, the import smoke test must not reference detect_response_template; assert the symbol is gone.

**Risk:** Removing without updating tests breaks Test 2 import and collection of the integration test. Coordinated removal is safe.


### [L-reap-archlist] LOW — CONFIRMED
**Claim:** REAP arch allow-list mixes class names with HF repo-id strings that can never match _detect_model_arch's output.

**Evidence:** REAP_SUPPORTED_ARCHS at pipeline.py:976-987 and ui/app.py:568-579 include class names (Qwen3MoeForCausalLM, MixtralForCausalLM) AND repo-ids ('Qwen3-Coder-30B-A3B-Instruct', 'gpt-oss-20b'). _detect_model_arch (pipeline.py:990-1003, ui/app.py:582-594) returns architectures[0] (a class name), so repo-id entries are unreachable; a real gpt-oss (e.g. GptOssForCausalLM) is silently skipped.

**Fix:** Replace repo-id entries with actual architectures[0] class strings cross-checked against reap.model_util.MODEL_ATTRS. Define the set once (shared module, L-source-dup) so CLI and UI agree.

**Test:** tests/test_reap_arch.py: every entry looks like a CausalLM class name (regex); if REAP importable, each is a key in reap.model_util.MODEL_ATTRS.

**Risk:** A wrong class name skips a supported model (already the case for those) or attempts an unsupported one (fails in-stage). Cross-check against REAP's MODEL_ATTRS.


### [L-source-dup] LOW — CONFIRMED
**Claim:** Stage source-resolution priority chain and the REAP stub block are duplicated 3+ times verbatim.

**Evidence:** Source priority (reap>heretic>merged>gguf) at pipeline.py:1194-1208, services.py:799-831, ui/app.py:843-866 (and REAP source pipeline.py:1015-1021 vs ui/app.py:619-624). REAP 14-module stub block verbatim at pipeline.py:1069-1084 and services.py:629-644. REAP_SUPPORTED_ARCHS duplicated pipeline.py:976-987 and ui/app.py:568-579.

**Fix:** Centralize: (1) artifact-source priority once as data on Artifacts (pipeline.py:151) or a shared services function. (2) REAP stub list + sys.path insert into one helper reused by ReapService and CLI. (3) REAP_SUPPORTED_ARCHS once. Folds into H1.

**Test:** tests/test_source_resolution.py: parametrize artifact layouts; the shared resolver returns documented priority; REAP_SUPPORTED_ARCHS is one object referenced by both modules.

**Risk:** Low behavior-preserving dedupe; the three chains currently agree.


### [L-reap-path-hardcoded] LOW — CONFIRMED
**Claim:** REAP integration hardcodes /server/programming/reap/src and is not in pyproject; non-portable.

**Evidence:** services.py:646 `sys.path.insert(0,'/server/programming/reap/src')` and pipeline.py:1086 identical. REAP not in pyproject deps/extras. Stub block injects 14 fake modules (services.py:629-644, pipeline.py:1069-1084).

**Fix:** Make the src path configurable: read FOUNDRY_REAP_SRC or FoundrySettings.reap_src_path (current path as default) and emit via repr(). Document REAP as an optional external integration; add an optional extras group or documented manual install. Centralize the stub block (L-source-dup).

**Test:** tests/test_reap_service.py: ReapService.build_script emits the configured src path (parametrize), not a hardcoded constant.

**Risk:** Unset path on a new machine now fails fast with a clear message instead of a confusing import error — net improvement.


### [L-fast-load-hack] LOW — CONFIRMED
**Claim:** fast_load fabricates a 2-entry hf_device_map and sets is_quantized=False to bypass HF validation; brittle against transformers/accelerate internals.

**Evidence:** fast_train_zeroclaw.py:296-298 `model.is_quantized=False` and `model.hf_device_map={'':0,'_dummy':0}` with comments (292-296) explaining it forces verify_device_map to skip and avoids validate_quantization_for_training. '_dummy' is synthetic.

**Fix:** Add a startup version assertion: pin and assert validated transformers+accelerate versions so an upgrade fails loudly pointing at this hack; comment the exact validated versions. Pin these in pyproject (transformers>=4.40.0 is too loose given this internals dependency).

**Test:** tests/test_fast_load_versions.py: the version guard raises/warns on out-of-range and passes for known-good (pure string logic).

**Risk:** Too-strict assertion blocks legit upgrades; make it a warning + documented known-good range or assert only the specific internals.


### [L-config-post] LOW — CONFIRMED
**Claim:** POST /api/config persists arbitrary unvalidated JSON.

**Evidence:** ui/app.py:992-998 set_config: cfg=load_config(); cfg.update(body); save_config(cfg) — body is dict, no schema. Inert today (only hf_username consumed) but writes attacker-controlled keys to ui/config.json.

**Fix:** Define a small Pydantic UIConfig (hf_username + real keys, extra='forbid'); validate body before merging; bound value sizes.

**Test:** Extend tests/test_ui_security.py: POST /api/config with an unexpected key -> 422/400; a valid hf_username persists.

**Risk:** Omitting a real key from the allowlist loses persistence — enumerate consumed keys first (currently hf_username).


### [L-hf-token-scope] LOW — CONFIRMED
**Claim:** HF token injected into every stage subprocess, not just upload; widens blast radius.

**Evidence:** ui/app.py:201-204 run_script loads HF_TOKEN from ~/.cache/huggingface/token into env for ALL stages (run_script is shared by every stage). activate.sh:26-28 exports it process-wide.

**Fix:** Scope to upload: add inject_hf_token: bool=False to run_script and set True only from do_upload (and CLI upload); remove the unconditional injection at ui/app.py:201-204. For gated base models at train time, allow opt-in (FOUNDRY_HF_TOKEN_ALL_STAGES=1).

**Test:** tests/test_token_scope.py (capture run_script env): HF_TOKEN present for upload script, absent for training script by default.

**Risk:** Gated models needed at training fail without the token — provide the per-stage opt-in.


### [L-cli-timeout] LOW — CONFIRMED
**Claim:** CLI subprocess has no timeout/interrupt; a gfx1151 kernel hang wedges the CLI indefinitely.

**Evidence:** pipeline.py:202-210 _run uses subprocess.Popen then iterates stdout and proc.wait() with no timeout. The UI has os.killpg stop (ui/app.py:1165-1169); the CLI has no equivalent or deadline.

**Fix:** Thread an optional per-stage timeout/deadline through _run (pipeline.py:191): launch with start_new_session=True, read stdout with a deadline, on expiry os.killpg(os.getpgid(proc.pid),SIGTERM) then SIGKILL. Optional stdout-silence heartbeat watchdog.

**Test:** tests/test_run_timeout.py: _run on a sleep command with a 1s timeout returns non-zero and reaps the child (no orphan).

**Risk:** Too-short timeout kills long stages (40B training runs hours); default to none/generous, opt-in via --stage-timeout.


### [L-dataset-format] LOW — RESOLVED (2026-06-09)
`core/dataset_format.py` adds auto-detecting normalization (messages / {text} /
{prompt,completion} / alpaca {instruction,input,output}) to one importable
module; the training entry (`core/_train_entry.py`) calls
`normalize_dataset` + `messages_to_text` so every input shape yields the same
chat structure. `{text}` rows are emitted verbatim (no template); heterogeneous
sources fail loudly. Covered by `tests/test_dataset_format.py` (22 tests).

### [L-dataset-format] LOW — CONFIRMED
**Claim:** Training supports only the chat 'messages' dataset format; no alpaca/{text}/{prompt,completion} branch.

**Evidence:** pipeline.py:499-504 fmt calls apply_chat_template(ex['messages']); services.py:179-208 raises if 'messages' not in columns then maps ex['messages']. No other format branch (default.yaml comments mention a 'text' field the code never honors).

**Fix:** Add format detection/normalization in the shared training entry: messages -> chat template; text -> use directly; {prompt,completion} -> synthesize messages; else error. Or a configurable dataset_format field. Lower priority.

**Test:** tests/test_dataset_format.py: feed messages/text/prompt-completion shapes to the normalizer; assert the produced 'text' column is correct for each.

**Risk:** Auto-detect could mis-handle multi-candidate columns; prefer explicit dataset_format with auto fallback.


### [L-stage-script-cleanup] LOW — CONFIRMED
**Claim:** Generated _stage_*.py scripts (with embedded config values) accumulate uncleaned in output dirs.

**Evidence:** pipeline.py:544-548 writes _stage_train.py and never unlinks (also _stage_export.py:614, _stage_heretic.py:952, _stage_reap.py:1168). ui/app.py:188 writes _stage_{ts}.py + .log (206); only workflow JSON has an unlink path. Scripts embed model names/paths/dataset IDs.

**Fix:** Write stage scripts to a tempfile.mkdtemp and clean up on success, OR unlink _stage_*.py after a successful run (keep the .log). Retain on failure for debugging. Apply in both CLI _run callers and run_script.

**Test:** tests/test_script_cleanup.py: success removes (or moves) the _stage_*.py while the .log remains; failure retains the script.

**Risk:** Removing on success loses post-mortem reproducibility; keep on failure and behind a --keep-stage-scripts flag.


## Additional issues the audit missed

- pyproject.toml ships datagen extras (lines 56-62: anthropic/ebooklib/bs4/lxml) for modules NOT tracked in git — `git ls-files datagen gardener` returns nothing; the on-disk datagen/ and gardener/ dirs contain only __pycache__ and a stale generation.log (no .py source). So `pip install -e .[datagen]` pulls deps for nonexistent modules; CLAUDE.md Directory Structure still lists datagen/ and gardener/ as live. Fix: remove the datagen extras + references, or restore from git history and document.
- Version drift: core/__version__.py:1 says '0.1.0' and pyproject.toml:6 version='0.1.0', but CHANGELOG.md documents [0.2.0] (API key auth, which IS in the code at ui/app.py:40-45). All three disagree with reality. Fix: bump __version__ and pyproject version to match the shipped 0.2.0 auth feature.
- Leftover duplicate egg-info: both foundry.egg-info AND pipeline.egg-info exist at the repo root (pre-rename artifact). Though *.egg-info is gitignored (.gitignore:9), the stale pipeline.egg-info confuses import resolution. Fix: rm -rf pipeline.egg-info (the Makefile clean target removes *.egg-info but it regenerated).
- README.md is stale/misleading: line 3 says '4-stage workflow' and omits heretic (abliteration) and reap (MoE pruning); the code has 6 stages (ALL_STAGES ui/app.py:72). README:142 says 'The UI serves without authentication — use only on trusted networks' though Bearer auth shipped in 0.2.0 (CHANGELOG). ui/app.py module docstring (lines 4-6) also still says '4-stage'. Fix: document all six stages and the shipped auth.
- Env divergence beyond warmup/kbit (subsumed by H1 but must be ported explicitly): CLI _run() (pipeline.py:193-198) does NOT set UNSLOTH_SKIP_TORCHVISION_CHECK, but services._env_preamble (services.py:33) and ui/app.py run_script (:196) DO. Also services-only chat-template fallback (services.py:117-120) and messages-normalization/token-length analysis (services.py:176-227) are absent from pipeline.py's training script — so CLI training of a template-less model (e.g. Gemma) can crash where the UI would not.
- Dockerfile HEALTHCHECK (final lines) curls http://localhost:7865/api/state, which is now behind verify_api_key — when FOUNDRY_API_KEY is set in the container the healthcheck will 401 and report the container unhealthy. Fix: point the healthcheck at /health (the unauthenticated endpoint, ui/app.py:963).
- Docker / runtime container exposure: docker-compose.yml publishes ${FOUNDRY_UI_PORT:-7865}:7865 and the Dockerfile CMD binds 0.0.0.0 with no API key default — combined with H3/M-rce, the container is reachable on the host's LAN without auth out of the box. Fix: document that the published port must be access-controlled and/or set a required key in the compose env.

## Ordered implementation plan

1. 1. Fix the two install/test breakages first so the suite can run: refactor core/pipeline.py:1430-1525 __main__ into def main() + guard call (M-entrypoint); fix tests/test_pipeline.sh:19 to derive PIPELINE_DIR from $(dirname BASH_SOURCE)/.. and update its stale unsloth-path messages (H4).
2. 2. Stand up offline pytest infrastructure: register slow/gpu markers in pyproject [tool.pytest.ini_options], port the worktree mock pattern (.worktrees/quark-onnx/tests/test_onnx_quark.py) into master, add an import smoke test (from core.pipeline import main, run_pipeline). Safety net before refactoring.
3. 3. Add the UI security test + bind/auth fix (H3): FOUNDRY_UI_HOST default 127.0.0.1 in ui/app.py:1199-1202, ui/run.sh, config.py; switch both API-key comparisons to hmac.compare_digest (L-timing-compare); add tests/test_ui_security.py; fix the Dockerfile HEALTHCHECK to /health. Neutralizes M-rce-auth.
4. 4. Port pipeline.py-only behavior INTO the Service classes (prereq, nothing lost): GPT-OSS prefix into HereticService; pick services' manual norm-upcast kbit; add warmup_ratio support and unify warmup (M-warmup); make services' chat-template fallback + UNSLOTH_SKIP_TORCHVISION_CHECK the canonical path.
5. 5. Add the script-equivalence test (H1 test) BEFORE rewiring, then migrate CLI stage_* functions to instantiate/call the Service classes; reconcile TrainingConfig vs TrainingCfg defaults. Run the equivalence test to prove CLI==UI where intended. (Resolves H1 + M-warmup + env-divergence.)
6. 6. Centralize duplicated data while open: artifact-source priority on Artifacts (L-source-dup), REAP stub block + REAP_SUPPORTED_ARCHS into one shared location, configurable REAP src path (L-reap-path-hardcoded), fix the REAP arch allow-list class names (L-reap-archlist).
7. 7. Promote one shared config module and fix the YAML loader to populate all sections + make configs/default.yaml actually load (L-config-fragmentation); add tests/test_config_load.py over configs/*.yaml.
8. 8. Implement completion markers and gate all skip/resume on them, shared CLI+UI (M-skip-marker); add tests/test_skip_markers.py. Supersedes the artifact-existence skips and the worktree ONNX skip (M-onnx-skip).
9. 9. Add the GPU-memory preflight (M-gpu-preflight, core/preflight.py) and the pre-upload quality-gate stage (M-quality-gate, new Service wired into STAGES/ALL_STAGES before upload); add their offline tests.
10. 10. Extract stage bodies into importable entry modules + thin launchers (H2), following onnx_quark.py, now that consolidation (step 5) means it is done once; add tests/test_stage_entries.py.
11. 11. Cleanup/refactor batch: remove or wire logging_config + dedupe LogFn/_default_log, drop structlog if unused (L-logging-dead); simplify the heretic dead loop (L-heretic-deadloop); scope HF_TOKEN to upload (L-hf-token-scope); validate POST /api/config (L-config-post); pin llama.cpp/Quark (L-supply-chain); add fast_load version guard (L-fast-load-hack); add CLI _run timeout (L-cli-timeout); clean up generated _stage_*.py (L-stage-script-cleanup); remove detect_response_template AND its test callers together (L-dead-detect); add dataset-format support (L-dataset-format).
12. 12. Documentation + housekeeping (non-code): update README/ui docstring to six stages + shipped auth; date-stamp AUDIT_REPORT/HANDOFF/LOGIC_AUDIT/OPEN_QUESTIONS as historical (S-already-fixed-critical); bump __version__/pyproject to match CHANGELOG; rm stale pipeline.egg-info; remove or restore datagen/gardener extras+refs.

## Test strategy

Existing infra: a bash suite tests/test_pipeline.sh (10 tests, broken at line 19 hardcoded /server/programming/pipeline, and most tests assert against absolute /server/ai model dirs + a running LMStudio — environment-specific, not portable) and one GPU integration module tests/test_training_integration.py (needs a GPU + ~5 GB download; explicitly excluded by `make test`). The worktree (.worktrees/quark-onnx/tests/) already demonstrates the target offline pattern: test_onnx_quark.py mocks subprocess and asserts argv construction (no GPU); test_onnx_stage.py uses pytest.importorskip + @pytest.mark.slow for the heavy path. Adopt that on master. How to run: `make test` runs `pytest tests/ -v --ignore=tests/test_training_integration.py` (after fixes it should collect the new offline modules); `make test-integration` runs the GPU module; `make lint` is py_compile of the 5 main files (keep, add ruff per roadmap). Plan: (1) Register markers slow/gpu in pyproject and tag the GPU body of test_training_integration.py. (2) Build a pure-offline suite needing no GPU/network/models, importable from repo root with sys.path.insert(0,'core'): test_script_equivalence.py (CLI vs UI generated-script equality — keystone for H1), test_ui_security.py (FastAPI TestClient/httpx, already in dev extras — auth/bind/health/config-validation for H3+M-rce+L-config-post+L-timing), test_skip_markers.py (completion-marker predicate over tmp dirs, M-skip-marker), test_config_load.py (every configs/*.yaml populates a full PipelineConfig, L-config), test_preflight.py and test_quality_gate.py (monkeypatch torch.cuda.mem_get_info and model.generate), plus the small unit tests per finding (heretic selection, reap arch, source resolution, dataset format, token scope, run timeout, fast_load version guard, stage cleanup). (3) Keep test_pipeline.sh as an environment smoke test but fix its path and gate the LMStudio/model-dir tests behind existence checks so it degrades to SKIP not FAIL off the dev box. (4) Add .github/workflows/ running `make lint` + the offline `make test` (CPU-only) on push/PR; never run gpu/slow markers in CI. Coverage goal: every confirmed finding ships with the offline test named in its test_to_add, and the H1 script-equivalence test runs BEFORE the consolidation refactor so the migration is proven non-regressing.

## Notes

Verified against HEAD 3c9bd03 (clean tree; only the untracked AUDIT_2026-06-09.md present). All six prompt focus areas confirmed with file:line evidence. Refinements to the prior audit: (a) M-skip's 'CLI has no skip logic at all' is PARTIAL — the CLI does skip REAP (pipeline.py:1026-1029); it lacks skip/resume for training/export/heretic/magicquant. (b) L-dead-detect is PARTIAL not fully dead — detect_response_template is still imported/asserted by both test files, so removal must update tests in the same change. (c) the foundry-upload entrypoint (pyproject:71) is VALID (hf_upload.main exists); only `foundry` (pipeline:main) is broken. (d) datagen/gardener: the audit's 'no tracked source exists' is correct (git ls-files empty), but the dirs DO exist on disk with stale pycache/log — the extras-for-nonexistent-modules problem is real. .worktrees/ is gitignored, so M-onnx-skip and the onnx_quark supply-chain item are branch-only — fix when landing feat/quark-onnx-stage. Everything else (injection closed, validate_dataset returns all_ok, did_training uses the enabled set) is genuinely already_fixed at HEAD, matching the audit's headline. Relevant absolute paths: /server/programming/Foundry/core/pipeline.py, /server/programming/Foundry/core/services.py, /server/programming/Foundry/ui/app.py, /server/programming/Foundry/core/config.py, /server/programming/Foundry/core/fast_train_zeroclaw.py, /server/programming/Foundry/tests/test_pipeline.sh, /server/programming/Foundry/tests/test_training_integration.py, /server/programming/Foundry/pyproject.toml, /server/programming/Foundry/configs/default.yaml, /server/programming/Foundry/ui/run.sh, /server/programming/Foundry/Dockerfile.