"""
Foundry pipeline: Training → Export → Heretic → MagicQuant → HF Upload.

Uses custom fast loaders to avoid single-threaded safetensors chunking
that stalls on AMD APU unified memory (128 GB GTT).

Key design decisions:
  - Training uses shard-by-shard BnB 4-bit quantization on GPU (fast_train_zeroclaw.py)
  - Export uses streaming LoRA merge at ~6 GB peak memory (fast_export.py)
  - Heretic removes safety alignment via Optuna-optimized directional ablation
  - Completion-only loss masks system/user turns (only assistant responses contribute)
  - Dataset validation pre-flight check before committing GPU time
  - Auto-install llama.cpp if not found (needed by MagicQuant for perplexity probing)
"""

import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


# ── Configuration ────────────────────────────────────────────────────────────

@dataclass
class TrainingConfig:
    model_name: str = "Tesslate/OmniCoder-9B"
    datasets: list[str] = field(default_factory=lambda: ["data/zeroclaw_training_data.jsonl"])
    # NOTE: defaults reconciled with the UI's TrainingCfg so CLI and UI produce
    # equivalent adapters from the same config (see audit H1/M-warmup).
    max_seq_length: int = 4096
    load_in_4bit: bool = True  # Unused by fast loader (always 4-bit), kept for config compat
    lora_r: int = 32
    lora_alpha: int = 64
    lora_dropout: float = 0.05
    use_rslora: bool = True  # Applied via the shared TrainingService (LoraConfig.use_rslora)
    num_train_epochs: int = 3
    per_device_train_batch_size: int = 2
    gradient_accumulation_steps: int = 4
    learning_rate: float = 2e-4
    lr_scheduler_type: str = "cosine"
    warmup_ratio: float = 0.05
    packing: bool = False  # Mutually exclusive with completion_only_loss
    optim: str = "paged_adamw_8bit"

    @property
    def dataset_path(self) -> str:
        """Backward compat -- returns first dataset."""
        return self.datasets[0] if self.datasets else ""

    @dataset_path.setter
    def dataset_path(self, value: str):
        """Backward compat -- sets datasets to a single-element list."""
        self.datasets = [value]


@dataclass
class ExportConfig:
    """Single stage: merge LoRA + convert to GGUF."""
    gguf_type: str = "bf16"  # bf16, f16, q8_0, etc.
    also_save_merged: bool = False  # optionally also save HF safetensors


@dataclass
class HereticConfig:
    """Heretic abliteration (safety alignment removal via directional ablation)."""
    n_trials: int = 200
    n_startup_trials: int = 60
    quantization: str = "bnb_4bit"  # "none" or "bnb_4bit"
    kl_divergence_scale: float = 1.0
    orthogonalize_direction: bool = False
    row_normalization: str = "none"  # "none", "pre", or "full"


@dataclass
class ReapConfig:
    """REAP expert pruning (MoE models only).

    Router-weighted Expert Activation Pruning. Removes a fraction of experts
    from each MoE layer based on activation-informed saliency metrics.
    Cerebras Research's tool — https://github.com/CerebrasResearch/reap

    Only supported for specific MoE architectures listed in reap.model_util.
    Dense models and unsupported MoE variants will skip this stage.
    """
    compression_ratio: float = 0.25  # Fraction of experts to remove per layer
    prune_method: str = "reap"  # reap, frequency, ean_sum, max_activations, etc.
    samples_per_category: int = 512  # Calibration samples
    model_max_length: int = 2048  # Max tokens per calibration sample
    dataset_name: str = "theblackcat102/evol-codealpaca-v1"  # Calibration dataset
    seed: int = 42


@dataclass
class MagicQuantConfig:
    target_base_quant: str = "MXFP4_MOE"
    generations: int = 50
    population_size: int = 100
    tiers: list[str] = field(default_factory=lambda: ["Q4", "Q5", "Q6"])
    verify: bool = False
    llamacpp_path: Optional[str] = None
    # measured: run the Predict->Measure->Learn loop (real perplexity per
    # candidate) instead of the default prediction-only search. Slower but
    # empirical. rocmfpx_schemes: let the search also explore the AMD-native
    # ROCmFPX fork types (requires the fork's libggml; produces GGUFs loadable
    # only on the fork). See docs/rocmfpx.md.
    measured: bool = False
    measurement_rounds: int = 3
    rocmfpx_schemes: bool = False
    # iq_schemes: let the search also explore the sub-4-bit stock-ggml IQ
    # schemes (IQ4_XS/IQ3_S/…); opt-in because their noise factors are
    # heuristic pending calibration. Imatrix-required IQ types stay excluded.
    iq_schemes: bool = False
    # seed: optional RNG seed for a reproducible search (None = nondeterministic).
    seed: Optional[int] = None
    # use_imatrix/imatrix_corpus: capture/reuse an importance matrix and weight
    # quantization noise by activation magnitude (both search paths). Off by
    # default (unweighted, historical behavior). imatrix_corpus=None uses the
    # bundled default calibration corpus.
    use_imatrix: bool = False
    imatrix_corpus: Optional[str] = None
    # enable_kl/kl_weight/enable_speed_bench: measured-search-only extras (see
    # MagicQuantOrchestrator.run_measured_search). enable_kl also measures real
    # KL-divergence-to-base per candidate and blends |mean_kl| * kl_weight into
    # final-survivor selection; enable_speed_bench also measures real
    # tokens/sec per candidate via llama-bench (informational).
    enable_kl: bool = False
    kl_weight: float = 0.1
    enable_speed_bench: bool = False
    # measurement_chunks: cap every perplexity/KL pass (both search paths) to
    # this many ctx_size-token chunks instead of the whole corpus, trading
    # statistical resolution for wall-clock time. None (default) measures the
    # whole corpus every pass -- promotes the old env-only MAGICQUANT_PPL_
    # CHUNKS to a real, provenance-stamped setting (see search_results.json's
    # "measurement" block).
    measurement_chunks: Optional[int] = None
    # stream_aware/head_aggressive: search-bias knobs (EvolutionarySurvivor
    # sampling, not scoring; both search paths). stream_aware biases sampling
    # of streamed matmul groups toward BF16->Q8_0 (recommended: faster
    # generation, smaller size, PPL-neutral). head_aggressive biases the 'H'
    # (LM head) group toward smaller K-quants; superseded by stream_aware for
    # most models. Off by default (unbiased sampling, historical behavior).
    stream_aware: bool = False
    head_aggressive: bool = False
    # speed_aware/speed_metric: measured-search-only final-survivor selection
    # bias (see MagicQuantOrchestrator.run_measured_search / _speed_aware_pick).
    # Within a tier, prefer the fastest near-tied measured candidate instead of
    # the flat quality-best. Off by default (unbiased selection, historical
    # behavior); a no-op for prediction-only search (run_full_search never
    # measures candidates for real, so there's nothing to re-rank).
    speed_aware: bool = False
    speed_metric: str = "bytes"  # "bytes" (deterministic size) | "bench" (measured tg)
    # speed_weight/use_bytes_tps: tps-aware SEARCH objective (both search
    # paths -- see _build_objective_weights). speed_weight reserves this much
    # weight for the objective's speed term, renormalizing precision:size to
    # fill the remainder; None (default) leaves today's fixed 0.50/0.35/0.15
    # weights unchanged. use_bytes_tps scores the speed term deterministically
    # from predicted size instead of the noisier speed_multiplier path --
    # recommended whenever speed_weight is set. NOTE: the objective alone only
    # reshapes per-candidate scoring within each size band; it's speed_aware
    # (above) that actually changes which candidate wins each tier in measured
    # search -- the two are meant to be paired.
    speed_weight: Optional[float] = None
    use_bytes_tps: bool = False
    # calibration_source/write_calibration: empirically calibrated noise
    # factors/speed multipliers (see magicquant.quant.calibration).
    # calibration_source loads them from this file instead of the fixed
    # tools/calibration_results.json path (both search paths). write_calibration
    # fits + writes <output>/noise_calibration.json from THIS measured run's
    # measurements (measured-search only; best-effort -- a fitting failure is
    # logged and never blocks a successful search from completing).
    calibration_source: str = ""
    write_calibration: bool = False


@dataclass
class ROCmFPXConfig:
    """ROCmFPX: AMD-tuned GGUF quant family (ROCmFP3/4/6/8, straight + agent
    presets) via https://github.com/ciru-ai/ROCmFPX -- a llama.cpp fork, not
    a pip package. Auto-installed (git clone + Strix Halo build) on first use.
    Optional stage, gated off by default (``--rocmfpx``).
    """
    formats: list[str] = field(
        default_factory=lambda: ["rocmfp4-agent", "rocmfp6-agent", "rocmfp8-agent"]
    )
    source_model: str = ""  # when export is skipped: path to GGUF or merged model dir
    rocmfpx_hint: str = ""  # path to an existing ROCmFPX/llama.cpp-fork build
    imatrix: str = ""  # optional path to an imatrix GGUF


@dataclass
class QATConfig:
    """QAT-LoRA: train adapters robust to a per-group hybrid quant config.

    The hybrid config is read from a prior MagicQuant search's
    ``search_results.json`` (``config_source`` + ``tier``); when empty it
    auto-resolves to ``<output>/magicquant/search_results.json``. Optional stage,
    gated off by default (``--qat`` to enable).
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


def detect_license(model_id: str) -> str:
    """Fetch the license from a HuggingFace model's metadata.

    Returns the SPDX-style license string (e.g. 'apache-2.0', 'mit',
    'llama3.2') or 'unknown' if it can't be determined.
    """
    try:
        from huggingface_hub import model_info
        info = model_info(model_id)
        tags = info.tags or []
        # HF stores license as a tag like "license:apache-2.0"
        for tag in tags:
            if tag.startswith("license:"):
                return tag.split(":", 1)[1]
        # Also check the card_data field
        if hasattr(info, 'card_data') and info.card_data:
            lic = getattr(info.card_data, 'license', None)
            if lic:
                return lic
    except Exception:
        pass
    return "unknown"


@dataclass
class UploadConfig:
    repo_id: str = ""
    private: bool = True
    base_model: str = ""
    license: str = ""  # empty = auto-detect from base model
    upload_lora: bool = False
    upload_merged: bool = False
    upload_gguf: bool = True


@dataclass
class PipelineConfig:
    output_dir: str = "./output"
    training: TrainingConfig = field(default_factory=TrainingConfig)
    export: Optional[ExportConfig] = field(default_factory=ExportConfig)
    heretic: Optional[HereticConfig] = None
    reap: Optional[ReapConfig] = None
    qat: Optional[QATConfig] = None  # optional; gated off by default (--qat)
    magicquant: Optional[MagicQuantConfig] = field(default_factory=MagicQuantConfig)
    rocmfpx: Optional[ROCmFPXConfig] = None  # optional; gated off by default (--rocmfpx)
    upload: Optional[UploadConfig] = None


# ── Artifact paths ───────────────────────────────────────────────────────────

class Artifacts:
    def __init__(self, output_dir: str):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

    @property
    def lora_dir(self) -> Path:
        return self.output_dir / "lora_adapters"

    @property
    def merged_dir(self) -> Path:
        return self.output_dir / "merged_model"

    @property
    def bf16_gguf(self) -> Path:
        return self.output_dir / "model-bf16.gguf"

    @property
    def heretic_dir(self) -> Path:
        return self.output_dir / "heretic_model"

    @property
    def reap_dir(self) -> Path:
        return self.output_dir / "reap_model"

    @property
    def magicquant_dir(self) -> Path:
        return self.output_dir / "magicquant"

    @property
    def qat_dir(self) -> Path:
        return self.output_dir / "qat_adapters"

    @property
    def rocmfpx_dir(self) -> Path:
        return self.output_dir / "rocmfpx"


# ── Helpers ──────────────────────────────────────────────────────────────────

# Shared logging helper (single source of truth — see core/log.py).
try:
    from log import LogFn, default_log as _default_log
except ImportError:  # pragma: no cover - when imported as the `core` package
    from core.log import LogFn, default_log as _default_log


def _run(cmd: list[str], log: LogFn, env_extra: dict = None, cwd: str = None,
         timeout: Optional[float] = None) -> int:
    env = os.environ.copy()
    env.update({
        "HSA_ENABLE_SDMA": "0",
        "PYTORCH_HIP_ALLOC_CONF": "backend:native,expandable_segments:True",
        # Mirror the UI/services preamble so CLI and UI subprocess envs match.
        "UNSLOTH_SKIP_TORCHVISION_CHECK": "1",
        "TORCH_ROCM_AOTRITON_ENABLE_EXPERIMENTAL": "1",
        "PYTHONUNBUFFERED": "1",
    })
    if env_extra:
        env.update(env_extra)

    # start_new_session=True puts the child in its own process group so we can
    # kill the whole tree on timeout (a gfx1151 kernel hang otherwise wedges the
    # CLI indefinitely — there is no UI stop button here).
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        env=env, cwd=cwd, text=True, bufsize=1, start_new_session=True,
    )

    if not timeout:
        try:
            for line in proc.stdout:
                line = line.rstrip()
                if line:
                    log(line)
        except Exception:
            _kill_process_group(proc)
            raise
        proc.wait()
        return proc.returncode

    # With a timeout, drain stdout on a background thread so a silent child
    # (no output) can still be killed when the deadline elapses.
    import threading

    def _drain():
        try:
            for line in proc.stdout:
                line = line.rstrip()
                if line:
                    log(line)
        except Exception:
            pass

    reader = threading.Thread(target=_drain, daemon=True)
    reader.start()
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        log(f"Stage exceeded timeout ({timeout:.0f}s) — terminating", "error")
        _kill_process_group(proc)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass
        reader.join(timeout=5)
        return proc.returncode if proc.returncode is not None else -1
    reader.join(timeout=5)
    return proc.returncode


def _kill_process_group(proc) -> None:
    """SIGTERM then SIGKILL the child's process group; never raise."""
    import signal
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(os.getpgid(proc.pid), sig)
        except (ProcessLookupError, OSError):
            return
        try:
            proc.wait(timeout=5)
            return
        except subprocess.TimeoutExpired:
            continue


def _find_python() -> str:
    return sys.executable


# Project root (the directory containing core/). Used by the Service classes.
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Pinned llama.cpp ref for reproducible auto-install (audit L-supply-chain).
# Bump deliberately; clone uses --branch so it pins a tag, not the default branch.
LLAMACPP_REPO = "https://github.com/ggml-org/llama.cpp.git"
LLAMACPP_PIN = "gguf-v0.19.0"  # known-good release tag


def _services():
    """Import the shared service classes (core/services.py).

    Imported lazily so importing pipeline.py for CLI parsing/tests doesn't pull
    in the service layer. Works both when ``core`` is on sys.path and when
    pipeline is imported as ``core.pipeline``.
    """
    try:
        import services as _svc
    except ImportError:  # pragma: no cover - package-import fallback
        from core import services as _svc
    return _svc


def _markers():
    try:
        import markers as _m
    except ImportError:  # pragma: no cover
        from core import markers as _m
    return _m


def _run_stage_script(
    script: str,
    script_path: Path,
    log: LogFn,
    *,
    stage: str = "",
    stage_dir: Optional[Path] = None,
    key_file: Optional[Path] = None,
    cfg_hash: str = "",
    env_extra: dict = None,
    timeout: Optional[float] = None,
    keep_script: bool = False,
) -> int:
    """Write ``script`` to ``script_path``, run it, write a completion marker on
    success, and clean up the generated stage script (kept on failure).

    Returns the subprocess exit code.
    """
    script_path = Path(script_path)
    script_path.parent.mkdir(parents=True, exist_ok=True)
    script_path.write_text(script)

    rc = _run([_find_python(), "-u", str(script_path)], log,
              env_extra=env_extra, cwd=str(PROJECT_ROOT), timeout=timeout)

    if rc == 0:
        # Record a completion marker when the key artifact is present + non-empty.
        if stage and stage_dir is not None and key_file is not None:
            kf = Path(key_file)
            if kf.exists() and kf.stat().st_size > 0:
                try:
                    _markers().write_marker(stage_dir, stage, kf, cfg_hash)
                except OSError as e:
                    log(f"Could not write completion marker: {e}", "warn")
        # Remove the generated stage script on success (keep the artifacts).
        if not keep_script:
            try:
                script_path.unlink()
            except OSError:
                pass
    return rc


def _preflight_stage(stage: str, config: PipelineConfig, log: LogFn, skip: bool = False) -> bool:
    """Advisory GPU-memory preflight for a stage. Logs but never aborts the run
    on its own (the caller decides); returns the check result for callers/tests.
    """
    try:
        try:
            import preflight as _pf
        except ImportError:  # pragma: no cover
            from core import preflight as _pf
    except Exception:
        return True
    # Best-effort param estimate from the base model config if available.
    cfg_json = Path(config.output_dir) / "merged_model" / "config.json"
    params_b = _pf.estimate_params_b(str(cfg_json)) if cfg_json.exists() else None
    needed = _pf.estimate_stage_gb(stage, params_b)
    return _pf.check_gpu_memory(needed, log=log, skip=skip)


def _find_llamacpp(hint: Optional[str] = None) -> Optional[Path]:
    candidates = [
        hint,
        os.environ.get("LLAMACPP_PATH"),
        str(Path.home() / "llama.cpp"),
        "./llama.cpp",
        "/usr/local",
    ]
    for c in candidates:
        if not c:
            continue
        p = Path(c)
        for sub in [p / "convert_hf_to_gguf.py", p / "bin" / "convert_hf_to_gguf.py",
                     p / "build" / "bin" / "llama-perplexity"]:
            if sub.exists():
                return p
    return None


# ── Improvement #3: Dataset validation ───────────────────────────────────────

def _validate_local_dataset(path: Path, log: LogFn) -> bool:
    """Validate a single local dataset file (JSONL/JSON)."""
    if not path.exists():
        log(f"Dataset not found: {path}", "error")
        return False

    if path.stat().st_size == 0:
        log("Dataset file is empty", "error")
        return False

    errors = []
    warnings = []
    tool_calls = 0
    roles_seen = set()
    n = 0

    with open(path) as f:
        for i, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                ex = json.loads(line)
            except json.JSONDecodeError as e:
                errors.append(f"Line {i}: invalid JSON — {e}")
                if len(errors) >= 5:
                    errors.append("(stopping after 5 errors)")
                    break
                continue

            n += 1

            if "messages" not in ex:
                errors.append(f"Line {i}: missing 'messages' field")
                continue

            msgs = ex["messages"]
            if not isinstance(msgs, list) or len(msgs) < 2:
                errors.append(f"Line {i}: 'messages' must be a list with >= 2 entries")
                continue

            for j, msg in enumerate(msgs):
                if "role" not in msg or "content" not in msg:
                    errors.append(f"Line {i}, message {j}: missing 'role' or 'content'")
                    break
                roles_seen.add(msg["role"])

            # Count tool calls for coverage stats
            for msg in msgs:
                if msg.get("role") == "assistant" and "<tool_call>" in msg.get("content", ""):
                    tool_calls += 1

    if errors:
        for e in errors:
            log(f"  {e}", "error")
        log(f"Dataset validation failed with {len(errors)} errors", "error")
        return False

    # Warnings
    if n < 10:
        warnings.append(f"Only {n} examples — consider adding more for better generalization")
    if "system" not in roles_seen:
        warnings.append("No 'system' role found — model may not learn system prompt behavior")
    if "assistant" not in roles_seen:
        warnings.append("No 'assistant' role found — nothing for the model to learn")

    for w in warnings:
        log(f"  Warning: {w}", "warn")

    # Stats
    log(f"  Examples: {n}")
    log(f"  Roles: {sorted(roles_seen)}")
    log(f"  Tool call turns: {tool_calls}")
    log(f"  File size: {path.stat().st_size / 1024:.1f} KB")
    return True


def validate_dataset(dataset_path_or_sources, log: LogFn) -> bool:
    """Pre-flight check on one or more dataset sources before committing GPU time.

    Accepts either a single path string (backward compat) or a list of sources.
    Local files are fully validated; HuggingFace dataset IDs are noted as remote.
    """
    log("Validating dataset(s)", "stage")

    # Normalize to list
    if isinstance(dataset_path_or_sources, str):
        sources = [dataset_path_or_sources]
    else:
        sources = list(dataset_path_or_sources)

    if not sources or all(not s.strip() for s in sources):
        log("No datasets configured", "error")
        return False

    all_ok = True
    for src in sources:
        src = src.strip()
        if not src:
            continue

        # Strip config/split suffixes for path detection
        clean = src.split("[")[0].split(":")[0] if (":" in src and not src.startswith("/") and not Path(src).suffix) or "[" in src else src

        local = Path(clean)
        if local.suffix in (".jsonl", ".json", ".csv", ".parquet"):
            # Local file -- full validation
            if not _validate_local_dataset(local, log):
                all_ok = False
        else:
            # Could be a HF dataset ID or a local path without extension
            if local.exists():
                if not _validate_local_dataset(local, log):
                    all_ok = False
            else:
                log(f"  HF dataset: {src} (will be downloaded if not cached)")

    if all_ok:
        log("Dataset validation passed", "success")
    return all_ok


# ── Improvement #4: Auto-install llama.cpp ───────────────────────────────────

def ensure_llamacpp(hint: Optional[str], log: LogFn) -> Optional[Path]:
    """Find llama.cpp, or auto-install it if missing."""
    found = _find_llamacpp(hint)
    if found:
        log(f"llama.cpp found at: {found}")
        return found

    install_dir = Path.home() / "llama.cpp"
    log("llama.cpp not found — auto-installing", "stage")

    # Clone a pinned ref (not the default branch) for reproducibility.
    log(f"Cloning llama.cpp from GitHub (pinned {LLAMACPP_PIN})...")
    rc = _run(["git", "clone", "--depth", "1", "--branch", LLAMACPP_PIN,
               LLAMACPP_REPO, str(install_dir)], log)
    if rc != 0:
        log("Failed to clone llama.cpp", "error")
        return None

    # Build
    log("Building llama.cpp (cmake)...")
    build_dir = install_dir / "build"
    rc = _run(["cmake", "-B", str(build_dir), "-DCMAKE_BUILD_TYPE=Release", str(install_dir)], log)
    if rc != 0:
        log("cmake configure failed", "error")
        return None

    import multiprocessing
    jobs = str(multiprocessing.cpu_count())
    rc = _run(["cmake", "--build", str(build_dir), "-j", jobs], log)
    if rc != 0:
        log("cmake build failed", "error")
        return None

    log(f"llama.cpp installed at: {install_dir}", "success")
    return install_dir


# ── Stage: Training (with completion-only loss) ─────────────────────────────
#
# Uses the custom fast loader (fast_train_zeroclaw.py) which creates the model
# on meta device, loads safetensors shard-by-shard with inline BnB 4-bit
# quantization, and uses PEFT LoRA directly. This avoids single-threaded
# safetensors chunking that crawls to a halt on unified memory as GTT fills.

def _training_cfg_hash(config: PipelineConfig) -> str:
    tc = config.training
    return _markers().config_hash({
        "model_name": tc.model_name, "datasets": tc.datasets,
        "max_seq_length": tc.max_seq_length, "lora_r": tc.lora_r,
        "lora_alpha": tc.lora_alpha, "lora_dropout": tc.lora_dropout,
        "use_rslora": tc.use_rslora, "num_train_epochs": tc.num_train_epochs,
        "per_device_train_batch_size": tc.per_device_train_batch_size,
        "gradient_accumulation_steps": tc.gradient_accumulation_steps,
        "learning_rate": tc.learning_rate, "lr_scheduler_type": tc.lr_scheduler_type,
        "warmup_ratio": tc.warmup_ratio, "optim": tc.optim, "packing": tc.packing,
    })


def stage_training(config: PipelineConfig, artifacts: Artifacts, log: LogFn,
                   force: bool = False, timeout: Optional[float] = None,
                   skip_preflight: bool = False) -> bool:
    """Run QLoRA training with completion-only loss using the custom fast loader.

    Builds the training script via the shared TrainingService (single source of
    truth with the UI) so CLI and UI produce equivalent adapters. Skips on a
    valid completion marker; preflights GPU memory; cleans up the stage script.
    """
    tc = config.training

    # Completion-marker resume: skip only when a valid marker matches the config
    # AND the key adapter file is present and non-empty (not bare existence).
    key_file = artifacts.lora_dir / "adapter_model.safetensors"
    cfg_hash = _training_cfg_hash(config)
    if _markers().is_stage_complete(artifacts.lora_dir, key_file, cfg_hash, force=force):
        log(f"Training already complete (marker matches) at {artifacts.lora_dir} — skipping", "success")
        return True

    # Validate dataset(s) before committing GPU time.
    if not validate_dataset(config.training.datasets, log):
        return False

    # GPU-memory preflight (advisory; overridable).
    _preflight_stage("training", config, log, skip=skip_preflight)

    log("Starting QLoRA training (fast loader, completion-only loss)", "stage")

    svc = _services().TrainingService(PROJECT_ROOT, _find_python())
    script = svc.build_script(
        model_name=tc.model_name,
        datasets=tc.datasets,
        output_dir=config.output_dir,
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
        optim=tc.optim,
        packing=tc.packing,
    )

    rc = _run_stage_script(
        script, artifacts.output_dir / "_stage_train.py", log,
        stage="training", stage_dir=artifacts.lora_dir, key_file=key_file,
        cfg_hash=cfg_hash, timeout=timeout,
    )
    if rc != 0:
        log(f"Training failed (exit code {rc})", "error")
        return False

    if not artifacts.lora_dir.exists():
        log("LoRA adapters directory not found after training", "error")
        return False

    log("Training complete", "success")
    return True


# ── Stage: Export (streaming LoRA merge) ──────────────────────────────────────
#
# Uses fast_export.py's streaming shard-by-shard merge instead of standard
# save_pretrained_merged(), which loads the entire model into memory (~80 GB
# for a 40B model). The streaming merge peaks at ~6 GB.

def stage_export(config: PipelineConfig, artifacts: Artifacts, log: LogFn,
                 force: bool = False, timeout: Optional[float] = None,
                 skip_preflight: bool = False) -> bool:
    """Merge LoRA adapters into base model using streaming shard-by-shard merge.

    Builds the export script via the shared ExportService (single source of truth
    with the UI). Skips on a valid completion marker; cleans up the stage script.
    """
    if not artifacts.lora_dir.exists():
        log("No LoRA adapters found — run training first", "error")
        return False

    # Read the adapter_config.json to find the base model ID.
    adapter_config_path = artifacts.lora_dir / "adapter_config.json"
    if adapter_config_path.exists():
        with open(adapter_config_path) as f:
            adapter_cfg = json.load(f)
        base_model_id = adapter_cfg.get("base_model_name_or_path", config.training.model_name)
    else:
        base_model_id = config.training.model_name

    # Completion-marker resume: a merged_model dir with safetensors + a marker.
    cfg_hash = _markers().config_hash({"base_model_id": base_model_id, "src": str(artifacts.lora_dir)})
    existing = sorted(artifacts.merged_dir.glob("*.safetensors")) if artifacts.merged_dir.exists() else []
    key_file = existing[0] if existing else (artifacts.merged_dir / "model.safetensors")
    if _markers().is_stage_complete(artifacts.merged_dir, key_file, cfg_hash, force=force):
        log(f"Export already complete (marker matches) at {artifacts.merged_dir} — skipping", "success")
        return True

    _preflight_stage("export", config, log, skip=skip_preflight)
    log("Merging LoRA to safetensors (streaming shard-by-shard)", "stage")

    svc = _services().ExportService(PROJECT_ROOT, _find_python())
    script = svc.build_script(
        base_model_id=base_model_id,
        lora_source=str(artifacts.lora_dir),
        has_lora=True,
        merged_dir=str(artifacts.merged_dir),
    )

    # Resolve the actual key file after the run for the marker.
    def _resolve_key():
        st = sorted(artifacts.merged_dir.glob("*.safetensors"))
        return st[0] if st else key_file

    rc = _run_stage_script(
        script, artifacts.output_dir / "_stage_export.py", log,
        cfg_hash=cfg_hash, timeout=timeout,
    )
    if rc != 0:
        log(f"Export failed (exit code {rc})", "error")
        return False

    if artifacts.merged_dir.exists():
        # Write the completion marker now that the real artifact exists.
        kf = _resolve_key()
        if kf.exists() and kf.stat().st_size > 0:
            try:
                _markers().write_marker(artifacts.merged_dir, "export", kf, cfg_hash)
            except OSError as e:
                log(f"Could not write completion marker: {e}", "warn")
        log(f"Merged safetensors ready at {artifacts.merged_dir}", "success")
    else:
        log("Merged model directory not found after export", "error")
        return False

    log("Export complete", "success")
    return True


# ── Stage: Heretic (abliteration) ───────────────────────────────────────────
#
# Removes safety alignment from LLMs via Optuna-optimized directional ablation.
# Uses heretic-llm's internal API (not the CLI) to avoid interactive prompts.
# Runs as a subprocess so GPU memory is fully freed afterward.

def stage_heretic(config: PipelineConfig, artifacts: Artifacts, log: LogFn,
                  force: bool = False, timeout: Optional[float] = None,
                  skip_preflight: bool = False) -> bool:
    """Run heretic abliteration on the merged model.

    Builds the abliteration script via the shared HereticService (single source
    of truth with the UI). Skips on a valid completion marker; cleans up the
    stage script.
    """
    if not artifacts.merged_dir.exists():
        log("No merged model found \u2014 run export first", "error")
        return False

    hc = config.heretic
    heretic_output = str(artifacts.heretic_dir)
    checkpoint_dir = str(artifacts.output_dir / "_heretic_checkpoints")

    cfg_hash = _markers().config_hash({
        "src": str(artifacts.merged_dir), "n_trials": hc.n_trials,
        "n_startup_trials": hc.n_startup_trials, "quantization": hc.quantization,
        "kl_divergence_scale": hc.kl_divergence_scale,
        "orthogonalize_direction": hc.orthogonalize_direction,
        "row_normalization": hc.row_normalization,
    })
    existing = sorted(artifacts.heretic_dir.glob("*.safetensors")) if artifacts.heretic_dir.exists() else []
    key_file = existing[0] if existing else (artifacts.heretic_dir / "model.safetensors")
    if _markers().is_stage_complete(artifacts.heretic_dir, key_file, cfg_hash, force=force):
        log(f"Heretic already complete (marker matches) at {artifacts.heretic_dir} \u2014 skipping", "success")
        return True

    _preflight_stage("heretic", config, log, skip=skip_preflight)
    log("Starting heretic abliteration", "stage")

    svc = _services().HereticService(PROJECT_ROOT, _find_python())
    script = svc.build_script(
        model_path=str(artifacts.merged_dir),
        output_path=heretic_output,
        checkpoint_dir=checkpoint_dir,
        n_trials=hc.n_trials,
        n_startup_trials=hc.n_startup_trials,
        quantization=hc.quantization,
        kl_divergence_scale=hc.kl_divergence_scale,
        orthogonalize_direction=hc.orthogonalize_direction,
        row_normalization=hc.row_normalization,
    )

    rc = _run_stage_script(
        script, artifacts.output_dir / "_stage_heretic.py", log,
        cfg_hash=cfg_hash, timeout=timeout,
    )
    if rc != 0:
        log(f"Heretic failed (exit code {rc})", "error")
        return False

    if not artifacts.heretic_dir.exists():
        log("Heretic output directory not found", "error")
        return False

    st = sorted(artifacts.heretic_dir.glob("*.safetensors"))
    kf = st[0] if st else key_file
    if kf.exists() and kf.stat().st_size > 0:
        try:
            _markers().write_marker(artifacts.heretic_dir, "heretic", kf, cfg_hash)
        except OSError as e:
            log(f"Could not write completion marker: {e}", "warn")

    log(f"Abliterated model saved to {artifacts.heretic_dir}", "success")
    return True


# ── Stage: REAP (expert pruning) ─────────────────────────────────────────────
#
# Router-weighted Expert Activation Pruning for MoE models.
# Only supports the specific architectures in reap.model_util.MODEL_ATTRS.
# Unsupported architectures (dense models, Granite MoE, etc.) are skipped
# silently with a warning so the rest of the pipeline can continue.

# Architectures supported by reap.model_util.MODEL_ATTRS (mirrors that dict).
# REAP-supported architectures and arch detection live in the shared
# reap_common module so the CLI and UI agree (audit L-source-dup / L-reap-archlist).
try:
    from reap_common import (
        REAP_SUPPORTED_ARCHS, detect_model_arch as _detect_model_arch,
    )
except ImportError:  # pragma: no cover - package-import fallback
    from core.reap_common import (
        REAP_SUPPORTED_ARCHS, detect_model_arch as _detect_model_arch,
    )


def stage_reap(config: PipelineConfig, artifacts: Artifacts, log: LogFn,
               force: bool = False, timeout: Optional[float] = None,
               skip_preflight: bool = False) -> bool:
    """Run REAP expert pruning on the merged or abliterated model.

    Builds the pruning script via the shared ReapService. Skips on a valid
    completion marker; silently skips (returns True) for unsupported archs.
    """
    log("Starting REAP expert pruning", "stage")

    # Determine source: prefer heretic output, fall back to merged safetensors
    if artifacts.heretic_dir.exists() and any(artifacts.heretic_dir.glob("*.safetensors")):
        source_path = artifacts.heretic_dir
        log(f"Source: abliterated model at {source_path}")
    elif artifacts.merged_dir.exists() and any(artifacts.merged_dir.glob("*.safetensors")):
        source_path = artifacts.merged_dir
        log(f"Source: merged safetensors at {source_path}")
    else:
        log("No merged or abliterated model found \u2014 run export first", "error")
        return False

    rc = config.reap
    cfg_hash = _markers().config_hash({
        "src": str(source_path), "compression_ratio": rc.compression_ratio,
        "prune_method": rc.prune_method, "samples_per_category": rc.samples_per_category,
        "model_max_length": rc.model_max_length, "dataset_name": rc.dataset_name,
        "seed": rc.seed,
    })
    existing = sorted(artifacts.reap_dir.glob("*.safetensors")) if artifacts.reap_dir.exists() else []
    key_file = existing[0] if existing else (artifacts.reap_dir / "model.safetensors")
    if _markers().is_stage_complete(artifacts.reap_dir, key_file, cfg_hash, force=force):
        log(f"REAP already complete (marker matches) at {artifacts.reap_dir} \u2014 skipping", "success")
        return True

    # Check architecture support before launching a subprocess.
    arch = _detect_model_arch(source_path)
    if arch is None:
        log(f"Could not detect model architecture from {source_path}/config.json \u2014 skipping REAP", "warn")
        return True
    if arch not in REAP_SUPPORTED_ARCHS:
        log(f"Architecture '{arch}' is not supported by REAP \u2014 skipping stage", "warn")
        log(f"  REAP supports: {sorted(REAP_SUPPORTED_ARCHS)}")
        return True
    log(f"Detected supported architecture: {arch}")

    _preflight_stage("reap", config, log, skip=skip_preflight)

    svc = _services().ReapService(PROJECT_ROOT, _find_python())
    script = svc.build_script(
        input_dir=str(source_path.resolve()),
        output_dir=str(artifacts.reap_dir.resolve()),
        cwd_dir=str(artifacts.output_dir.resolve()),
        compression_ratio=rc.compression_ratio,
        prune_method=rc.prune_method,
        samples_per_category=rc.samples_per_category,
        model_max_length=rc.model_max_length,
        dataset_name=rc.dataset_name,
        seed=rc.seed,
    )

    rc_code = _run_stage_script(
        script, artifacts.output_dir / "_stage_reap.py", log,
        cfg_hash=cfg_hash, timeout=timeout,
    )
    if rc_code != 0:
        log(f"REAP failed (exit code {rc_code})", "error")
        return False

    if not artifacts.reap_dir.exists():
        log("REAP output directory not found", "error")
        return False

    st = sorted(artifacts.reap_dir.glob("*.safetensors"))
    kf = st[0] if st else key_file
    if kf.exists() and kf.stat().st_size > 0:
        try:
            _markers().write_marker(artifacts.reap_dir, "reap", kf, cfg_hash)
        except OSError as e:
            log(f"Could not write completion marker: {e}", "warn")

    log(f"Pruned model saved to {artifacts.reap_dir}", "success")
    return True


# ── Stage: QAT (quantization-aware LoRA) ─────────────────────────────────────
#
# Trains LoRA adapters robust to MagicQuant's per-group hybrid quant config.
# The per-group config is read from a prior MagicQuant search's
# search_results.json (config_source, or auto-detected in the output's
# magicquant/ dir). Optional stage, gated off by default (--qat).


def _resolve_qat_config_source(qc: "QATConfig", artifacts: "Artifacts") -> Optional[Path]:
    """Return the search_results.json the QAT stage should target, or None.

    Explicit ``config_source`` wins (resolved relative to the output dir / project
    root if not absolute); otherwise auto-detect ``<output>/magicquant/search_results.json``.
    """
    if qc.config_source:
        p = Path(qc.config_source)
        if p.is_absolute():
            return p if p.exists() else None
        for base in (artifacts.output_dir, PROJECT_ROOT):
            cand = base / qc.config_source
            if cand.exists():
                return cand
        return None
    auto = artifacts.magicquant_dir / "search_results.json"
    return auto if auto.exists() else None


def stage_qat(config: PipelineConfig, artifacts: Artifacts, log: LogFn,
              force: bool = False, timeout: Optional[float] = None,
              skip_preflight: bool = False) -> bool:
    """Run QAT-LoRA fine-tuning against the per-group hybrid config.

    Builds the QAT script via the shared QATService (single source of truth with
    the UI). Skips on a valid completion marker.
    """
    log("Starting QAT-LoRA", "stage")

    qc = config.qat
    source_model = config.training.model_name

    config_source = _resolve_qat_config_source(qc, artifacts)
    if config_source is None:
        log(
            "No search_results.json found for QAT (set QAT config_source or run "
            "a MagicQuant search first)",
            "error",
        )
        return False
    log(f"Hybrid config: {config_source} (tier {qc.tier})")

    if not qc.dataset:
        log("No QAT dataset configured — set a chat JSONL dataset path", "error")
        return False

    cfg_hash = _markers().config_hash({
        "model": source_model, "config": str(config_source), "tier": qc.tier,
        "dataset": qc.dataset, "lora_r": qc.lora_r, "lora_alpha": qc.lora_alpha,
        "epochs": qc.epochs, "max_steps": qc.max_steps, "lr": qc.lr,
        "max_seq_len": qc.max_seq_len,
    })
    key_file = artifacts.qat_dir / "qat_meta.json"
    if _markers().is_stage_complete(artifacts.qat_dir, key_file, cfg_hash, force=force):
        log(f"QAT already complete (marker matches) at {artifacts.qat_dir} — skipping", "success")
        return True

    _preflight_stage("qat", config, log, skip=skip_preflight)

    svc = _services().QATService(PROJECT_ROOT, _find_python())
    script = svc.build_script(
        model=source_model,
        config_path=str(config_source),
        tier=qc.tier,
        dataset=qc.dataset,
        out=str(artifacts.qat_dir.resolve()),
        lora_r=qc.lora_r,
        lora_alpha=qc.lora_alpha,
        epochs=qc.epochs,
        max_steps=qc.max_steps,
        lr=qc.lr,
        max_seq_len=qc.max_seq_len,
    )

    rc = _run_stage_script(
        script, artifacts.output_dir / "_stage_qat.py", log,
        cfg_hash=cfg_hash, timeout=timeout,
    )
    if rc != 0:
        log(f"QAT failed (exit code {rc})", "error")
        return False

    if not key_file.exists():
        log("QAT output (qat_meta.json) not found after run", "error")
        return False
    try:
        _markers().write_marker(artifacts.qat_dir, "qat", key_file, cfg_hash)
    except OSError as e:
        log(f"Could not write completion marker: {e}", "warn")
    log(f"QAT adapters saved to {artifacts.qat_dir}", "success")
    return True


# ── Stage: MagicQuant ────────────────────────────────────────────────────────

def stage_magicquant(config: PipelineConfig, artifacts: Artifacts, log: LogFn,
                     force: bool = False, timeout: Optional[float] = None,
                     skip_preflight: bool = False) -> bool:
    """Run MagicQuant evolutionary search and generate hybrid GGUFs.

    Builds the quantization script via the shared MagicQuantService (single
    source of truth with the UI). Skips on a valid completion marker.
    """
    log("Starting MagicQuant evolutionary quantization", "stage")

    # Determine source via the shared artifact-priority resolver
    # (reap > heretic > merged > bf16 gguf). Pass it to the service as an
    # explicit override so the in-subprocess resolution lands on the same model.
    try:
        from reap_common import resolve_artifact_source
    except ImportError:  # pragma: no cover
        from core.reap_common import resolve_artifact_source
    source = resolve_artifact_source(artifacts.output_dir, require_safetensors=False)
    if source is None:
        log("No merged model or BF16 GGUF found — run export first", "error")
        return False
    log(f"Source: {source}")

    mc = config.magicquant

    cfg_hash = _markers().config_hash({
        "src": str(source), "generations": mc.generations,
        "population_size": mc.population_size, "target_base_quant": mc.target_base_quant,
        "tiers": mc.tiers, "verify": mc.verify,
        "measured": mc.measured, "measurement_rounds": mc.measurement_rounds,
        "rocmfpx_schemes": mc.rocmfpx_schemes, "iq_schemes": mc.iq_schemes,
        "seed": mc.seed,
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
    existing = sorted(artifacts.magicquant_dir.glob("*.gguf")) if artifacts.magicquant_dir.exists() else []
    key_file = existing[0] if existing else (artifacts.magicquant_dir / "_placeholder.gguf")
    if _markers().is_stage_complete(artifacts.magicquant_dir, key_file, cfg_hash, force=force):
        log(f"MagicQuant already complete (marker matches) at {artifacts.magicquant_dir} — skipping", "success")
        return True

    model_name = config.training.model_name.split("/")[-1]
    import json as _json
    svc = _services().MagicQuantService(PROJECT_ROOT, _find_python())
    script = svc.build_script(
        llamacpp_hint=mc.llamacpp_path or "",
        pipeline_root_str=str(PROJECT_ROOT),
        mq_source_override=str(source),
        out_abs_str=str(artifacts.output_dir.resolve()),
        generations=mc.generations,
        population_size=mc.population_size,
        target_base_quant=mc.target_base_quant,
        tiers_json=_json.dumps(mc.tiers),
        model_name=model_name,
        verify=mc.verify,
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

    rc = _run_stage_script(
        script, artifacts.output_dir / "_stage_magicquant.py", log,
        cfg_hash=cfg_hash, timeout=timeout,
    )
    if rc != 0:
        log(f"MagicQuant failed (exit code {rc})", "error")
        return False

    ggufs = sorted(artifacts.magicquant_dir.glob("*.gguf")) if artifacts.magicquant_dir.exists() else []
    if not ggufs:
        log("No GGUF files produced by MagicQuant", "error")
        return False
    for p in ggufs:
        log(f"  {p.name} ({p.stat().st_size / 1e9:.1f} GB)")
    try:
        _markers().write_marker(artifacts.magicquant_dir, "magicquant", ggufs[0], cfg_hash)
    except OSError as e:
        log(f"Could not write completion marker: {e}", "warn")
    log(f"Generated {len(ggufs)} hybrid GGUF files", "success")
    return True


# ── Stage: ROCmFPX ───────────────────────────────────────────────────────────

def stage_rocmfpx(config: PipelineConfig, artifacts: Artifacts, log: LogFn,
                  force: bool = False, timeout: Optional[float] = None,
                  skip_preflight: bool = False) -> bool:
    """Quantize with ROCmFPX (AMD-tuned GGUF quant family).

    Builds the quantize script via the shared ROCmFPXService (single source of
    truth with the UI). Same source priority as MagicQuant. Skips on a valid
    completion marker.
    """
    log("Starting ROCmFPX quantization", "stage")

    rc_cfg = config.rocmfpx

    if rc_cfg.source_model:
        source = rc_cfg.source_model
    else:
        try:
            from reap_common import resolve_artifact_source
        except ImportError:  # pragma: no cover
            from core.reap_common import resolve_artifact_source
        source = resolve_artifact_source(artifacts.output_dir, require_safetensors=False)
    if source is None:
        log("No merged model or BF16 GGUF found — run export first, or set "
            "ROCmFPX source_model", "error")
        return False
    log(f"Source: {source}")

    cfg_hash = _markers().config_hash({
        "src": str(source), "formats": rc_cfg.formats, "imatrix": rc_cfg.imatrix,
    })
    existing = sorted(artifacts.rocmfpx_dir.glob("*.gguf")) if artifacts.rocmfpx_dir.exists() else []
    key_file = existing[0] if existing else (artifacts.rocmfpx_dir / "_placeholder.gguf")
    if _markers().is_stage_complete(artifacts.rocmfpx_dir, key_file, cfg_hash, force=force):
        log(f"ROCmFPX already complete (marker matches) at {artifacts.rocmfpx_dir} — skipping", "success")
        return True

    _preflight_stage("rocmfpx", config, log, skip=skip_preflight)

    model_name = config.training.model_name.split("/")[-1]
    import json as _json
    svc = _services().ROCmFPXService(PROJECT_ROOT, _find_python())
    script = svc.build_script(
        rocmfpx_hint=rc_cfg.rocmfpx_hint,
        pipeline_root_str=str(PROJECT_ROOT),
        source_override=str(source),
        out_abs_str=str(artifacts.output_dir.resolve()),
        formats_json=_json.dumps(rc_cfg.formats),
        model_name=model_name,
        imatrix=rc_cfg.imatrix,
    )

    rc = _run_stage_script(
        script, artifacts.output_dir / "_stage_rocmfpx.py", log,
        cfg_hash=cfg_hash, timeout=timeout,
    )
    if rc != 0:
        log(f"ROCmFPX failed (exit code {rc})", "error")
        return False

    ggufs = sorted(artifacts.rocmfpx_dir.glob("*.gguf")) if artifacts.rocmfpx_dir.exists() else []
    if not ggufs:
        log("No GGUF files produced by ROCmFPX", "error")
        return False
    for p in ggufs:
        log(f"  {p.name} ({p.stat().st_size / 1e9:.1f} GB)")
    try:
        _markers().write_marker(artifacts.rocmfpx_dir, "rocmfpx", ggufs[0], cfg_hash)
    except OSError as e:
        log(f"Could not write completion marker: {e}", "warn")
    log(f"Generated {len(ggufs)} ROCmFPX GGUF files", "success")
    return True


# ── Stage: Upload ────────────────────────────────────────────────────────────

def _resolve_license(uc: UploadConfig, model_name: str, log: LogFn) -> str:
    """Return the license string, auto-detecting from HF if not explicitly set."""
    if uc.license:
        return uc.license
    base = uc.base_model or model_name
    log("Detecting license from base model...")
    lic = detect_license(base)
    log(f"  License: {lic}")
    return lic


def stage_upload(config: PipelineConfig, artifacts: Artifacts, log: LogFn,
                 enabled: set = None) -> bool:
    """Upload artifacts to HuggingFace Hub.

    Delegates to hf_upload module for model card generation, progress
    reporting, and file upload. Supports dry-run mode via stage_upload_dry_run().

    Args:
        enabled: set of stage names that actually ran. Used to determine
                 did_training / did_heretic / did_magicquant flags for the
                 model card. Falls back to config presence check if not provided.
    """
    from hf_upload import HFUploadConfig, upload

    uc = config.upload
    if not uc or not uc.repo_id:
        log("No repo_id configured for upload", "error")
        return False

    tc = config.training
    license_id = _resolve_license(uc, tc.model_name, log)

    # Determine which stages actually ran, not just which configs are present.
    # config.training is always non-None (non-optional field with a default),
    # so `config.training is not None` is always True -- use `enabled` instead.
    _enabled = enabled or set()
    hf_cfg = HFUploadConfig(
        repo_id=uc.repo_id,
        private=uc.private,
        license=license_id,
        upload_gguf=uc.upload_gguf,
        upload_lora=uc.upload_lora,
        upload_merged=uc.upload_merged,
        base_model=uc.base_model or tc.model_name,
        dataset_name=tc.dataset_path,
        did_training="training" in _enabled,
        did_heretic="heretic" in _enabled,
        did_reap="reap" in _enabled,
        did_magicquant="magicquant" in _enabled,
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
    )

    return upload(hf_cfg, config.output_dir, log=log)


def stage_upload_dry_run(config: PipelineConfig, artifacts: Artifacts, log: LogFn,
                         enabled: set = None):
    """Dry-run upload: validate credentials and report what would be uploaded.

    Returns a DryRunReport (from hf_upload module).

    Args:
        enabled: set of stage names that actually ran. See stage_upload().
    """
    from hf_upload import HFUploadConfig, dry_run

    uc = config.upload
    if not uc or not uc.repo_id:
        log("No repo_id configured for upload", "error")
        return None

    tc = config.training
    license_id = _resolve_license(uc, tc.model_name, log)
    _enabled = enabled or set()
    hf_cfg = HFUploadConfig(
        repo_id=uc.repo_id,
        private=uc.private,
        license=license_id,
        upload_gguf=uc.upload_gguf,
        upload_lora=uc.upload_lora,
        upload_merged=uc.upload_merged,
        base_model=uc.base_model or tc.model_name,
        dataset_name=tc.dataset_path,
        did_training="training" in _enabled,
        did_heretic="heretic" in _enabled,
        did_reap="reap" in _enabled,
        did_magicquant="magicquant" in _enabled,
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
    )

    return dry_run(hf_cfg, config.output_dir, log=log)


# ── Pipeline runner ──────────────────────────────────────────────────────────

STAGES = [
    ("training",   stage_training),
    ("export",     stage_export),
    ("heretic",    stage_heretic),
    ("reap",       stage_reap),
    ("qat",        stage_qat),
    ("magicquant", stage_magicquant),
    ("rocmfpx",    stage_rocmfpx),
    ("upload",     stage_upload),
]


def run_pipeline(config: PipelineConfig, log: LogFn = _default_log,
                 force: bool = False, stage_timeout: Optional[float] = None,
                 skip_preflight: bool = False) -> dict[str, bool]:
    """Run the full pipeline. Returns {stage_name: success/None(skipped)}.

    ``force`` re-runs every stage even when a completion marker matches.
    ``stage_timeout`` (seconds) kills a wedged stage subprocess (advisory).
    ``skip_preflight`` disables the GPU-memory preflight check.
    """
    artifacts = Artifacts(config.output_dir)
    results = {}

    enabled = set()
    if config.training is not None:
        enabled.add("training")
    if config.export is not None:
        enabled.add("export")
    if config.heretic is not None:
        enabled.add("heretic")
    if config.reap is not None:
        enabled.add("reap")
    if config.qat is not None:
        enabled.add("qat")
    if config.magicquant is not None:
        enabled.add("magicquant")
    if config.rocmfpx is not None:
        enabled.add("rocmfpx")
    if config.upload is not None:
        enabled.add("upload")

    log(f"Pipeline: {' → '.join(s for s, _ in STAGES if s in enabled)}", "stage")

    _stage_kwargs = {"force": force, "timeout": stage_timeout, "skip_preflight": skip_preflight}

    for stage_name, stage_fn in STAGES:
        if stage_name not in enabled:
            log(f"Skipping {stage_name}")
            results[stage_name] = None
            continue

        # Upload stages need to know which stages actually ran (not just
        # which configs are present) to generate accurate model cards.
        if stage_name == "upload":
            ok = stage_fn(config, artifacts, log, enabled=enabled)
        else:
            ok = stage_fn(config, artifacts, log, **_stage_kwargs)
        results[stage_name] = ok
        if not ok:
            log(f"Pipeline stopped at {stage_name}", "error")
            break

    return results


# ── Config loading ───────────────────────────────────────────────────────────

# Mapping of YAML section name -> (config attribute on PipelineConfig, dataclass).
# Used to populate every section from a YAML file, not just `training`.
_CONFIG_SECTIONS = {
    "training": ("training", TrainingConfig),
    "export": ("export", ExportConfig),
    "heretic": ("heretic", HereticConfig),
    "reap": ("reap", ReapConfig),
    "qat": ("qat", QATConfig),
    "magicquant": ("magicquant", MagicQuantConfig),
    "rocmfpx": ("rocmfpx", ROCmFPXConfig),
    "upload": ("upload", UploadConfig),
}


def _set_known_fields(obj, values: dict) -> None:
    """Set only attributes that already exist on the dataclass instance.

    Unknown YAML keys (e.g. flat-file extras like target_modules, load_in_4bit,
    save_strategy) are ignored rather than raising, so legacy flat configs load.
    """
    for k, v in values.items():
        if hasattr(obj, k):
            setattr(obj, k, v)


def load_yaml_into_config(config_path: str, cfg: "PipelineConfig") -> "PipelineConfig":
    """Populate a PipelineConfig from a YAML file.

    Accepts both layouts:
      - Nested: top-level ``training:``/``export:``/... sections (bf16-zeroclaw.yaml).
      - Flat: top-level training keys with no ``training:`` wrapper (default.yaml).
        In the flat case, all recognized training-section keys are applied to
        ``cfg.training`` so ``--config configs/default.yaml`` is no longer a no-op.

    Only fields that exist on the matching dataclass are set; unknown keys are
    ignored. For optional sections (export/heretic/reap/magicquant/upload) that
    are currently ``None`` on ``cfg``, the section is instantiated when present
    in the YAML so its values are honored.
    """
    import yaml

    with open(config_path) as f:
        data = yaml.safe_load(f) or {}

    # Detect nested layout: any top-level key matches a known section name.
    has_sections = any(k in data for k in _CONFIG_SECTIONS)

    if has_sections:
        for section, (attr, dc_cls) in _CONFIG_SECTIONS.items():
            if section not in data or not isinstance(data[section], dict):
                continue
            current = getattr(cfg, attr, None)
            if current is None:
                current = dc_cls()
                setattr(cfg, attr, current)
            _set_known_fields(current, data[section])
    else:
        # Flat layout: treat all top-level keys as training-section keys.
        _set_known_fields(cfg.training, data)

    return cfg


# ── CLI ──────────────────────────────────────────────────────────────────────

def main(argv: Optional[list[str]] = None) -> int:
    """Entry point for the `foundry` console script and `python core/pipeline.py`.

    Parses CLI arguments, builds a PipelineConfig, and either runs a dry-run
    upload validation or the full pipeline. Returns a process exit code.
    """
    import argparse

    parser = argparse.ArgumentParser(description="Training + Quantization Pipeline")
    parser.add_argument("--config", type=str, help="YAML config file")
    parser.add_argument("--output-dir", type=str, default="./output")
    parser.add_argument("--model", type=str)
    parser.add_argument("--dataset", type=str, help="Single dataset (local path or HF ID)")
    parser.add_argument("--datasets", nargs="+", help="Multiple datasets (local paths or HF IDs)")
    parser.add_argument("--no-export", action="store_true")
    parser.add_argument("--heretic", action="store_true", help="Enable heretic abliteration stage")
    parser.add_argument("--no-heretic", action="store_true", help="Disable heretic abliteration stage")
    parser.add_argument("--heretic-trials", type=int, default=200, help="Heretic Optuna trials")
    parser.add_argument("--heretic-quantization", type=str, default="bnb_4bit",
                        help="Heretic model quantization (none or bnb_4bit)")
    parser.add_argument("--reap", action="store_true", help="Enable REAP expert pruning stage (MoE only)")
    parser.add_argument("--no-reap", action="store_true", help="Disable REAP expert pruning stage")
    parser.add_argument("--reap-compression-ratio", type=float, default=0.25,
                        help="REAP compression ratio — fraction of experts to remove per layer")
    parser.add_argument("--qat", action="store_true",
                        help="Enable QAT-LoRA stage (quant-aware fine-tune; default off)")
    parser.add_argument("--no-qat", action="store_true",
                        help="Disable the QAT-LoRA stage")
    parser.add_argument("--qat-dataset", type=str, default="",
                        help="Chat JSONL dataset for QAT (required when --qat)")
    parser.add_argument("--qat-config-source", type=str, default="",
                        help="Path to a prior search_results.json for QAT "
                             "(default: auto-detect <output>/magicquant/search_results.json)")
    parser.add_argument("--qat-tier", type=str, default="Q4",
                        help="Tier within search_results.json to target for QAT (default: Q4)")
    parser.add_argument("--no-magicquant", action="store_true")
    parser.add_argument("--magicquant-measured", action="store_true",
                        help="Run MagicQuant's measured (Predict->Measure->Learn) search "
                             "with real perplexity instead of prediction-only (slower)")
    parser.add_argument("--magicquant-rounds", type=int, default=3,
                        help="Measurement rounds for --magicquant-measured (default 3)")
    parser.add_argument("--magicquant-rocmfpx", action="store_true",
                        help="Let MagicQuant's search also explore AMD-native ROCmFPX fork "
                             "types (needs a ROCmFPX build; output loads only on the fork)")
    parser.add_argument("--magicquant-iq", action="store_true",
                        help="Let MagicQuant's search also explore sub-4-bit stock-ggml "
                             "IQ schemes (IQ4_XS/IQ3_S/…; heuristic factors pending calibration)")
    parser.add_argument("--magicquant-seed", type=int, default=None,
                        help="Optional RNG seed for a reproducible MagicQuant search "
                             "(default: nondeterministic)")
    parser.add_argument("--magicquant-use-imatrix", action="store_true",
                        help="Capture/reuse an importance matrix and weight quant "
                             "noise by activation magnitude during MagicQuant search")
    parser.add_argument("--magicquant-imatrix-corpus", type=str, default=None,
                        help="Calibration corpus for --magicquant-use-imatrix "
                             "(default: bundled corpus)")
    parser.add_argument("--magicquant-kl", action="store_true",
                        help="Also measure real KL-divergence-to-base per candidate "
                             "during --magicquant-measured search")
    parser.add_argument("--magicquant-kl-weight", type=float, default=0.1,
                        help="Weight applied to |mean_kl| when blending into the "
                             "measured-search selection score (default 0.1)")
    parser.add_argument("--magicquant-speed-bench", action="store_true",
                        help="Also measure real tokens/sec per candidate (llama-bench) "
                             "during --magicquant-measured search")
    parser.add_argument("--magicquant-chunks", type=int, default=None,
                        help="Cap perplexity/KL passes to this many ctx-size chunks "
                             "instead of the whole corpus (both measured and "
                             "prediction-only search paths); default: whole corpus")
    parser.add_argument("--magicquant-stream-aware", action="store_true",
                        help="Bias MagicQuant's search sampling toward BF16->Q8_0 on "
                             "streamed matmul groups (recommended: ~+18%% gen speed, "
                             "-16%% size, PPL-neutral; ~-8%% prompt speed)")
    parser.add_argument("--magicquant-head-aggressive", action="store_true",
                        help="Bias MagicQuant's search sampling for the 'H' (LM head) "
                             "group toward smaller K-quants (superseded by "
                             "--magicquant-stream-aware for most models)")
    parser.add_argument("--magicquant-speed-aware", action="store_true",
                        help="Within each tier, prefer the fastest near-tied measured "
                             "candidate instead of the flat quality-best (measured "
                             "search only; see --magicquant-speed-metric)")
    parser.add_argument("--magicquant-speed-metric", type=str, default="bytes",
                        choices=["bytes", "bench"],
                        help="How --magicquant-speed-aware measures 'fastest': bytes "
                             "= deterministic size (default, recommended) or bench = "
                             "measured tg (needs --magicquant-speed-bench)")
    parser.add_argument("--magicquant-speed-weight", type=float, default=None,
                        help="Reserve this weight (0.0-0.8ish) for the search "
                             "objective's speed term, renormalizing precision:size to "
                             "fill the remainder (both search paths; default: "
                             "unchanged 0.50/0.35/0.15 weights)")
    parser.add_argument("--magicquant-use-bytes-tps", action="store_true",
                        help="Score the search objective's speed term deterministically "
                             "from predicted size instead of the noisy speed_multiplier "
                             "path (recommended whenever --magicquant-speed-weight is set)")
    parser.add_argument("--magicquant-calibration-source", type=str, default="",
                        help="Load empirically calibrated noise factors/speed "
                             "multipliers from this noise_calibration.json instead of "
                             "the fixed calibration_results.json path (both search paths)")
    parser.add_argument("--magicquant-write-calibration", action="store_true",
                        help="After a successful --magicquant-measured search, fit + "
                             "write <output>/noise_calibration.json from this run's "
                             "measurements")
    parser.add_argument("--magicquant-optimize-for-speed", action="store_true",
                        help="Convenience: turns on speed-aware selection (bytes "
                             "metric) AND a moderate speed-weighted, bytes-scored "
                             "search objective together (speed_aware=True, "
                             "speed_metric=bytes, speed_weight=0.35, "
                             "use_bytes_tps=True) -- override any of these "
                             "individually with the flags above")
    parser.add_argument("--rocmfpx", action="store_true",
                        help="Enable ROCmFPX stage (AMD-tuned GGUF quants; default off)")
    parser.add_argument("--no-rocmfpx", action="store_true",
                        help="Disable the ROCmFPX stage")
    parser.add_argument("--rocmfpx-formats", nargs="+", default=None,
                        help="ROCmFPX '<format>-<profile>' specs, e.g. rocmfp4-agent "
                             "rocmfp6-agent rocmfp8-agent (default: those three)")
    parser.add_argument("--rocmfpx-source-model", type=str, default="",
                        help="Path to a GGUF file or merged safetensors dir "
                             "(when export/magicquant are skipped)")
    parser.add_argument("--rocmfpx-hint", type=str, default="",
                        help="Path to an existing ROCmFPX/llama.cpp-fork build "
                             "(default: auto-detect or auto-install)")
    parser.add_argument("--upload-to", type=str, help="HF repo ID")
    parser.add_argument("--llamacpp-path", type=str)
    parser.add_argument("--dry-run", action="store_true",
                        help="Validate upload credentials and show what would be uploaded (no actual upload)")
    parser.add_argument("--force", action="store_true",
                        help="Re-run every stage even if a completion marker matches")
    parser.add_argument("--stage-timeout", type=float, default=None,
                        help="Per-stage subprocess timeout in seconds (kills a wedged stage)")
    parser.add_argument("--skip-preflight", action="store_true",
                        help="Skip the advisory GPU-memory preflight check")
    args = parser.parse_args(argv)

    cfg = PipelineConfig(output_dir=args.output_dir)

    if args.config:
        load_yaml_into_config(args.config, cfg)

    if args.model:
        cfg.training.model_name = args.model
    if args.datasets:
        cfg.training.datasets = args.datasets
    elif args.dataset:
        cfg.training.datasets = [args.dataset]
    if args.no_export:
        cfg.export = None
    if args.heretic and not args.no_heretic:
        cfg.heretic = HereticConfig(
            n_trials=args.heretic_trials,
            quantization=args.heretic_quantization,
        )
    if args.no_heretic:
        cfg.heretic = None
    if args.reap and not args.no_reap:
        cfg.reap = ReapConfig(compression_ratio=args.reap_compression_ratio)
    if args.no_reap:
        cfg.reap = None
    if args.qat and not args.no_qat:
        cfg.qat = QATConfig(
            config_source=args.qat_config_source,
            tier=args.qat_tier,
            dataset=args.qat_dataset,
        )
    if args.no_qat:
        cfg.qat = None
    if args.no_magicquant:
        cfg.magicquant = None
    if cfg.magicquant is not None:
        if args.magicquant_measured:
            cfg.magicquant.measured = True
            cfg.magicquant.measurement_rounds = args.magicquant_rounds
        if args.magicquant_rocmfpx:
            cfg.magicquant.rocmfpx_schemes = True
        if args.magicquant_iq:
            cfg.magicquant.iq_schemes = True
        if args.magicquant_seed is not None:
            cfg.magicquant.seed = args.magicquant_seed
        if args.magicquant_use_imatrix:
            cfg.magicquant.use_imatrix = True
        if args.magicquant_imatrix_corpus is not None:
            cfg.magicquant.imatrix_corpus = args.magicquant_imatrix_corpus
        if args.magicquant_kl:
            cfg.magicquant.enable_kl = True
            cfg.magicquant.kl_weight = args.magicquant_kl_weight
        if args.magicquant_speed_bench:
            cfg.magicquant.enable_speed_bench = True
        if args.magicquant_chunks is not None:
            cfg.magicquant.measurement_chunks = args.magicquant_chunks
        if args.magicquant_stream_aware:
            cfg.magicquant.stream_aware = True
        if args.magicquant_head_aggressive:
            cfg.magicquant.head_aggressive = True
        if args.magicquant_optimize_for_speed:
            cfg.magicquant.speed_aware = True
            cfg.magicquant.speed_metric = "bytes"
            cfg.magicquant.speed_weight = 0.35
            cfg.magicquant.use_bytes_tps = True
        if args.magicquant_speed_aware:
            cfg.magicquant.speed_aware = True
        # Applied independently of --magicquant-speed-aware (like
        # --magicquant-imatrix-corpus is independent of --magicquant-use-
        # imatrix) so an explicit override composes with --magicquant-
        # optimize-for-speed without also having to repeat --magicquant-
        # speed-aware.
        if args.magicquant_speed_metric != "bytes":
            cfg.magicquant.speed_metric = args.magicquant_speed_metric
        if args.magicquant_speed_weight is not None:
            cfg.magicquant.speed_weight = args.magicquant_speed_weight
        if args.magicquant_use_bytes_tps:
            cfg.magicquant.use_bytes_tps = True
        if args.magicquant_calibration_source:
            cfg.magicquant.calibration_source = args.magicquant_calibration_source
        if args.magicquant_write_calibration:
            cfg.magicquant.write_calibration = True
    if args.rocmfpx and not args.no_rocmfpx:
        cfg.rocmfpx = ROCmFPXConfig(
            source_model=args.rocmfpx_source_model,
            rocmfpx_hint=args.rocmfpx_hint,
            **({"formats": args.rocmfpx_formats} if args.rocmfpx_formats else {}),
        )
    if args.no_rocmfpx:
        cfg.rocmfpx = None
    if args.upload_to:
        cfg.upload = UploadConfig(repo_id=args.upload_to)
    if args.llamacpp_path and cfg.magicquant:
        cfg.magicquant.llamacpp_path = args.llamacpp_path

    # Dry-run mode: validate upload without running the full pipeline
    if args.dry_run:
        if not cfg.upload:
            if args.upload_to:
                cfg.upload = UploadConfig(repo_id=args.upload_to)
            else:
                print("ERROR: --dry-run requires --upload-to <repo_id>")
                return 1
        artifacts = Artifacts(cfg.output_dir)
        # Build the enabled set from config presence (same logic as run_pipeline).
        _dry_enabled = set()
        if cfg.training is not None:
            _dry_enabled.add("training")
        if cfg.export is not None:
            _dry_enabled.add("export")
        if cfg.heretic is not None:
            _dry_enabled.add("heretic")
        if cfg.reap is not None:
            _dry_enabled.add("reap")
        if cfg.qat is not None:
            _dry_enabled.add("qat")
        if cfg.magicquant is not None:
            _dry_enabled.add("magicquant")
        if cfg.rocmfpx is not None:
            _dry_enabled.add("rocmfpx")
        if cfg.upload is not None:
            _dry_enabled.add("upload")
        report = stage_upload_dry_run(cfg, artifacts, _default_log, enabled=_dry_enabled)
        return 0 if report and report.ok else 1

    results = run_pipeline(
        cfg, force=args.force, stage_timeout=args.stage_timeout,
        skip_preflight=args.skip_preflight,
    )

    print("\n" + "=" * 50)
    print("Pipeline Results:")
    for stage, ok in results.items():
        sym = "+" if ok else ("-" if ok is None else "X")
        print(f"  {sym} {stage}")

    # Non-zero exit if any enabled stage failed (None == skipped is fine).
    return 0 if all(ok is not False for ok in results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
