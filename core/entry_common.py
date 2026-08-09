"""Shared stdlib-only helpers used by the core/_<stage>_entry.py modules (audit H2).

Each entry module must stay dependency-free at import time (torch/transformers/etc.
are imported lazily inside run()) so config parsing stays unit-testable without a
GPU. This module holds logic identical across multiple entry modules so it has one
implementation instead of several hand-kept-in-sync copies.
"""

from __future__ import annotations

from pathlib import Path


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
