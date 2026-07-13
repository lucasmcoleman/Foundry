#!/usr/bin/env python3
"""
Foundry UI — FastAPI backend.

Orchestrates a 6-stage LLM fine-tuning pipeline (Heretic and REAP optional):
  Training → Export → Heretic → REAP → MagicQuant → Upload

Stage scripts are built by the shared service layer (core/services.py), the same
source of truth the CLI (core/pipeline.py) uses.

Uses WebSocket for real-time log streaming to the browser. Binds 127.0.0.1 by
default (set FOUNDRY_UI_HOST=0.0.0.0 + FOUNDRY_API_KEY to expose). Port defaults
to 7865 (configurable via FOUNDRY_UI_PORT).
"""

import asyncio
import hmac
import json
import os
import re
import signal
import sys
import time
from enum import Enum
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Depends, HTTPException, Header, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, FileResponse
from pydantic import BaseModel, ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "core"))
import markers
from config import settings as foundry_settings
from reap_common import REAP_SUPPORTED_ARCHS, detect_model_arch as _detect_model_arch
from services import (
    TrainingService,
    ExportService,
    HereticService,
    ReapService,
    QATService,
    MagicQuantService,
    ROCmFPXService,
    UploadService,
)
from serving import build_serve_command, detect_mtp, format_serve_command


def _training_marker_hash(tc) -> str:
    """Config hash for the training completion marker (mirrors the CLI)."""
    return markers.config_hash({
        "model_name": tc.model_name, "datasets": tc.datasets,
        "max_seq_length": tc.max_seq_length, "lora_r": tc.lora_r,
        "lora_alpha": tc.lora_alpha, "lora_dropout": tc.lora_dropout,
        "use_rslora": tc.use_rslora, "num_train_epochs": tc.num_train_epochs,
        "per_device_train_batch_size": tc.per_device_train_batch_size,
        "gradient_accumulation_steps": tc.gradient_accumulation_steps,
        "learning_rate": tc.learning_rate, "lr_scheduler_type": tc.lr_scheduler_type,
        "warmup_ratio": tc.warmup_ratio, "optim": tc.optim, "packing": tc.packing,
    })

API_KEY = os.environ.get("FOUNDRY_API_KEY", "")
# When set (FOUNDRY_REQUIRE_AUTH=1), every protected endpoint requires a key even
# if API_KEY is empty — fails closed instead of open.
REQUIRE_AUTH = os.environ.get("FOUNDRY_REQUIRE_AUTH", "0") not in ("", "0", "false", "False")


async def verify_api_key(authorization: str = Header(default="")):
    """Check Bearer token in the Authorization header.

    No-op when API_KEY is unset AND auth is not required. Uses a constant-time
    comparison (hmac.compare_digest) to avoid timing side channels.
    """
    if not API_KEY:
        if REQUIRE_AUTH:
            raise HTTPException(status_code=401, detail="Authentication required but no API key configured")
        return
    if not hmac.compare_digest(authorization, f"Bearer {API_KEY}"):
        raise HTTPException(status_code=401, detail="Invalid API key")

app = FastAPI(title="Foundry")

# Explicit CORS allowlist -- no wildcard. The UI is a single-page app served
# from this same origin (index.html + static assets) and authenticates via a
# Bearer token (FOUNDRY_API_KEY), never cookies, so allow_credentials is left
# at its default (False): explicit origins + credentials is only needed for
# cookie-based auth, which this app doesn't use.
#
# Origins come from FOUNDRY_UI_ORIGINS (comma-separated) when set, else a
# built-in list covering loopback + this box's LAN addresses/hostname on the
# UI port. Extend FOUNDRY_UI_ORIGINS if the box gets a new LAN IP.
_UI_PORT = os.environ.get("FOUNDRY_UI_PORT", str(foundry_settings.ui_port))
_default_origins = [
    f"http://localhost:{_UI_PORT}", f"http://127.0.0.1:{_UI_PORT}",
    f"http://192.168.0.29:{_UI_PORT}",      # eno1 (static LAN)
    f"http://192.168.0.194:{_UI_PORT}",     # wlp195s0 (dynamic LAN)
    f"http://masterserver:{_UI_PORT}",      # hostname
    f"http://masterserver.local:{_UI_PORT}",
]
_env_origins = os.environ.get("FOUNDRY_UI_ORIGINS", "")
ALLOWED_ORIGINS = (
    [o.strip() for o in _env_origins.split(",") if o.strip()]
    if _env_origins else _default_origins
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=False,
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["Authorization", "Content-Type"],
)

FOUNDRY_DIR = Path(__file__).resolve().parent.parent


def _resolve_venv_python() -> str:
    """Locate the venv Python interpreter at runtime."""
    candidate = FOUNDRY_DIR / ".venv" / "bin" / "python"
    if candidate.exists():
        return str(candidate)
    return sys.executable


VENV_PYTHON = _resolve_venv_python()


# ── Flywheel integration ─────────────────────────────────────────────────────
# foundry-flywheel is a sibling repo that composes Foundry + foundry_gym +
# veredict + MagicQuant + did-it-help into one gated improvement iteration.
# It is imported/run READ-ONLY as a library; the UI never edits its tree.
FLYWHEEL_ROOT = Path(os.environ.get("FLYWHEEL_ROOT", "/server/programming/foundry-flywheel"))


def _resolve_flywheel_python() -> str:
    """Interpreter for the flywheel subprocess.

    Prefer the flywheel's own venv when present; otherwise fall back to the UI's
    venv Python (VENV_PYTHON), which has numpy/pyyaml/requests and can import
    ``flywheel`` plus the composed tools (they are added to sys.path on demand by
    ``flywheel/_tool_paths.py``). Override the repo location with FLYWHEEL_ROOT.
    """
    cand = FLYWHEEL_ROOT / ".venv" / "bin" / "python"
    if cand.exists():
        return str(cand)
    return VENV_PYTHON


# ── State ────────────────────────────────────────────────────────────────────

class StageStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETE = "complete"
    FAILED = "failed"
    SKIPPED = "skipped"

ALL_STAGES = ["training", "export", "heretic", "reap", "qat", "magicquant", "rocmfpx", "upload"]

class PipelineState:
    """Shared mutable state for the running pipeline, including WebSocket fan-out."""

    def __init__(self):
        self.stages = {s: StageStatus.PENDING for s in ALL_STAGES}
        self.running = False
        self.current_stage = None
        self.progress = 0
        self.ws_clients: list[WebSocket] = []
        self.active_proc = None

    async def broadcast(self, msg: dict):
        dead = []
        for ws in list(self.ws_clients):
            try:
                await ws.send_json(msg)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.ws_clients.remove(ws)

    async def log(self, text: str, level: str = "info"):
        await self.broadcast({"type": "log", "text": text, "level": level, "ts": time.time()})

    async def set_stage(self, stage: str, status: StageStatus):
        self.stages[stage] = status
        if status == StageStatus.RUNNING:
            self.current_stage = stage
        await self.broadcast({"type": "stage_update", "stage": stage, "status": status.value})

    async def set_progress(self, pct: int):
        self.progress = pct
        await self.broadcast({"type": "progress", "percent": pct})

state = PipelineState()


# ── Pydantic models ──────────────────────────────────────────────────────────

class TrainingCfg(BaseModel):
    model_name: str = "Tesslate/OmniCoder-9B"
    datasets: list[str] = ["data/zeroclaw_training_data.jsonl"]
    max_seq_length: int = 4096
    load_in_4bit: bool = True
    lora_r: int = 32
    lora_alpha: int = 64
    lora_dropout: float = 0.05
    use_rslora: bool = True
    num_train_epochs: int = 3
    per_device_train_batch_size: int = 2
    gradient_accumulation_steps: int = 4
    learning_rate: float = 2e-4
    lr_scheduler_type: str = "cosine"
    warmup_ratio: float = 0.05  # preferred; same as CLI. Logs the effective steps.
    warmup_steps: Optional[int] = None  # optional override; ratio wins when both set
    optim: str = "paged_adamw_8bit"
    packing: bool = False
    output_dir: str = "./output"

class ExportCfg(BaseModel):
    gguf_type: str = "bf16"
    also_save_merged: bool = False
    source_model: str = ""  # when training is skipped: HF ID or local path to model/lora

class HereticCfg(BaseModel):
    n_trials: int = 200
    n_startup_trials: int = 60
    quantization: str = "bnb_4bit"
    kl_divergence_scale: float = 1.0
    orthogonalize_direction: bool = False
    row_normalization: str = "none"

class ReapCfg(BaseModel):
    compression_ratio: float = 0.25
    prune_method: str = "reap"
    samples_per_category: int = 512
    model_max_length: int = 2048
    dataset_name: str = "theblackcat102/evol-codealpaca-v1"
    seed: int = 42

class MagicQuantCfg(BaseModel):
    target_base_quant: str = "MXFP4_MOE"
    generations: int = 50
    population_size: int = 100
    tiers: list[str] = ["Q4", "Q5", "Q6"]
    llamacpp_path: str = ""
    source_model: str = ""  # when export is skipped: path to GGUF or merged model dir
    measured: bool = False           # real perplexity search vs prediction-only
    measurement_rounds: int = 3
    rocmfpx_schemes: bool = False    # explore AMD-native ROCmFPX fork types
    iq_schemes: bool = False         # explore sub-4-bit stock-ggml IQ schemes
    seed: Optional[int] = None       # optional RNG seed for a reproducible search
    use_imatrix: bool = False        # capture/reuse an importance matrix (both search paths)
    imatrix_corpus: Optional[str] = None  # calibration corpus; None = bundled default
    enable_kl: bool = False          # measured-search only: real KL-divergence-to-base
    kl_weight: float = 0.1           # weight of |mean_kl| in measured-search selection
    enable_speed_bench: bool = False  # measured-search only: real tokens/sec per candidate
    measurement_chunks: Optional[int] = None  # cap perplexity/KL passes (both search paths)
    stream_aware: bool = False       # bias sampling toward BF16->Q8_0 on streamed groups
    head_aggressive: bool = False    # bias 'H' (LM head) group sampling toward smaller K-quants
    speed_aware: bool = False        # measured-search only: prefer fastest near-tied candidate per tier
    speed_metric: str = "bytes"      # "bytes" (deterministic) | "bench" (measured tg); only meaningful when speed_aware
    speed_weight: Optional[float] = None  # tps-aware SEARCH objective weight (both search paths); None = unchanged weights
    use_bytes_tps: bool = False      # score the search objective's speed term from predicted size, not noisy speed_multiplier
    calibration_source: str = ""     # path to a noise_calibration.json to load (both search paths)
    write_calibration: bool = False  # measured-search only: emit <output>/noise_calibration.json from this run

class QATCfg(BaseModel):
    """QAT-LoRA stage config.

    The hybrid quant config comes from a prior MagicQuant search's
    ``search_results.json`` (``config_source`` + ``tier``); when empty it
    auto-resolves to ``<output>/<model>/magicquant/search_results.json``.
    """
    config_source: str = ""  # path to search_results.json; empty = auto-detect
    tier: str = "Q4"
    dataset: str = ""
    lora_r: int = 32
    lora_alpha: float = 64.0
    epochs: float = 1.0
    max_steps: int = -1
    lr: float = 2e-4
    max_seq_len: int = 512

class ROCmFPXCfg(BaseModel):
    """ROCmFPX stage config (AMD-tuned GGUF quant family, see docs/rocmfpx.md).

    Auto-installed on first use (git clone + Strix Halo build) -- not a pip
    package, unlike MagicQuant.
    """
    formats: list[str] = ["rocmfp4-agent", "rocmfp6-agent", "rocmfp8-agent"]
    rocmfpx_hint: str = ""  # path to an existing ROCmFPX/llama.cpp-fork build
    source_model: str = ""  # when export is skipped: path to GGUF or merged model dir
    imatrix: str = ""  # optional path to an imatrix GGUF

class UploadCfg(BaseModel):
    repo_id: str = ""
    private: bool = True
    license: str = ""  # empty = auto-detect from base model
    upload_gguf: bool = True
    upload_lora: bool = False
    upload_merged: bool = False
    upload_dataset: bool = True

class UIConfig(BaseModel):
    """Persisted UI config (ui/config.json). Only known keys are accepted; any
    unexpected key is rejected (extra='forbid') so POST /api/config can't write
    arbitrary attacker-controlled data into the file.
    """
    model_config = {"extra": "forbid"}
    hf_username: str = ""
    # QAT stage persisted fields (T10). extra='forbid' still applies to anything
    # not listed here, so POST /api/config can only write known keys.
    qat_enabled: bool = False
    qat_dataset: str = ""
    qat_tier: str = "Q4"
    qat_lora_r: int = 32
    qat_lora_alpha: float = 64.0
    qat_epochs: float = 1.0
    qat_lr: float = 2e-4


class RunRequest(BaseModel):
    training: TrainingCfg = TrainingCfg()
    export: Optional[ExportCfg] = ExportCfg()
    heretic: Optional[HereticCfg] = None
    reap: Optional[ReapCfg] = None
    qat: Optional[QATCfg] = None
    magicquant: Optional[MagicQuantCfg] = None
    rocmfpx: Optional[ROCmFPXCfg] = None
    upload: Optional[UploadCfg] = None
    enabled_stages: list[str] = ["training", "export"]


class FlywheelRequest(BaseModel):
    """One pre-built-mode flywheel iteration driven from the browser.

    The candidate arm is a served model id (``candidate_model``); leaving it
    empty runs an A/A control against ``base_model`` (the honest outcome is
    HOLD). Train/quantize are disabled — producing a candidate (train+quant)
    is a hours-of-GPU run left for a later UI iteration.
    """
    model_config = {"extra": "forbid"}
    base_model: str = "qwen3.5:9b"
    candidate_model: Optional[str] = None          # None/empty -> A/A control
    families: str = "math_logic"                   # comma-separated gym families (= axes)
    n_tasks: int = 4                               # tasks per family (>= 2 for paired stats)
    difficulty: float = 0.3
    seeds: str = "17,29"                           # comma-separated sampling seeds
    max_tokens: int = 640
    base_url: str = "http://127.0.0.1:4004/v1"     # llama-swap OpenAI-compatible endpoint
    bootstrap_iters: int = 2000                    # did-it-help bootstrap iterations
    gpu_lock_timeout_s: float = 5400.0             # patience for /tmp/claude-gpu.lock
    name: str = "flywheel-ui"
    # Optional decision-policy knobs (None => flywheel DecisionPolicy default).
    min_macro_delta: Optional[float] = None        # practical-significance floor for KEEP
    veredict_floor: Optional[float] = None         # min candidate veredict pass-rate


# ── Subprocess helper ────────────────────────────────────────────────────────

async def run_script(script: str, output_dir: str, inject_hf_token: bool = False) -> int:
    """Write a Python script to disk and execute it in the venv, streaming stdout to WebSocket clients.

    ``inject_hf_token`` is False by default: the HF token is only loaded into the
    subprocess env for the upload stage (and for any stage when
    FOUNDRY_HF_TOKEN_ALL_STAGES=1 is set, e.g. gated base models at train time).
    This narrows the blast radius vs. injecting it into every stage.
    """
    # Resolve relative paths against the project root, not uvicorn's CWD
    out_path = Path(output_dir)
    if not out_path.is_absolute():
        out_path = FOUNDRY_DIR / out_path
    script_path = out_path / f"_stage_{int(time.time())}.py"
    script_path.parent.mkdir(parents=True, exist_ok=True)
    script_path.write_text(script)

    env = os.environ.copy()
    env.update({
        "HSA_ENABLE_SDMA": "0",
        "PYTORCH_HIP_ALLOC_CONF": "backend:native,expandable_segments:True",
        "UNSLOTH_SKIP_TORCHVISION_CHECK": "1",
        "TORCH_ROCM_AOTRITON_ENABLE_EXPERIMENTAL": "1",
        "PYTHONUNBUFFERED": "1",
    })
    # Scope the HF token to stages that need it (upload), or opt in globally via
    # FOUNDRY_HF_TOKEN_ALL_STAGES=1 (e.g. gated base models at train time).
    token_all_stages = os.environ.get("FOUNDRY_HF_TOKEN_ALL_STAGES", "0") not in ("", "0", "false", "False")
    token_wanted = inject_hf_token or token_all_stages
    if token_wanted:
        if "HF_TOKEN" not in env:
            token_path = Path.home() / ".cache" / "huggingface" / "token"
            if token_path.exists():
                env["HF_TOKEN"] = token_path.read_text().strip()
    else:
        # Do not leak an inherited token into non-upload stage subprocesses.
        env.pop("HF_TOKEN", None)

    log_path = script_path.with_suffix(".log")
    log_file = open(log_path, "w")

    try:
        proc = await asyncio.create_subprocess_exec(
            VENV_PYTHON, "-u", str(script_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=env, cwd=str(FOUNDRY_DIR),
            limit=1024 * 1024,  # 1MB line buffer — tqdm \r bars can be huge
            start_new_session=True,
        )
        state.active_proc = proc

        try:
            async for raw in proc.stdout:
                log_file.write(raw.decode("utf-8", errors="replace"))
                log_file.flush()
                # tqdm uses \r without \n, so one "line" may contain many \r-separated updates.
                # Split on \r and process the last (most recent) segment.
                segments = raw.decode("utf-8", errors="replace").split("\r")
                for text in segments:
                    text = text.strip()
                    if not text:
                        continue
                    # Parse progress from tqdm bars and shard-loading output.
                    # Two phases: loading = 0-15%, training/processing = 15-100%.
                    # Shard loading: "Done in 2.1s | Progress: 45/1275 (4%)"
                    # Tqdm training: "  5%|█ | 3/57 [00:15<04:30, 5.00s/it]"
                    pct_parsed = False
                    if "Progress:" in text and "%" in text:
                        try:
                            raw_pct = int(text.split("(")[1].split("%")[0])
                            await state.set_progress(int(raw_pct * 0.15))
                            pct_parsed = True
                        except (ValueError, IndexError):
                            pass
                    elif "%|" in text:
                        try:
                            raw_pct = int(float(text.split("%|")[0].strip().split()[-1]))
                            if "Fetching" in text or "Map:" in text or "Tokenizing" in text:
                                pass  # ignore quick preprocessing tqdm bars
                            else:
                                # Training/export tqdm: map 0-100% to 15-100%
                                await state.set_progress(15 + int(raw_pct * 0.85))
                            pct_parsed = True
                        except (ValueError, IndexError):
                            pass
                    if pct_parsed:
                        continue  # don't log every progress bar update
                    if "'loss'" in text:
                        await state.log(text, "metric")
                    elif "PIPELINE_STAGE_COMPLETE" in text:
                        await state.log(text, "success")
                    elif "Error" in text or "error" in text.lower():
                        await state.log(text, "error")
                    else:
                        await state.log(text)
        except Exception:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            raise
        finally:
            await proc.wait()
            state.active_proc = None
    finally:
        log_file.close()

    return proc.returncode


# ── Dataset validation (Improvement #3) ──────────────────────────────────────

FOUNDRY_ROOT = Path(__file__).resolve().parent.parent


async def validate_dataset(sources: list[str]) -> bool:
    """Pre-flight dataset check for one or more dataset sources."""
    await state.log("Validating dataset(s)...", "stage")

    if not sources or all(not s.strip() for s in sources):
        await state.log("No datasets configured", "error")
        return False

    all_ok = True
    for src in sources:
        src = src.strip()
        if not src:
            continue

        # Strip config/split suffixes for path detection
        clean = src
        if "[" in clean and clean.endswith("]"):
            clean = clean.split("[")[0]
        if ":" in clean and not clean.startswith("/") and not Path(clean).suffix:
            clean = clean.rsplit(":", 1)[0]

        local = Path(clean)
        if local.suffix in (".jsonl", ".json", ".csv", ".parquet"):
            # Local file -- full validation
            p = local
            if not p.is_absolute():
                p = FOUNDRY_ROOT / p
            if not p.exists():
                await state.log(f"Dataset not found: {src}", "error")
                all_ok = False
                continue
            if p.stat().st_size == 0:
                await state.log(f"Dataset file is empty: {src}", "error")
                all_ok = False
                continue

            errors = []
            n = 0
            tool_calls = 0
            roles = set()

            with open(p) as f:
                for i, line in enumerate(f, 1):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        ex = json.loads(line)
                    except json.JSONDecodeError as e:
                        errors.append(f"Line {i}: invalid JSON -- {e}")
                        if len(errors) >= 5:
                            break
                        continue
                    n += 1
                    if "messages" not in ex:
                        errors.append(f"Line {i}: missing 'messages' field")
                        continue
                    msgs = ex["messages"]
                    if not isinstance(msgs, list) or len(msgs) < 2:
                        errors.append(f"Line {i}: 'messages' needs >= 2 entries")
                        continue
                    for msg in msgs:
                        if "role" not in msg or "content" not in msg:
                            errors.append(f"Line {i}: message missing 'role' or 'content'")
                            break
                        roles.add(msg["role"])
                        if msg["role"] == "assistant" and "<tool_call>" in msg.get("content", ""):
                            tool_calls += 1

            if errors:
                for e in errors[:5]:
                    await state.log(f"  {e}", "error")
                await state.log(f"Validation failed for {src} ({len(errors)} errors)", "error")
                all_ok = False
                continue

            if n < 10:
                await state.log(f"  Warning: only {n} examples in {src}", "warn")
            await state.log(f"  {src}: {n} examples, {tool_calls} tool-call turns, roles: {sorted(roles)}")
        else:
            # Possibly a HuggingFace dataset -- check if it exists locally first
            if local.exists():
                await state.log(f"  Local path: {src}")
            else:
                await state.log(f"  HF dataset: {src} (will be downloaded if not cached)")

    if all_ok:
        await state.log("Dataset validation passed", "success")
    return all_ok


# ── Stage runners ────────────────────────────────────────────────────────────

def _resolve_out(output_dir: str) -> Path:
    """Resolve output_dir the same way run_script does."""
    p = Path(output_dir)
    return p if p.is_absolute() else FOUNDRY_DIR / p


def _assert_output_dir_contained(output_dir: str) -> None:
    """Reject an output_dir that would escape the Foundry tree.

    ``run_script`` writes a generated stage script + log into ``output_dir``
    and then executes it with the venv Python — this is the actual
    write+exec surface behind /api/run. An absolute path or a ``../`` relative
    path here would let an authenticated LAN client write and run a script
    anywhere the service user can reach. Resolve symlinks/``..`` and require
    the result to stay under FOUNDRY_DIR.
    """
    resolved = _resolve_out(output_dir).resolve()
    root = FOUNDRY_DIR.resolve()
    if resolved != root and root not in resolved.parents:
        raise HTTPException(
            status_code=400,
            detail=f"output_dir must resolve inside {root} (got {resolved})",
        )


async def do_training(cfg: RunRequest) -> bool:
    """Run the QLoRA training stage. Skips if LoRA adapters already exist."""
    tc = cfg.training
    out = _resolve_out(tc.output_dir)

    # Completion-marker resume: skip only when a valid marker matches the config
    # AND the key adapter file (adapter_model.safetensors) is present and
    # non-empty. PEFT writes adapter_config.json early, so existence alone is not
    # proof the stage finished (audit M-skip-marker).
    lora_dir = out / "lora_adapters"
    key_file = lora_dir / "adapter_model.safetensors"
    cfg_hash = _training_marker_hash(tc)
    if markers.is_stage_complete(lora_dir, key_file, cfg_hash):
        await state.log(f"Training already complete (marker matches) at {lora_dir} — skipping", "success")
        await state.set_stage("training", StageStatus.COMPLETE)
        await state.set_progress(100)
        return True

    # Validate dataset(s) before committing GPU time
    if not await validate_dataset(tc.datasets):
        return False

    await state.set_stage("training", StageStatus.RUNNING)
    await state.set_progress(0)
    await state.log("Starting QLoRA training (completion-only loss)", "stage")

    svc = TrainingService(FOUNDRY_ROOT, VENV_PYTHON)
    script = svc.build_script(
        model_name=tc.model_name,
        datasets=tc.datasets,
        output_dir=tc.output_dir,
        max_seq_length=tc.max_seq_length,
        lora_r=tc.lora_r,
        lora_alpha=tc.lora_alpha,
        lora_dropout=tc.lora_dropout,
        use_rslora=tc.use_rslora,
        num_train_epochs=tc.num_train_epochs,
        per_device_train_batch_size=tc.per_device_train_batch_size,
        gradient_accumulation_steps=tc.gradient_accumulation_steps,
        learning_rate=tc.learning_rate,
        lr_scheduler_type=tc.lr_scheduler_type,
        warmup_ratio=tc.warmup_ratio,
        warmup_steps=tc.warmup_steps,
        optim=tc.optim,
        packing=tc.packing,
    )
    rc = await run_script(script, tc.output_dir)
    ok = rc == 0
    if ok and key_file.exists() and key_file.stat().st_size > 0:
        try:
            markers.write_marker(lora_dir, "training", key_file, cfg_hash)
        except OSError:
            pass
    await state.set_stage("training", StageStatus.COMPLETE if ok else StageStatus.FAILED)
    if ok:
        await state.set_progress(100)
    return ok


async def do_export(cfg: RunRequest) -> bool:
    """Merge LoRA + export. Smart routing based on upstream/downstream stages."""
    ec = cfg.export
    out = cfg.training.output_dir
    out_abs = _resolve_out(out)
    training_enabled = "training" in cfg.enabled_stages
    mq_enabled = "magicquant" in cfg.enabled_stages

    # Completion-marker resume (audit M-skip-marker): skip only when a valid
    # marker matches AND the key artifact is present + non-empty.
    merged = out_abs / "merged_model"
    export_hash = markers.config_hash({
        "model_name": cfg.training.model_name,
        "source_model": cfg.export.source_model if cfg.export else "",
        "training_enabled": training_enabled,
    })
    existing_st = sorted(merged.glob("*.safetensors")) if merged.exists() else []
    export_key = existing_st[0] if existing_st else (merged / "model.safetensors")
    if markers.is_stage_complete(merged, export_key, export_hash):
        await state.log(f"Export already complete (marker matches) at {merged} — skipping export", "success")
        await state.set_stage("export", StageStatus.COMPLETE)
        await state.set_progress(100)
        return True

    await state.set_stage("export", StageStatus.RUNNING)
    await state.set_progress(0)

    # Determine model source: base model ID for streaming_merge + optional LoRA dir
    if training_enabled:
        base_model_id = cfg.training.model_name
        lora_source = f"{out}/lora_adapters"
        has_lora = True
    elif ec.source_model:
        base_model_id = ec.source_model
        has_lora = (Path(ec.source_model) / "adapter_config.json").exists()
        if has_lora:
            try:
                adapter_cfg_data = json.loads((Path(ec.source_model) / "adapter_config.json").read_text())
                base_model_id = adapter_cfg_data.get("base_model_name_or_path", ec.source_model)
            except (json.JSONDecodeError, OSError):
                pass
            lora_source = ec.source_model
        else:
            lora_source = None
    else:
        await state.log("Export requires a model source. Enable Training or set a Source Model path.", "error")
        await state.set_stage("export", StageStatus.FAILED)
        return False

    # Validate source exists (for local paths)
    if not base_model_id.startswith(("http", "hf://")) and "/" in base_model_id:
        source_path = Path(base_model_id)
        if source_path.is_absolute() and not source_path.exists():
            await state.log(f"Source model not found: {base_model_id}", "error")
            await state.set_stage("export", StageStatus.FAILED)
            return False

    load_desc = "LoRA adapters" if has_lora else "model"
    if mq_enabled:
        desc = f"Merging {load_desc} to safetensors" if has_lora else f"Saving {load_desc} as safetensors for MagicQuant"
    else:
        desc = f"Exporting {load_desc} to safetensors (for GGUF conversion)"
    await state.log(desc, "stage")

    svc = ExportService(FOUNDRY_ROOT, VENV_PYTHON)
    script = svc.build_script(
        base_model_id=base_model_id,
        lora_source=lora_source,
        has_lora=has_lora,
        merged_dir=str(out_abs / "merged_model"),
    )
    rc = await run_script(script, out)
    ok = rc == 0
    if ok and merged.exists():
        st = sorted(merged.glob("*.safetensors"))
        kf = st[0] if st else export_key
        if kf.exists() and kf.stat().st_size > 0:
            try:
                markers.write_marker(merged, "export", kf, export_hash)
            except OSError:
                pass
    await state.set_stage("export", StageStatus.COMPLETE if ok else StageStatus.FAILED)
    if ok:
        await state.set_progress(100)
    return ok


async def do_heretic(cfg: RunRequest) -> bool:
    """Run heretic abliteration on the merged model to remove safety alignment."""
    out = cfg.training.output_dir
    out_abs = _resolve_out(out)
    hc = cfg.heretic

    # Completion-marker resume (audit M-skip-marker).
    heretic_dir = out_abs / "heretic_model"
    heretic_hash = markers.config_hash({
        "n_trials": hc.n_trials, "n_startup_trials": hc.n_startup_trials,
        "quantization": hc.quantization, "kl_divergence_scale": hc.kl_divergence_scale,
        "orthogonalize_direction": hc.orthogonalize_direction,
        "row_normalization": hc.row_normalization,
    })
    existing_h = sorted(heretic_dir.glob("*.safetensors")) if heretic_dir.exists() else []
    heretic_key = existing_h[0] if existing_h else (heretic_dir / "model.safetensors")
    if markers.is_stage_complete(heretic_dir, heretic_key, heretic_hash):
        await state.log(f"Heretic already complete (marker matches) at {heretic_dir} -- skipping heretic", "success")
        await state.set_stage("heretic", StageStatus.COMPLETE)
        await state.set_progress(100)
        return True

    # Determine model source: prefer merged_model from export stage
    merged_dir = out_abs / "merged_model"
    if not merged_dir.exists() or not any(merged_dir.glob("*.safetensors")):
        await state.log("No merged model found -- run export first", "error")
        await state.set_stage("heretic", StageStatus.FAILED)
        return False

    await state.set_stage("heretic", StageStatus.RUNNING)
    await state.set_progress(0)
    await state.log("Starting heretic abliteration (Optuna-optimized directional ablation)", "stage")

    checkpoint_dir = str(out_abs / "_heretic_checkpoints")

    svc = HereticService(FOUNDRY_ROOT, VENV_PYTHON)
    script = svc.build_script(
        model_path=str(merged_dir),
        output_path=str(heretic_dir),
        checkpoint_dir=checkpoint_dir,
        n_trials=hc.n_trials,
        n_startup_trials=hc.n_startup_trials,
        quantization=hc.quantization,
        kl_divergence_scale=hc.kl_divergence_scale,
        orthogonalize_direction=hc.orthogonalize_direction,
        row_normalization=hc.row_normalization,
    )
    rc = await run_script(script, out)
    ok = rc == 0
    if ok and heretic_dir.exists():
        st = sorted(heretic_dir.glob("*.safetensors"))
        kf = st[0] if st else heretic_key
        if kf.exists() and kf.stat().st_size > 0:
            try:
                markers.write_marker(heretic_dir, "heretic", kf, heretic_hash)
            except OSError:
                pass
    await state.set_stage("heretic", StageStatus.COMPLETE if ok else StageStatus.FAILED)
    if ok:
        await state.set_progress(100)
    return ok


# REAP_SUPPORTED_ARCHS and _detect_model_arch are imported from reap_common
# (single source of truth shared with the CLI — audit L-source-dup/L-reap-archlist).


async def do_reap(cfg: RunRequest) -> bool:
    """Run REAP expert pruning on the merged or abliterated model.

    Reads from heretic_model/ (preferred) or merged_model/ and writes to
    reap_model/. Silently skips (returns True) if the architecture is not
    supported by REAP.
    """
    out = cfg.training.output_dir
    out_abs = _resolve_out(out)
    rc = cfg.reap

    # Completion-marker resume (audit M-skip-marker).
    reap_dir = out_abs / "reap_model"
    reap_hash = markers.config_hash({
        "compression_ratio": rc.compression_ratio, "prune_method": rc.prune_method,
        "samples_per_category": rc.samples_per_category,
        "model_max_length": rc.model_max_length, "dataset_name": rc.dataset_name,
        "seed": rc.seed,
    })
    existing_r = sorted(reap_dir.glob("*.safetensors")) if reap_dir.exists() else []
    reap_key = existing_r[0] if existing_r else (reap_dir / "model.safetensors")
    if markers.is_stage_complete(reap_dir, reap_key, reap_hash):
        await state.log(f"REAP already complete (marker matches) at {reap_dir} — skipping REAP", "success")
        await state.set_stage("reap", StageStatus.COMPLETE)
        await state.set_progress(100)
        return True

    # Determine model source: prefer heretic output, fall back to merged_model
    heretic_dir = out_abs / "heretic_model"
    merged_dir = out_abs / "merged_model"
    if heretic_dir.exists() and any(heretic_dir.glob("*.safetensors")):
        source_dir = heretic_dir
        await state.log(f"REAP source: abliterated model at {source_dir}")
    elif merged_dir.exists() and any(merged_dir.glob("*.safetensors")):
        source_dir = merged_dir
        await state.log(f"REAP source: merged model at {source_dir}")
    else:
        await state.log("No merged or abliterated model found -- run export first", "error")
        await state.set_stage("reap", StageStatus.FAILED)
        return False

    # Check architecture support before launching a subprocess.
    arch = _detect_model_arch(source_dir)
    if arch is None:
        await state.log(
            f"Could not detect model architecture from {source_dir}/config.json — skipping REAP",
            "warn",
        )
        await state.set_stage("reap", StageStatus.SKIPPED)
        return True
    if arch not in REAP_SUPPORTED_ARCHS:
        await state.log(
            f"Architecture '{arch}' is not supported by REAP — skipping stage",
            "warn",
        )
        await state.log(f"  REAP supports: {sorted(REAP_SUPPORTED_ARCHS)}")
        await state.set_stage("reap", StageStatus.SKIPPED)
        return True
    await state.log(f"Detected supported REAP architecture: {arch}")

    await state.set_stage("reap", StageStatus.RUNNING)
    await state.set_progress(0)
    await state.log("Starting REAP expert pruning", "stage")

    svc = ReapService(FOUNDRY_ROOT, VENV_PYTHON)
    script = svc.build_script(
        input_dir=str(source_dir.resolve()),
        output_dir=str(reap_dir.resolve()),
        cwd_dir=str(out_abs.resolve()),
        compression_ratio=rc.compression_ratio,
        prune_method=rc.prune_method,
        samples_per_category=rc.samples_per_category,
        model_max_length=rc.model_max_length,
        dataset_name=rc.dataset_name,
        seed=rc.seed,
    )
    rc_code = await run_script(script, out)
    ok = rc_code == 0
    if ok and reap_dir.exists():
        st = sorted(reap_dir.glob("*.safetensors"))
        kf = st[0] if st else reap_key
        if kf.exists() and kf.stat().st_size > 0:
            try:
                markers.write_marker(reap_dir, "reap", kf, reap_hash)
            except OSError:
                pass
    await state.set_stage("reap", StageStatus.COMPLETE if ok else StageStatus.FAILED)
    if ok:
        await state.set_progress(100)
    return ok


def _resolve_qat_config_source(qc: "QATCfg", out_abs: Path) -> Optional[Path]:
    """Resolve the search_results.json the QAT stage targets, or None.

    Explicit ``config_source`` wins (relative paths resolve against the output
    dir / project root); otherwise auto-detect ``<output>/magicquant/search_results.json``.
    """
    if qc.config_source:
        p = Path(qc.config_source)
        if p.is_absolute():
            return p if p.exists() else None
        for base in (out_abs, FOUNDRY_ROOT):
            cand = base / qc.config_source
            if cand.exists():
                return cand
        return None
    auto = out_abs / "magicquant" / "search_results.json"
    return auto if auto.exists() else None


async def do_qat(cfg: RunRequest) -> bool:
    """Run QAT-LoRA: fine-tune adapters robust to the per-group hybrid config."""
    out = cfg.training.output_dir
    out_abs = _resolve_out(out)
    qc = cfg.qat

    if qc is None:
        await state.log("QAT stage enabled but no QAT config provided", "error")
        await state.set_stage("qat", StageStatus.FAILED)
        return False

    config_source = _resolve_qat_config_source(qc, out_abs)
    if config_source is None:
        await state.log(
            "No search_results.json found for QAT. Set a Hybrid Config Source, or "
            "run a MagicQuant search first so its search_results.json exists.",
            "error",
        )
        await state.set_stage("qat", StageStatus.FAILED)
        return False

    if not qc.dataset:
        await state.log("No QAT dataset configured — set a chat JSONL dataset path", "error")
        await state.set_stage("qat", StageStatus.FAILED)
        return False

    # Completion-marker resume (mirrors the other stages).
    qat_dir = out_abs / "qat_adapters"
    qat_hash = markers.config_hash({
        "model": cfg.training.model_name, "config": str(config_source),
        "tier": qc.tier, "dataset": qc.dataset, "lora_r": qc.lora_r,
        "lora_alpha": qc.lora_alpha, "epochs": qc.epochs, "max_steps": qc.max_steps,
        "lr": qc.lr, "max_seq_len": qc.max_seq_len,
    })
    qat_key = qat_dir / "qat_meta.json"
    if markers.is_stage_complete(qat_dir, qat_key, qat_hash):
        await state.log(f"QAT already complete (marker matches) at {qat_dir} — skipping", "success")
        await state.set_stage("qat", StageStatus.COMPLETE)
        await state.set_progress(100)
        return True

    await state.set_stage("qat", StageStatus.RUNNING)
    await state.set_progress(0)
    await state.log(f"Starting QAT-LoRA (hybrid config: {config_source}, tier {qc.tier})", "stage")

    svc = QATService(FOUNDRY_ROOT, VENV_PYTHON)
    script = svc.build_script(
        model=cfg.training.model_name,
        config_path=str(config_source),
        tier=qc.tier,
        dataset=qc.dataset,
        out=str(qat_dir),
        lora_r=qc.lora_r,
        lora_alpha=qc.lora_alpha,
        epochs=qc.epochs,
        max_steps=qc.max_steps,
        lr=qc.lr,
        max_seq_len=qc.max_seq_len,
    )
    rc = await run_script(script, out)
    ok = rc == 0 and qat_key.exists()
    if ok:
        try:
            markers.write_marker(qat_dir, "qat", qat_key, qat_hash)
        except OSError:
            pass
    await state.set_stage("qat", StageStatus.COMPLETE if ok else StageStatus.FAILED)
    if ok:
        await state.set_progress(100)
    return ok


async def do_magicquant(cfg: RunRequest) -> bool:
    """Run MagicQuant evolutionary search and generate tiered hybrid GGUFs."""
    out = cfg.training.output_dir
    out_abs = _resolve_out(out)
    mc = cfg.magicquant
    export_enabled = "export" in cfg.enabled_stages

    # Completion-marker resume (audit M-skip-marker).
    mq_dir = out_abs / "magicquant"
    mq_hash = markers.config_hash({
        "generations": mc.generations, "population_size": mc.population_size,
        "target_base_quant": mc.target_base_quant, "tiers": mc.tiers,
        "source_model": mc.source_model, "measured": mc.measured,
        "measurement_rounds": mc.measurement_rounds, "rocmfpx_schemes": mc.rocmfpx_schemes,
        "iq_schemes": mc.iq_schemes, "seed": mc.seed,
        "use_imatrix": mc.use_imatrix, "imatrix_corpus": mc.imatrix_corpus,
        "enable_kl": mc.enable_kl, "kl_weight": mc.kl_weight,
        "enable_speed_bench": mc.enable_speed_bench,
        "measurement_chunks": mc.measurement_chunks,
        "stream_aware": mc.stream_aware, "head_aggressive": mc.head_aggressive,
        "speed_aware": mc.speed_aware, "speed_metric": mc.speed_metric,
        "speed_weight": mc.speed_weight, "use_bytes_tps": mc.use_bytes_tps,
        "calibration_source": mc.calibration_source,
        "write_calibration": mc.write_calibration,
    })
    existing_ggufs = sorted(mq_dir.glob("*.gguf")) if mq_dir.exists() else []
    mq_key = existing_ggufs[0] if existing_ggufs else (mq_dir / "_placeholder.gguf")
    if markers.is_stage_complete(mq_dir, mq_key, mq_hash):
        await state.log(f"MagicQuant already complete (marker matches) at {mq_dir} — skipping", "success")
        await state.set_stage("magicquant", StageStatus.COMPLETE)
        await state.set_progress(100)
        return True

    await state.set_stage("magicquant", StageStatus.RUNNING)
    await state.set_progress(0)

    await state.log("Starting MagicQuant evolutionary search", "stage")

    tiers_json = json.dumps(mc.tiers)
    model_name = _derive_model_short_name(cfg)
    hint = mc.llamacpp_path or ""
    mq_source_override = mc.source_model if (mc.source_model and not export_enabled) else ""

    svc = MagicQuantService(FOUNDRY_ROOT, VENV_PYTHON)
    script = svc.build_script(
        llamacpp_hint=hint,
        pipeline_root_str=str(FOUNDRY_ROOT),
        mq_source_override=mq_source_override,
        out_abs_str=str(out_abs),
        generations=mc.generations,
        population_size=mc.population_size,
        target_base_quant=mc.target_base_quant,
        tiers_json=tiers_json,
        model_name=model_name,
        verify=False,
        measured=mc.measured,
        measurement_rounds=mc.measurement_rounds,
        rocmfpx_schemes=mc.rocmfpx_schemes,
        iq_schemes=mc.iq_schemes,
        seed=mc.seed,
        use_imatrix=mc.use_imatrix,
        imatrix_corpus=mc.imatrix_corpus,
        enable_kl=mc.enable_kl,
        kl_weight=mc.kl_weight,
        enable_speed_bench=mc.enable_speed_bench,
        measurement_chunks=mc.measurement_chunks,
        stream_aware=mc.stream_aware,
        head_aggressive=mc.head_aggressive,
        speed_aware=mc.speed_aware,
        speed_metric=mc.speed_metric,
        speed_weight=mc.speed_weight,
        use_bytes_tps=mc.use_bytes_tps,
        calibration_source=mc.calibration_source,
        write_calibration=mc.write_calibration,
    )
    rc = await run_script(script, out)
    ok = rc == 0
    if ok and mq_dir.exists():
        ggufs = sorted(mq_dir.glob("*.gguf"))
        if ggufs:
            try:
                markers.write_marker(mq_dir, "magicquant", ggufs[0], mq_hash)
            except OSError:
                pass
    await state.set_stage("magicquant", StageStatus.COMPLETE if ok else StageStatus.FAILED)
    if ok:
        await state.set_progress(100)
    return ok


async def do_rocmfpx(cfg: RunRequest) -> bool:
    """Run ROCmFPX quantization (AMD-tuned GGUF quant family)."""
    out = cfg.training.output_dir
    out_abs = _resolve_out(out)
    rc_cfg = cfg.rocmfpx
    export_enabled = "export" in cfg.enabled_stages

    if rc_cfg is None:
        await state.log("ROCmFPX stage enabled but no ROCmFPX config provided", "error")
        await state.set_stage("rocmfpx", StageStatus.FAILED)
        return False

    # Completion-marker resume (mirrors do_magicquant).
    rc_dir = out_abs / "rocmfpx"
    rc_hash = markers.config_hash({
        "formats": rc_cfg.formats, "imatrix": rc_cfg.imatrix,
        "source_model": rc_cfg.source_model,
    })
    existing_ggufs = sorted(rc_dir.glob("*.gguf")) if rc_dir.exists() else []
    rc_key = existing_ggufs[0] if existing_ggufs else (rc_dir / "_placeholder.gguf")
    if markers.is_stage_complete(rc_dir, rc_key, rc_hash):
        await state.log(f"ROCmFPX already complete (marker matches) at {rc_dir} — skipping", "success")
        await state.set_stage("rocmfpx", StageStatus.COMPLETE)
        await state.set_progress(100)
        return True

    await state.set_stage("rocmfpx", StageStatus.RUNNING)
    await state.set_progress(0)
    await state.log("Starting ROCmFPX quantization", "stage")

    model_name = _derive_model_short_name(cfg)
    source_override = rc_cfg.source_model if (rc_cfg.source_model and not export_enabled) else ""

    svc = ROCmFPXService(FOUNDRY_ROOT, VENV_PYTHON)
    script = svc.build_script(
        rocmfpx_hint=rc_cfg.rocmfpx_hint,
        pipeline_root_str=str(FOUNDRY_ROOT),
        source_override=source_override,
        out_abs_str=str(out_abs),
        formats_json=json.dumps(rc_cfg.formats),
        model_name=model_name,
        imatrix=rc_cfg.imatrix,
    )
    rc = await run_script(script, out)
    ok = rc == 0
    if ok and rc_dir.exists():
        ggufs = sorted(rc_dir.glob("*.gguf"))
        if ggufs:
            try:
                markers.write_marker(rc_dir, "rocmfpx", ggufs[0], rc_hash)
            except OSError:
                pass
    await state.set_stage("rocmfpx", StageStatus.COMPLETE if ok else StageStatus.FAILED)
    if ok:
        await state.set_progress(100)
    return ok


async def do_upload(cfg: RunRequest) -> bool:
    """Upload pipeline artifacts (GGUF, LoRA, merged) to HuggingFace Hub.

    Delegates to hf_upload module for model card generation, progress reporting,
    and file upload.
    """
    out = cfg.training.output_dir
    uc = cfg.upload
    await state.set_stage("upload", StageStatus.RUNNING)
    await state.set_progress(0)
    await state.log("Uploading to HuggingFace Hub", "stage")

    if not uc or not uc.repo_id:
        await state.log("No repo_id configured", "error")
        await state.set_stage("upload", StageStatus.FAILED)
        return False

    tc = cfg.training
    out_abs = _resolve_out(out)

    enabled = set(cfg.enabled_stages)
    # Auto-detect license from base model if not explicitly set
    license_id = uc.license
    if not license_id or license_id == "auto":
        await state.log("Detecting license from base model...")
        try:
            from huggingface_hub import model_info as _mi
            _info = _mi(tc.model_name)
            for _tag in (_info.tags or []):
                if _tag.startswith("license:"):
                    license_id = _tag.split(":", 1)[1]
                    break
            if not license_id or license_id == "auto":
                license_id = getattr(getattr(_info, 'card_data', None), 'license', '') or "unknown"
            await state.log(f"  License: {license_id}")
        except Exception as e:
            license_id = "unknown"
            await state.log(f"  Could not detect license: {e}", "warn")

    svc = UploadService(FOUNDRY_ROOT, VENV_PYTHON)
    script = svc.build_script(
        repo_id=uc.repo_id,
        private=uc.private,
        license_id=license_id,
        upload_gguf=uc.upload_gguf,
        upload_lora=uc.upload_lora,
        upload_merged=uc.upload_merged,
        upload_dataset=uc.upload_dataset,
        base_model=tc.model_name,
        dataset_name=tc.datasets[0] if tc.datasets else "",
        did_training="training" in enabled,
        did_heretic="heretic" in enabled,
        did_reap="reap" in enabled,
        did_magicquant="magicquant" in enabled,
        lora_r=tc.lora_r,
        lora_alpha=tc.lora_alpha,
        lora_dropout=tc.lora_dropout,
        num_epochs=tc.num_train_epochs,
        learning_rate=tc.learning_rate,
        max_seq_length=tc.max_seq_length,
        batch_size=tc.per_device_train_batch_size,
        gradient_accumulation=tc.gradient_accumulation_steps,
        optimizer=tc.optim,
        lr_scheduler=tc.lr_scheduler_type,
        out_abs=str(out_abs),
    )
    rc = await run_script(script, out, inject_hf_token=True)
    ok = rc == 0
    await state.set_stage("upload", StageStatus.COMPLETE if ok else StageStatus.FAILED)
    if ok:
        await state.set_progress(100)
    return ok


# ── Pipeline orchestration ───────────────────────────────────────────────────

STAGE_RUNNERS = {
    "training":   do_training,
    "export":     do_export,
    "heretic":    do_heretic,
    "reap":       do_reap,
    "qat":        do_qat,
    "magicquant": do_magicquant,
    "rocmfpx":    do_rocmfpx,
    "upload":     do_upload,
}

async def validate_pipeline(cfg: RunRequest) -> bool:
    """Pre-flight checks for stage dependencies."""
    enabled = set(cfg.enabled_stages)
    out_abs = _resolve_out(cfg.training.output_dir)

    # Export without training: needs a source model
    if "export" in enabled and "training" not in enabled:
        source = cfg.export.source_model if cfg.export else ""
        if not source:
            await state.log("Export is enabled without Training, but no Source Model is set. "
                            "Provide a HuggingFace model ID or local path in the Export config.", "error")
            return False
        await state.log(f"Training skipped — Export will use source model: {source}")

    # Heretic without export: needs merged model from prior run
    if "heretic" in enabled and "export" not in enabled:
        has_merged = (out_abs / "merged_model").exists()
        if not has_merged:
            await state.log("Heretic is enabled without Export, and no merged model was found "
                            "in the output directory. Enable Export to produce a merged model first.", "error")
            return False
        await state.log(f"Export skipped -- Heretic will use existing: {out_abs}/merged_model")

    # REAP without export/heretic: needs a merged or abliterated model from prior run
    if "reap" in enabled and "export" not in enabled and "heretic" not in enabled:
        has_heretic = (out_abs / "heretic_model").exists()
        has_merged = (out_abs / "merged_model").exists()
        if not has_heretic and not has_merged:
            await state.log("REAP is enabled without Export or Heretic, and no merged or abliterated "
                            "model was found in the output directory. Enable an upstream stage or run "
                            "a pipeline to produce a safetensors model first.", "error")
            return False
        target = "heretic_model" if has_heretic else "merged_model"
        await state.log(f"Upstream stages skipped — REAP will use existing: {out_abs}/{target}")

    # MagicQuant without export: needs a source
    if "magicquant" in enabled and "export" not in enabled:
        mc = cfg.magicquant
        source = mc.source_model if mc else ""
        # Check if prior pipeline output exists
        has_reap = (out_abs / "reap_model").exists()
        has_heretic = (out_abs / "heretic_model").exists()
        has_merged = (out_abs / "merged_model").exists()
        has_gguf = (out_abs / "model-bf16.gguf").exists()
        if not source and not has_reap and not has_heretic and not has_merged and not has_gguf:
            await state.log("MagicQuant is enabled without Export, and no existing model artifacts "
                            "were found in the output directory. Set a Source Model path in MagicQuant "
                            "config, or enable Export.", "error")
            return False
        if source:
            await state.log(f"Export skipped — MagicQuant will use source: {source}")
        elif has_reap:
            await state.log(f"Export skipped — MagicQuant will use existing: {out_abs}/reap_model")
        elif has_heretic:
            await state.log(f"Export skipped — MagicQuant will use existing: {out_abs}/heretic_model")
        elif has_merged:
            await state.log(f"Export skipped — MagicQuant will use existing: {out_abs}/merged_model")
        else:
            await state.log(f"Export skipped — MagicQuant will use existing: {out_abs}/model-bf16.gguf")

    # ROCmFPX without export: needs a source (same check as MagicQuant)
    if "rocmfpx" in enabled and "export" not in enabled:
        rc_cfg = cfg.rocmfpx
        source = rc_cfg.source_model if rc_cfg else ""
        has_reap = (out_abs / "reap_model").exists()
        has_heretic = (out_abs / "heretic_model").exists()
        has_merged = (out_abs / "merged_model").exists()
        has_gguf = (out_abs / "model-bf16.gguf").exists()
        if not source and not has_reap and not has_heretic and not has_merged and not has_gguf:
            await state.log("ROCmFPX is enabled without Export, and no existing model artifacts "
                            "were found in the output directory. Set a Source Model path in ROCmFPX "
                            "config, or enable Export.", "error")
            return False
        if source:
            await state.log(f"Export skipped — ROCmFPX will use source: {source}")
        elif has_reap:
            await state.log(f"Export skipped — ROCmFPX will use existing: {out_abs}/reap_model")
        elif has_heretic:
            await state.log(f"Export skipped — ROCmFPX will use existing: {out_abs}/heretic_model")
        elif has_merged:
            await state.log(f"Export skipped — ROCmFPX will use existing: {out_abs}/merged_model")
        else:
            await state.log(f"Export skipped — ROCmFPX will use existing: {out_abs}/model-bf16.gguf")

    # Upload: check that at least some artifacts will exist
    if "upload" in enabled:
        uc = cfg.upload
        if not uc or not uc.repo_id:
            await state.log("Upload is enabled but Repository ID is empty.", "error")
            return False

    return True


def _derive_model_short_name(cfg: RunRequest) -> str:
    """Extract a short model name from the first available source across stages."""
    enabled = set(cfg.enabled_stages)
    if "training" in enabled:
        raw = cfg.training.model_name
    elif "export" in enabled and cfg.export and cfg.export.source_model:
        raw = cfg.export.source_model
    elif "magicquant" in enabled and cfg.magicquant and cfg.magicquant.source_model:
        raw = cfg.magicquant.source_model
    else:
        raw = cfg.training.model_name
    # Strip org/user prefix, path components, and known model file extensions
    name = raw.rstrip("/").split("/")[-1]
    for ext in (".gguf", ".safetensors", ".bin", ".pt", ".pth"):
        if name.lower().endswith(ext):
            name = name[:-len(ext)]
            break
    # Sanitize for filesystem
    return "".join(c if c.isalnum() or c in "-_." else "-" for c in name).strip("-") or "model"


async def run_pipeline(cfg: RunRequest):
    """Execute enabled pipeline stages in order: training, export, magicquant, upload."""
    state.running = True
    state.current_stage = None
    state.progress = 0
    enabled = set(cfg.enabled_stages)

    # Create model-specific output subdirectory (avoid nested dirs on re-run)
    model_name = _derive_model_short_name(cfg)
    base_out = cfg.training.output_dir
    if not base_out.rstrip("/").endswith(f"/{model_name}") and Path(base_out).name != model_name:
        cfg.training.output_dir = f"{base_out}/{model_name}"
    out_abs = _resolve_out(cfg.training.output_dir)
    out_abs.mkdir(parents=True, exist_ok=True)
    await state.log(f"Output directory: {out_abs}", "info")

    for s in ALL_STAGES:
        await state.set_stage(s, StageStatus.SKIPPED if s not in enabled else StageStatus.PENDING)

    try:
        if not await validate_pipeline(cfg):
            await state.log("Pipeline aborted due to validation errors.", "error")
            return

        for stage_name in ALL_STAGES:
            if stage_name not in enabled:
                continue
            if not state.running:
                await state.log("Pipeline stopped by user", "warn")
                break
            ok = await STAGE_RUNNERS[stage_name](cfg)
            if not ok:
                await state.log(f"Pipeline stopped at {stage_name}", "error")
                break

        if all(state.stages[s] in (StageStatus.COMPLETE, StageStatus.SKIPPED) for s in ALL_STAGES):
            await state.log("Pipeline complete!", "success")
    except Exception as e:
        await state.log(f"Pipeline error: {e}", "error")
    finally:
        state.running = False
        await state.broadcast({"type": "pipeline_done"})


# ── Flywheel runner ──────────────────────────────────────────────────────────
#
# The flywheel is GPU-bound and takes the SAME _pipeline_lock / state.running as
# the Foundry pipeline, so the two are mutually exclusive (one active GPU job).
# It runs as a subprocess in the flywheel repo, streaming progress to the same
# WebSocket via new message types (flywheel_stage, flywheel_done) plus the
# shared log stream. Its subprocess is set as state.active_proc so /api/stop
# works for free, and its own gpu_lock.py serializes inference at the OS level.

# Static driver: reads its config from $FLYWHEEL_PARAMS (data, never interpolated
# into source), builds a pre-built-mode IterationConfig, wraps the real ToolSuite
# to emit per-stage @@FLYWHEEL@@ markers, and runs one real iteration.
_FLYWHEEL_DRIVER = r'''
import sys, os, json, traceback

_repo = os.environ.get("FLYWHEEL_REPO", "")
if _repo and _repo not in sys.path:
    sys.path.insert(0, _repo)

from flywheel import (
    ArmConfig, DecisionPolicy, EvalConfig, IterationConfig, JudgeConfig,
    SignalConfig, StageFailure, StatsConfig, run_iteration,
)
from flywheel.loop import ToolSuite, default_suite


def emit(obj):
    print("@@FLYWHEEL@@ " + json.dumps(obj), flush=True)


with open(os.environ["FLYWHEEL_PARAMS"]) as f:
    p = json.load(f)

base_model = p["base_model"]
candidate_model = p.get("candidate_model") or base_model
aa = candidate_model == base_model
families = tuple(f.strip() for f in p["families"].split(",") if f.strip())
seeds = tuple(int(s) for s in str(p["seeds"]).split(",") if str(s).strip())
checkers = tuple((name, spec) for name, spec in p.get("checkers", []))

policy_kw = {}
if p.get("min_macro_delta") is not None:
    policy_kw["min_macro_delta"] = float(p["min_macro_delta"])
if p.get("veredict_floor") is not None:
    policy_kw["veredict_floor"] = float(p["veredict_floor"])

cfg = IterationConfig(
    name=p.get("name", "flywheel-ui"),
    workdir=p["workdir"],
    baseline=ArmConfig(label=base_model + " (base arm)", model_id=base_model),
    candidate=ArmConfig(
        label=candidate_model + " (candidate arm"
              + (", A/A control" if aa else "") + ")",
        model_id=candidate_model),
    signal=SignalConfig(families=families, n_tasks=int(p["n_tasks"]),
                        difficulty=float(p["difficulty"]), seed_base=1000),
    eval=EvalConfig(base_url=p["base_url"], seeds=seeds,
                    max_tokens=int(p["max_tokens"]),
                    gpu_lock_timeout_s=float(p.get("gpu_lock_timeout_s", 5400.0))),
    judge=JudgeConfig(checkers=checkers),
    stats=StatsConfig(bootstrap_iters=int(p.get("bootstrap_iters", 2000))),
    policy=DecisionPolicy(**policy_kw),
)

emit({"event": "start", "aa_control": aa, "base_model": base_model,
      "candidate_model": candidate_model, "families": list(families),
      "n_tasks": int(p["n_tasks"]), "seeds": list(seeds)})
# pre-built candidate: train + quantize are skipped
emit({"event": "stage", "stage": "train", "status": "skipped"})
emit({"event": "stage", "stage": "quantize", "status": "skipped"})

_base = default_suite()


def _wrap(name, fn):
    def inner(*a, **k):
        emit({"event": "stage", "stage": name, "status": "running"})
        out = fn(*a, **k)
        emit({"event": "stage", "stage": name, "status": "success"})
        return out
    return inner


suite = ToolSuite(
    train=_base.train,
    quantize=_base.quantize,
    generate_tasks=_wrap("signal", _base.generate_tasks),
    rollout=_wrap("rollout", _base.rollout),
    judge=_wrap("judge", _base.judge),
    gate=_wrap("gate", _base.gate),
)

try:
    manifest = run_iteration(cfg, suite=suite, log=lambda m: print(m, flush=True))
except StageFailure as e:
    mp = os.path.join(e.manifest._dir, "iteration.json")
    emit({"event": "stage", "stage": e.stage, "status": "failed"})
    emit({"event": "error", "stage": e.stage, "error": str(e.cause),
          "manifest_path": mp})
    print("STAGE FAILED: %s: %s" % (e.stage, e.cause), flush=True)
    sys.exit(1)
except Exception as e:
    traceback.print_exc()
    emit({"event": "error", "error": "%s: %s" % (type(e).__name__, e)})
    sys.exit(1)

emit({"event": "stage", "stage": "decide", "status": "running"})
emit({"event": "stage", "stage": "decide", "status": "success"})
mp = os.path.join(manifest._dir, "iteration.json")
emit({"event": "manifest", "manifest_path": mp,
      "iteration_id": manifest.iteration_id})
print("[flywheel] manifest: " + mp, flush=True)
'''


async def _run_flywheel_subprocess(script_path: Path, params_path: Path):
    """Run the flywheel driver, streaming stdout to WS clients.

    Returns (returncode, manifest_path, error). Sets state.active_proc so
    /api/stop can kill it. Never injects the HF token (the flywheel never
    uploads).
    """
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    env["FLYWHEEL_REPO"] = str(FLYWHEEL_ROOT)
    env["FLYWHEEL_PARAMS"] = str(params_path)
    existing_pp = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = str(FLYWHEEL_ROOT) + (os.pathsep + existing_pp if existing_pp else "")
    env.pop("HF_TOKEN", None)

    py = _resolve_flywheel_python()
    manifest_path = None
    err = None
    log_file = open(script_path.with_suffix(".log"), "w")
    try:
        proc = await asyncio.create_subprocess_exec(
            py, "-u", str(script_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=env, cwd=str(FLYWHEEL_ROOT),
            limit=1024 * 1024,
            start_new_session=True,
        )
        state.active_proc = proc
        try:
            async for raw in proc.stdout:
                decoded = raw.decode("utf-8", errors="replace")
                log_file.write(decoded)
                log_file.flush()
                for line in decoded.split("\r"):
                    line = line.rstrip("\n").strip()
                    if not line:
                        continue
                    if line.startswith("@@FLYWHEEL@@ "):
                        try:
                            ev = json.loads(line[len("@@FLYWHEEL@@ "):])
                        except json.JSONDecodeError:
                            continue
                        kind = ev.get("event")
                        if kind == "stage":
                            await state.broadcast({"type": "flywheel_stage",
                                                   "stage": ev["stage"],
                                                   "status": ev["status"]})
                            if ev["status"] in ("running", "failed"):
                                await state.log(
                                    f"[flywheel] stage {ev['stage']}: {ev['status']}",
                                    "stage" if ev["status"] == "running" else "error")
                        elif kind == "start":
                            mode = "A/A control" if ev.get("aa_control") else "A/B"
                            await state.log(
                                f"[flywheel] {mode} — base={ev['base_model']} "
                                f"candidate={ev['candidate_model']} "
                                f"families={ev['families']} n_tasks={ev['n_tasks']} "
                                f"seeds={ev['seeds']}", "stage")
                        elif kind == "manifest":
                            manifest_path = ev.get("manifest_path")
                        elif kind == "error":
                            err = ev.get("error")
                            manifest_path = ev.get("manifest_path") or manifest_path
                            await state.log(f"[flywheel] error: {err}", "error")
                    else:
                        lvl = ("error" if ("Traceback" in line or "Error" in line
                                           or "error" in line.lower()) else "info")
                        await state.log(line, lvl)
        except Exception:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            raise
        finally:
            await proc.wait()
            state.active_proc = None
    finally:
        log_file.close()
    return proc.returncode, manifest_path, err


async def run_flywheel(req: FlywheelRequest):
    """Drive one flywheel iteration and broadcast the final verdict + decision."""
    state.current_stage = None
    state.progress = 0
    workdir = FLYWHEEL_ROOT / "output" / "ui"
    try:
        # Reset the flywheel stage tracker for the new run.
        for stg, status in (("train", "skipped"), ("quantize", "skipped"),
                            ("signal", "pending"), ("rollout", "pending"),
                            ("judge", "pending"), ("gate", "pending"),
                            ("decide", "pending")):
            await state.broadcast({"type": "flywheel_stage", "stage": stg,
                                   "status": status})

        workdir.mkdir(parents=True, exist_ok=True)
        fams = [f.strip() for f in req.families.split(",") if f.strip()]
        # math_logic secondary contract mirrors scripts/demo.py (verified live
        # against veredict.checkers.regex_contract).
        checkers = ([["regex_contract", {"must_match": [r"Answer:\s*-?\d+"]}]]
                    if "math_logic" in fams else [])
        ts = int(time.time())
        script_path = workdir / f"_flywheel_{ts}.py"
        params_path = workdir / f"_flywheel_{ts}.params.json"
        params = {
            "name": req.name,
            "workdir": str(workdir),
            "base_model": req.base_model,
            "candidate_model": req.candidate_model,
            "families": req.families,
            "n_tasks": req.n_tasks,
            "difficulty": req.difficulty,
            "seeds": req.seeds,
            "max_tokens": req.max_tokens,
            "base_url": req.base_url,
            "bootstrap_iters": req.bootstrap_iters,
            "gpu_lock_timeout_s": req.gpu_lock_timeout_s,
            "min_macro_delta": req.min_macro_delta,
            "veredict_floor": req.veredict_floor,
            "checkers": checkers,
        }
        params_path.write_text(json.dumps(params, indent=2))
        script_path.write_text(_FLYWHEEL_DRIVER)

        await state.log(f"Flywheel: interpreter {_resolve_flywheel_python()}", "info")
        await state.log(f"Flywheel: workdir {workdir}", "info")
        await state.log("Flywheel: acquiring GPU lock /tmp/claude-gpu.lock "
                        "(waits politely if contended)", "info")

        rc, manifest_path, err = await _run_flywheel_subprocess(script_path, params_path)

        if rc == 0 and manifest_path and Path(manifest_path).exists():
            data = json.loads(Path(manifest_path).read_text())
            decision = data.get("decision") or {}
            action = (decision.get("action") or "").upper()
            await state.log(f"Flywheel decision: {action} "
                            f"(promoted: {decision.get('promoted')})", "success")
            await state.broadcast({
                "type": "flywheel_done", "ok": True,
                "verdict": data.get("verdict"),
                "decision": decision,
                "manifest_path": manifest_path,
                "iteration_id": data.get("iteration_id"),
            })
        else:
            await state.broadcast({
                "type": "flywheel_done", "ok": False,
                "error": err or f"flywheel exited with code {rc}",
                "manifest_path": manifest_path,
            })
    except Exception as e:
        await state.log(f"Flywheel error: {e}", "error")
        await state.broadcast({"type": "flywheel_done", "ok": False,
                               "error": str(e)})
    finally:
        state.running = False
        state.active_proc = None
        await state.broadcast({"type": "pipeline_done"})


# ── Persistent config ────────────────────────────────────────────────────────

CONFIG_PATH = Path(__file__).parent / "config.json"

def load_config() -> dict:
    """Load persisted UI config from config.json, or return empty dict on failure."""
    if CONFIG_PATH.exists():
        try:
            return json.loads(CONFIG_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {}

def save_config(cfg: dict):
    """Persist UI config to config.json."""
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2))


# ── Routes ───────────────────────────────────────────────────────────────────

@app.get("/health")
async def health_check():
    """Health check endpoint -- no authentication required."""
    return {"status": "ok", "auth_enabled": bool(API_KEY)}


@app.get("/", response_class=HTMLResponse)
async def index():
    """Serve the single-page frontend HTML. No auth required -- the JS frontend handles auth."""
    return FileResponse(Path(__file__).parent / "index.html")


@app.get("/api/state", dependencies=[Depends(verify_api_key)])
async def get_state():
    """Return the current pipeline state: stage statuses, running flag, and progress."""
    return {
        "stages": {k: v.value for k, v in state.stages.items()},
        "running": state.running,
        "current_stage": state.current_stage,
        "progress": state.progress,
    }


@app.get("/api/config", dependencies=[Depends(verify_api_key)])
async def get_config():
    """Return the persisted UI configuration (e.g. HuggingFace username)."""
    return load_config()


@app.post("/api/config", dependencies=[Depends(verify_api_key)])
async def set_config(body: dict):
    """Merge and persist UI configuration values. Returns the updated config.

    The incoming body is validated against UIConfig (extra='forbid'), so only
    known keys are persisted and unexpected keys are rejected with 422.
    """
    try:
        validated = UIConfig(**body)
    except ValidationError as e:
        raise HTTPException(status_code=422, detail=e.errors())
    cfg = load_config()
    # Only persist fields explicitly provided in the request.
    cfg.update(validated.model_dump(exclude_unset=True))
    save_config(cfg)
    return cfg


# ── Workflow save/restore ───────────────────────────────────────────────────

WORKFLOW_DIR = Path.home() / ".foundry" / "workflows"
_WORKFLOW_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]+$")


@app.get("/api/workflows", dependencies=[Depends(verify_api_key)])
async def list_workflows():
    """List saved workflow names."""
    WORKFLOW_DIR.mkdir(parents=True, exist_ok=True)
    files = sorted(WORKFLOW_DIR.glob("*.json"))
    return {"workflows": [f.stem for f in files]}


@app.post("/api/workflows/{name}", dependencies=[Depends(verify_api_key)])
async def save_workflow(name: str, cfg: RunRequest):
    """Save current config as a named workflow."""
    if not _WORKFLOW_NAME_RE.match(name):
        raise HTTPException(status_code=400, detail="Invalid workflow name. Use alphanumeric, hyphens, and underscores only.")
    WORKFLOW_DIR.mkdir(parents=True, exist_ok=True)
    data = {
        "version": 1,
        "name": name,
        "saved_at": time.time(),
        "enabled_stages": cfg.enabled_stages,
        "config": cfg.model_dump(),
    }
    path = WORKFLOW_DIR / f"{name}.json"
    path.write_text(json.dumps(data, indent=2))
    return {"status": "saved", "name": name}


@app.get("/api/workflows/{name}", dependencies=[Depends(verify_api_key)])
async def load_workflow(name: str):
    """Load a saved workflow."""
    if not _WORKFLOW_NAME_RE.match(name):
        raise HTTPException(status_code=400, detail="Invalid workflow name.")
    path = WORKFLOW_DIR / f"{name}.json"
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"Workflow '{name}' not found")
    data = json.loads(path.read_text())
    return data


@app.delete("/api/workflows/{name}", dependencies=[Depends(verify_api_key)])
async def delete_workflow(name: str):
    """Delete a saved workflow."""
    if not _WORKFLOW_NAME_RE.match(name):
        raise HTTPException(status_code=400, detail="Invalid workflow name.")
    path = WORKFLOW_DIR / f"{name}.json"
    if path.exists():
        path.unlink()
    return {"status": "deleted", "name": name}


# ── Run History ──────────────────────────────────────────────────────────────

@app.get("/api/runs", dependencies=[Depends(verify_api_key)])
async def list_runs():
    """List all pipeline output directories with their stage logs."""
    output_dir = FOUNDRY_DIR / "output"
    if not output_dir.exists():
        return {"runs": []}

    # Detect which output dir the active pipeline is writing to
    active_dir = None
    if state.running and state.active_proc and state.active_proc.returncode is None:
        # Find the most recently modified _stage_*.log across all output dirs
        try:
            all_logs = sorted(output_dir.rglob("_stage_*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
            if all_logs:
                active_dir = all_logs[0].parent.name
        except Exception:
            pass

    runs = []
    for model_dir in sorted(output_dir.iterdir(), key=lambda d: d.stat().st_mtime, reverse=True):
        if not model_dir.is_dir():
            continue
        logs = []
        for log_file in sorted(model_dir.glob("_stage_*.log"), reverse=True):
            logs.append({
                "name": log_file.name,
                "size": log_file.stat().st_size,
                "modified": log_file.stat().st_mtime,
                "live": state.running and model_dir.name == active_dir and log_file == sorted(model_dir.glob("_stage_*.log"), key=lambda p: p.stat().st_mtime)[-1],
            })
        # Also check for magicquant subdir logs
        mq_dir = model_dir / "magicquant"
        ggufs = []
        if mq_dir.exists():
            for gguf in mq_dir.glob("*.gguf"):
                ggufs.append({"name": gguf.name, "size_gb": round(gguf.stat().st_size / 1e9, 1)})

        # Check what artifacts exist
        has_lora = (model_dir / "lora_adapters" / "adapter_config.json").exists()
        has_merged = (model_dir / "merged_model").exists() and any((model_dir / "merged_model").glob("*.safetensors"))

        runs.append({
            "model": model_dir.name,
            "path": str(model_dir),
            "logs": logs,
            "ggufs": ggufs,
            "has_lora": has_lora,
            "has_merged": has_merged,
            "active": model_dir.name == active_dir,
        })
    return {"runs": runs, "pipeline_running": state.running}


@app.get("/api/runs/{model}/{logfile}", dependencies=[Depends(verify_api_key)])
async def get_run_log(model: str, logfile: str):
    """Read a specific log file from a run."""
    # Validate names to prevent path traversal
    if not re.match(r'^[\w\-.]+$', model) or not re.match(r'^_stage_\d+\.log$', logfile):
        raise HTTPException(status_code=400, detail="Invalid name")

    path = FOUNDRY_DIR / "output" / model / logfile
    if not path.exists():
        raise HTTPException(status_code=404, detail="Log not found")

    # Return last 2000 lines if file is huge, or full content if reasonable
    content = path.read_text(errors="replace")
    lines = content.split("\n")
    return {
        "model": model,
        "logfile": logfile,
        "total_lines": len(lines),
        "content": content if len(content) < 500_000 else "\n".join(lines[-2000:]),
        "truncated": len(content) >= 500_000,
    }


@app.get("/api/serve-command/{model}", dependencies=[Depends(verify_api_key)])
async def get_serve_command(model: str):
    """Recommended llama-server invocation for each GGUF a run produced.

    Read-only: reports whether each GGUF auto-enables MTP speculative
    decoding (~1.7x measured generation speedup) and the exact command to
    serve it optimally on this box.
    """
    if not re.match(r'^[\w\-.]+$', model):
        raise HTTPException(status_code=400, detail="Invalid name")

    model_dir = FOUNDRY_DIR / "output" / model
    if not model_dir.exists():
        raise HTTPException(status_code=404, detail="Run not found")

    ggufs = sorted(model_dir.glob("magicquant/*.gguf")) + sorted(model_dir.glob("rocmfpx/*.gguf"))
    commands = [
        {
            "name": gguf.name,
            "mtp": detect_mtp(str(gguf)),
            "command": format_serve_command(build_serve_command(str(gguf))),
        }
        for gguf in ggufs
    ]
    return {"model": model, "commands": commands}


_pipeline_lock = asyncio.Lock()


@app.post("/api/run", dependencies=[Depends(verify_api_key)])
async def start_pipeline(cfg: RunRequest):
    """
    Launch the pipeline in a background task.

    Accepts a full RunRequest with per-stage config and an enabled_stages list.
    Returns an error if a pipeline is already in progress.
    Uses an asyncio.Lock to prevent race conditions from rapid concurrent requests.
    """
    _assert_output_dir_contained(cfg.training.output_dir)
    if _pipeline_lock.locked():
        return {"error": "Pipeline is already running"}
    async with _pipeline_lock:
        if state.running:
            return {"error": "Pipeline is already running"}
        state.running = True
        for s in ALL_STAGES:
            state.stages[s] = StageStatus.PENDING
        state.progress = 0
        asyncio.create_task(run_pipeline(cfg))
    return {"status": "started"}


@app.post("/api/stop", dependencies=[Depends(verify_api_key)])
async def stop_pipeline():
    """Request a graceful pipeline stop. Kills active subprocess and sets running flag to False."""
    if not state.running:
        return {"error": "Pipeline is not running"}
    state.running = False
    if state.active_proc and state.active_proc.returncode is None:
        try:
            os.killpg(os.getpgid(state.active_proc.pid), signal.SIGTERM)
        except (ProcessLookupError, OSError):
            pass
    await state.log("Stop requested by user", "warn")
    await state.broadcast({"type": "pipeline_done"})
    return {"status": "stopping"}


@app.post("/api/flywheel", dependencies=[Depends(verify_api_key)])
async def start_flywheel(req: FlywheelRequest):
    """Launch one foundry-flywheel iteration (pre-built mode) in the background.

    Shares _pipeline_lock / state.running with the Foundry pipeline so the two
    are mutually exclusive (one active GPU job). Streams progress over the same
    WebSocket via flywheel_stage / flywheel_done messages; the subprocess is set
    as state.active_proc so /api/stop halts it.
    """
    if req.n_tasks < 2:
        return {"error": "n_tasks must be >= 2 (paired stats need at least 2 tasks)"}
    if not [f for f in req.families.split(",") if f.strip()]:
        return {"error": "families must be non-empty"}
    if not [s for s in req.seeds.split(",") if s.strip()]:
        return {"error": "seeds must be non-empty"}
    if _pipeline_lock.locked():
        return {"error": "A job is already running"}
    async with _pipeline_lock:
        if state.running:
            return {"error": "A job is already running"}
        state.running = True
        state.progress = 0
        asyncio.create_task(run_flywheel(req))
    return {"status": "started"}


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket, token: str = Query(default="")):
    """
    WebSocket endpoint for real-time log streaming.

    Clients connect here to receive log lines, stage updates, and progress
    events as JSON messages. The connection stays open until the client
    disconnects.

    Authentication is via the ``token`` query parameter (e.g. ``/ws?token=...``).
    """
    if API_KEY and not hmac.compare_digest(token, API_KEY):
        await ws.close(code=4001, reason="Invalid API key")
        return
    if not API_KEY and REQUIRE_AUTH:
        await ws.close(code=4001, reason="Authentication required")
        return
    await ws.accept()
    state.ws_clients.append(ws)
    try:
        while True:
            await ws.receive_text()
    except (WebSocketDisconnect, ConnectionResetError, RuntimeError, Exception):
        if ws in state.ws_clients:
            state.ws_clients.remove(ws)


def select_host(requested: Optional[str], api_key: str, require_auth: bool = False):
    """Decide the uvicorn bind host.

    Default is loopback (127.0.0.1). Binding to a non-loopback address (e.g.
    0.0.0.0) is only allowed when an API key is set (or auth is otherwise
    required); otherwise we refuse to expose an unauthenticated, shell-equivalent
    endpoint on the network. Returns the host string.

    Raises SystemExit when a non-loopback bind is requested without auth.
    """
    host = requested or "127.0.0.1"
    loopback = host in ("127.0.0.1", "::1", "localhost")
    if not loopback and not (api_key or require_auth):
        raise SystemExit(
            f"Refusing to bind {host} without authentication: running the "
            "pipeline grants shell-equivalent host access. Set FOUNDRY_API_KEY "
            "(and optionally FOUNDRY_REQUIRE_AUTH=1), or bind 127.0.0.1."
        )
    return host


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("FOUNDRY_UI_PORT", foundry_settings.ui_port))
    host = select_host(os.environ.get("FOUNDRY_UI_HOST"), API_KEY, REQUIRE_AUTH)
    if host not in ("127.0.0.1", "::1", "localhost"):
        print(f"WARNING: binding {host} — the pipeline grants shell-equivalent host access; "
              "ensure the API key and network access are controlled.")
    uvicorn.run(app, host=host, port=port)
