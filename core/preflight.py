"""GPU-memory and system-memory preflight checks.

OOM from the shared CPU/GPU memory pool is the documented dominant failure on
the target Strix Halo APU. This module provides advisory pre-stage checks:
read free VRAM via ``torch.cuda.mem_get_info()`` (with a ``rocm-smi`` text
fallback) and abort early when free memory is below a per-stage estimate.

The check is advisory, not a guarantee — another process can grab memory after
the snapshot — so estimates are conservative and the check is overridable.

System-memory checks (``check_system_memory`` et al.) were added after an
incident where a resident llama-swap model held ~21 GB of GTT (pinned,
unreclaimable, drawn from the shared 121 GB CPU/GPU pool) when a pipeline run
launched on top of it; the kernel OOM-killed bystander processes (dbus,
docker) and the box livelocked. The gate's primary signal is system
MemAvailable — GTT-in-use is informational only and never blocks, because the
whole point is to let a run proceed whenever MemAvailable is actually
sufficient, resident model or not.
"""

import os
import re
import subprocess
from pathlib import Path
from typing import Callable, Optional, Tuple

LogFn = Callable[[str, str], None]


def _default_log(msg: str, level: str = "info") -> None:
    print(f"[preflight] {msg}")


def parse_rocm_smi_free_gb(text: str) -> Optional[float]:
    """Parse free VRAM (GB) from ``rocm-smi --showmeminfo vram`` output.

    Looks for the total and used VRAM byte counts and returns free GB.
    Returns None if the expected fields are not present.
    """
    total = used = None
    for line in text.splitlines():
        m_total = re.search(r"VRAM Total Memory \(B\)\s*:\s*(\d+)", line)
        if m_total:
            total = int(m_total.group(1))
        m_used = re.search(r"VRAM Total Used Memory \(B\)\s*:\s*(\d+)", line)
        if m_used:
            used = int(m_used.group(1))
    if total is None or used is None:
        return None
    return max(0.0, (total - used) / 1e9)


def _rocm_smi_free_gb() -> Optional[float]:
    try:
        out = subprocess.run(
            ["rocm-smi", "--showmeminfo", "vram"],
            capture_output=True, text=True, timeout=15,
        )
    except (FileNotFoundError, OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return parse_rocm_smi_free_gb(out.stdout)


def get_free_vram_gb() -> Optional[float]:
    """Return free GPU memory in GB, or None if it cannot be determined."""
    try:
        import torch
        if torch.cuda.is_available():
            free, _total = torch.cuda.mem_get_info()
            return free / 1e9
    except Exception:
        pass
    return _rocm_smi_free_gb()


def check_gpu_memory(
    estimated_gb: float,
    log: LogFn = _default_log,
    skip: bool = False,
) -> bool:
    """Return True if it is safe to proceed (or the check is skipped/unknown).

    Returns False only when free VRAM is known AND below ``estimated_gb``.
    When the amount of free memory cannot be determined, returns True with a
    warning (advisory check, never a hard blocker on unknown).
    """
    if skip:
        log(f"GPU preflight skipped (estimate was {estimated_gb:.1f} GB)", "warn")
        return True
    free = get_free_vram_gb()
    if free is None:
        log("Could not determine free GPU memory — proceeding without preflight", "warn")
        return True
    if free < estimated_gb:
        log(
            f"Insufficient GPU memory: ~{free:.1f} GB free, "
            f"~{estimated_gb:.1f} GB estimated needed. "
            "Free memory (e.g. unload LM Studio) or pass --skip-preflight.",
            "error",
        )
        return False
    log(f"GPU preflight OK: ~{free:.1f} GB free >= ~{estimated_gb:.1f} GB needed")
    return True


def estimate_params_b(config_path: Optional[str]) -> Optional[float]:
    """Best-effort parameter count (billions) from a model's config.json.

    Uses hidden_size, num_hidden_layers, vocab_size as a rough dense estimate.
    Returns None when the file is missing or lacks the needed fields.
    """
    if not config_path:
        return None
    import json
    from pathlib import Path
    p = Path(config_path)
    if not p.exists():
        return None
    try:
        cfg = json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    h = cfg.get("hidden_size")
    layers = cfg.get("num_hidden_layers")
    vocab = cfg.get("vocab_size")
    if not (h and layers):
        return None
    # ~12 * layers * hidden^2 for transformer blocks, plus embedding/head.
    params = 12 * layers * (h ** 2)
    if vocab:
        params += 2 * vocab * h
    return params / 1e9


def estimate_stage_gb(stage: str, params_b: Optional[float]) -> float:
    """Conservative per-stage GPU memory estimate in GB.

    ``training`` dominates (optimizer state + activations); ``export``/merge is
    light (streaming). Scales with model size when known, with sane defaults.
    """
    pb = params_b if params_b else 40.0  # assume a large model if unknown
    if stage == "training":
        # ~0.75 GB/B params for 4-bit QLoRA + LoRA grads + activations.
        return max(8.0, 0.75 * pb)
    if stage in ("export", "merge"):
        return max(4.0, 0.15 * pb)
    if stage == "heretic":
        return max(6.0, 0.5 * pb)
    return max(4.0, 0.3 * pb)


# ── System-memory (MemAvailable) preflight ──────────────────────────────────
#
# Separate from the GPU-VRAM checks above: this is the PRIMARY gate against
# the hard-freeze failure mode (see module docstring). GTT usage is surfaced
# as an informational warning only — it is never itself a blocking signal,
# because a resident model pinning GTT does not necessarily leave the system
# short on MemAvailable.

def parse_meminfo_available_gb(text: str) -> Optional[float]:
    """Parse ``MemAvailable: <kB> kB`` from ``/proc/meminfo`` content.

    MemAvailable (not MemFree) is used because it already accounts for
    reclaimable caches/buffers — it is the kernel's own estimate of memory
    available to a new process without swapping, which is exactly the
    question this gate needs answered. Returns None if the field is missing
    or malformed.
    """
    m = re.search(r"^MemAvailable:\s*(\d+)\s*kB", text, re.MULTILINE)
    if not m:
        return None
    return int(m.group(1)) * 1024 / 1e9


def get_mem_available_gb() -> Optional[float]:
    """Return system MemAvailable in GB, or None if it cannot be determined."""
    try:
        text = Path("/proc/meminfo").read_text()
    except OSError:
        return None
    return parse_meminfo_available_gb(text)


def get_gtt_used_gb() -> Optional[float]:
    """Return total in-use GTT (Graphics Translation Table) memory, in GB.

    GTT lets AMD APUs/iGPUs address system RAM as GPU memory; a resident
    model (llama-swap, LM Studio) can pin many GB of it, unreclaimable by the
    kernel. Summed across all ``/sys/class/drm/card*/device/mem_info_gtt_used``
    files (verified present on this box as
    ``/sys/class/drm/card1/device/mem_info_gtt_used``). Returns None if no
    such files exist or every read fails — this value is informational only
    (see ``check_system_memory``) and never gates the check on its own.
    """
    total_bytes = 0
    found = False
    for p in Path("/sys/class/drm").glob("card*/device/mem_info_gtt_used"):
        try:
            total_bytes += int(p.read_text().strip())
            found = True
        except (OSError, ValueError):
            continue
    if not found:
        return None
    return total_bytes / 1e9


def estimate_stage_system_gb(stage: str) -> float:
    """Conservative flat system-RAM (MemAvailable) requirement per stage, in GB.

    Flat rather than model-size-scaled: unlike the GPU-VRAM estimates above,
    the dominant system-RAM costs here are streaming I/O buffers, subprocess
    overhead, and (for magicquant/rocmfpx) llama.cpp loading the model into
    GTT — not host-resident tensors that scale cleanly with param count.
    """
    constants = {
        "training": 32.0,    # shard-by-shard BnB loading + optimizer state + host-side activation buffers during shard swaps
        "heretic": 24.0,     # Optuna trials repeatedly load/hold the merged model for directional-ablation search
        "export": 12.0,      # streaming shard-by-shard LoRA merge is light, but source shard + merged shard buffers briefly overlap
        "magicquant": 48.0,  # dominates: probe/tier GGUF writes + llama-perplexity loading the full model into GTT for measured search
        "rocmfpx": 16.0,     # llama-quantize conversion pass (+ tensor-type-file hybrid reproduction of a MagicQuant tier)
        "qat": 32.0,         # frozen base held fake-quantized in host RAM while LoRA adapters train against it
    }
    return constants.get(stage, 12.0)  # default: conservative floor for lighter/unlisted stages (e.g. upload, reap)


def check_system_memory(
    stage: str,
    log: LogFn = _default_log,
    skip: bool = False,
) -> bool:
    """Return True if it is safe to proceed on system-memory grounds.

    PRIMARY GATE is system MemAvailable — see module docstring for the
    incident this exists to prevent. GTT-in-use is logged as an informational
    warning only and NEVER blocks: the goal is for runs to proceed whenever
    MemAvailable is actually sufficient, resident model loaded or not.

    Requirement resolution: the ``FOUNDRY_MIN_AVAILABLE_GB`` env var (float),
    when set, overrides every stage's requirement; otherwise
    ``estimate_stage_system_gb(stage)`` is used. The whole check is bypassed
    (returns True, logs that it was skipped) when ``skip`` is True or
    ``FOUNDRY_SKIP_MEM_PREFLIGHT=1`` is set.

    Returns False only when MemAvailable is known AND below the requirement —
    unknown readings never block (advisory check, matching
    ``check_gpu_memory``'s existing contract).
    """
    override = os.environ.get("FOUNDRY_MIN_AVAILABLE_GB")
    if override:
        try:
            required_gb = float(override)
        except ValueError:
            required_gb = estimate_stage_system_gb(stage)
    else:
        required_gb = estimate_stage_system_gb(stage)

    if skip or os.environ.get("FOUNDRY_SKIP_MEM_PREFLIGHT") == "1":
        log(
            f"System-memory preflight skipped for '{stage}' "
            f"(requirement was ~{required_gb:.1f} GB)",
            "warn",
        )
        return True

    gtt_used = get_gtt_used_gb()
    if gtt_used is not None and gtt_used > 4.0:
        log(
            f"~{gtt_used:.1f} GB of GPU GTT is currently pinned, likely by a "
            "resident model (llama-swap / LM Studio) — informational only, "
            "not blocking this run.",
            "warn",
        )

    available = get_mem_available_gb()
    if available is None:
        log("Could not determine system MemAvailable — proceeding without preflight", "warn")
        return True

    if available < required_gb:
        log(
            f"Insufficient system memory for '{stage}': ~{available:.1f} GB "
            f"MemAvailable, ~{required_gb:.1f} GB estimated needed. Unload "
            "resident models (e.g. llama-swap / LM Studio) to free RAM, or "
            "override via FOUNDRY_MIN_AVAILABLE_GB, or skip via "
            "FOUNDRY_SKIP_MEM_PREFLIGHT=1.",
            "error",
        )
        return False

    log(f"System-memory preflight OK: ~{available:.1f} GB MemAvailable >= ~{required_gb:.1f} GB needed")
    return True
