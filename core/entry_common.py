"""Shared stdlib-only helpers used by the core/_<stage>_entry.py modules (audit H2).

Each entry module must stay dependency-free at import time (torch/transformers/etc.
are imported lazily inside run()) so config parsing stays unit-testable without a
GPU. This module holds logic identical across multiple entry modules so it has one
implementation instead of several hand-kept-in-sync copies.
"""

from __future__ import annotations

from pathlib import Path

# ROCm env vars each entry module's run() sets before importing torch (must run
# first, preserving the original ordering requirement). Shared by every entry
# module except _reap_entry.py, whose copy is intentionally a 3-key subset
# (missing UNSLOTH_SKIP_TORCHVISION_CHECK) -- left untouched here pending a
# decision on whether that's deliberate or a gap (see CHANGELOG).
ROCM_ENV = {
    "HSA_ENABLE_SDMA": "0",
    "PYTORCH_HIP_ALLOC_CONF": "backend:native,expandable_segments:True",
    "UNSLOTH_SKIP_TORCHVISION_CHECK": "1",
    "TORCH_ROCM_AOTRITON_ENABLE_EXPERIMENTAL": "1",
}


def hf_cache_probe(model_id: str) -> None:
    """Log whether the model is local / cached / will be downloaded (info only)."""
    if Path(model_id).exists():
        print(f"Loading from local path: {model_id}", flush=True)
        return
    try:
        from huggingface_hub import scan_cache_dir

        for repo in scan_cache_dir().repos:
            if repo.repo_id == model_id:
                print(
                    f"Model found in HF cache ({repo.size_on_disk / 1e9:.1f} GB) — "
                    "no download needed",
                    flush=True,
                )
                return
        print(f"Model not in cache — will download from HuggingFace: {model_id}", flush=True)
    except Exception:
        print(f"Loading model: {model_id}", flush=True)
