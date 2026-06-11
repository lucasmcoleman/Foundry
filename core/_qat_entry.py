"""Importable entry module for the QAT-LoRA stage (T9).

``QATService.build_script`` emits a thin shim that writes a JSON config and
invokes ``core/_qat_entry.py:run()``. The quantization-aware fine-tune itself
lives in MagicQuant's ``magicquant.qat.run_qat``; this module is the typed,
importable launcher for it (subprocess context, like ``_magicquant_entry.py`` /
``_export_entry.py``).

Module import is stdlib-only; ``magicquant.qat`` (and torch through it) is
imported lazily inside ``run()`` so config parsing is unit-testable without the
``[qat]`` extra installed.
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

    # ``pipeline_root`` is carried by the shim for sys.path consistency with the
    # other stages, but it is not a run_qat key — strip it before dispatch.
    pipeline_root = cfg.pop("pipeline_root", None)
    if pipeline_root:
        core_path = str(Path(pipeline_root) / "core")
        if core_path not in sys.path:
            sys.path.insert(0, core_path)

    # run_qat is imported from the submodule (not the package __init__, which
    # stays light to avoid pulling transformers/trl at import time).
    from magicquant.qat.train import run_qat

    config_src = cfg.get("config")
    tier = cfg.get("tier")
    print(
        f"Starting QAT-LoRA: model={cfg.get('model')!r} "
        f"config={config_src!r} tier={tier!r} dataset={cfg.get('dataset')!r}",
        flush=True,
    )

    out = run_qat(cfg)
    print(f"QAT adapters written to: {out}", flush=True)
    print("PIPELINE_STAGE_COMPLETE=qat", flush=True)


if __name__ == "__main__":
    run()
