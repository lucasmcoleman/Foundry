"""Importable entry module for the LoRA-merge / export stage (audit H2).

``ExportService.build_script`` emits a thin shim that writes a JSON config and
invokes ``core/_export_entry.py:run()``. The streaming shard-by-shard merge lives
in ``core/fast_export.py``; this module is the typed, importable launcher for it.

Module import is stdlib-only; fast_export (and torch through it) is imported
lazily inside ``run()`` so config parsing is unit-testable without a GPU.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_ROCM_ENV = {
    "HSA_ENABLE_SDMA": "0",
    "PYTORCH_HIP_ALLOC_CONF": "backend:native,expandable_segments:True",
    "UNSLOTH_SKIP_TORCHVISION_CHECK": "1",
    "TORCH_ROCM_AOTRITON_ENABLE_EXPERIMENTAL": "1",
}


def parse_config(cfg_path: str) -> dict:
    return json.loads(Path(cfg_path).read_text())


def run(cfg_path: str | None = None) -> None:
    import os

    for k, v in _ROCM_ENV.items():
        os.environ.setdefault(k, v)

    if cfg_path is None:
        cfg_path = sys.argv[1]
    cfg = parse_config(cfg_path)

    core_path = str(Path(cfg["pipeline_root"]) / "core")
    if core_path not in sys.path:
        sys.path.insert(0, core_path)

    from fast_export import streaming_merge

    try:
        from entry_common import hf_cache_probe
    except ImportError:
        from core.entry_common import hf_cache_probe
    hf_cache_probe(cfg["base_model_id"])
    lora_dir = cfg["lora_source"] if cfg["has_lora"] else None
    streaming_merge(
        model_id=cfg["base_model_id"],
        lora_dir=lora_dir,
        merged_dir=cfg["merged_dir"],
    )
    print("PIPELINE_STAGE_COMPLETE=export", flush=True)


if __name__ == "__main__":
    run()
