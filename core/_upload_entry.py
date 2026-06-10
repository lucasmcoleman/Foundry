"""Importable entry module for the HuggingFace upload stage (audit H2).

``UploadService.build_script`` emits a thin shim that writes a JSON config and
invokes ``core/_upload_entry.py:run()``. The upload itself (model-card
generation + Hub push) lives in ``core/hf_upload.py``; this is the typed,
importable launcher for it. Module import is stdlib-only.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Keys forwarded verbatim from the JSON config into HFUploadConfig.
_HF_CONFIG_KEYS = (
    "repo_id", "private", "license", "upload_gguf", "upload_lora", "upload_merged",
    "upload_dataset", "base_model", "dataset_name", "did_training", "did_heretic",
    "did_reap", "did_magicquant", "lora_r", "lora_alpha", "lora_dropout",
    "num_epochs", "learning_rate", "max_seq_length", "batch_size",
    "gradient_accumulation", "optimizer", "lr_scheduler",
)


def parse_config(cfg_path: str) -> dict:
    return json.loads(Path(cfg_path).read_text())


def run(cfg_path: str | None = None) -> None:
    if cfg_path is None:
        cfg_path = sys.argv[1]
    cfg = parse_config(cfg_path)

    core_path = str(Path(cfg["pipeline_root"]) / "core")
    if core_path not in sys.path:
        sys.path.insert(0, core_path)

    from hf_upload import HFUploadConfig, upload

    hf_cfg = HFUploadConfig(**{k: cfg[k] for k in _HF_CONFIG_KEYS})
    ok = upload(hf_cfg, cfg["out_abs"])
    if ok:
        print("PIPELINE_STAGE_COMPLETE=upload", flush=True)
    else:
        sys.exit(1)


if __name__ == "__main__":
    run()
