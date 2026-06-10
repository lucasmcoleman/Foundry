"""GPU-memory preflight checks.

OOM from the shared CPU/GPU memory pool is the documented dominant failure on
the target Strix Halo APU. This module provides an advisory pre-stage check:
read free VRAM via ``torch.cuda.mem_get_info()`` (with a ``rocm-smi`` text
fallback) and abort early when free memory is below a per-stage estimate.

The check is advisory, not a guarantee — another process can grab memory after
the snapshot — so estimates are conservative and the check is overridable.
"""

import re
import subprocess
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
