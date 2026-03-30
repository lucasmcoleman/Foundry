#!/usr/bin/env python3
"""Run MagicQuant + Upload stages using the pipeline's built-in functions."""

import sys
sys.path.insert(0, ".")

from pipeline import (
    PipelineConfig, TrainingConfig, MagicQuantConfig, UploadConfig, Artifacts,
    stage_magicquant, stage_upload, _default_log,
)

MODEL_ID = "DavidAU/Qwen3.5-40B-Claude-4.6-Opus-Deckard-Heretic-Uncensored-Thinking"
OUTPUT_DIR = "./output-zeroclaw-qwen40b"
HF_REPO = "lmcoleman/Qwen3.5-40B-Claude-4.6-Opus-Deckard-Heretic-Uncensored-Thinking-Zeroclaw-GGUF"

config = PipelineConfig(
    training=TrainingConfig(model_name=MODEL_ID),
    magicquant=MagicQuantConfig(),
    upload=UploadConfig(
        repo_id=HF_REPO,
        private=False,
        base_model=MODEL_ID,
        upload_gguf=True,
        upload_lora=True,
    ),
)

artifacts = Artifacts(OUTPUT_DIR)

print("=" * 60)
print("Stage: MagicQuant")
print("=" * 60)

ok = stage_magicquant(config, artifacts, _default_log)
if not ok:
    print("MagicQuant failed!")
    sys.exit(1)

print("\n" + "=" * 60)
print("Stage: Upload to HuggingFace")
print("=" * 60)

ok = stage_upload(config, artifacts, _default_log)
if not ok:
    print("Upload failed!")
    sys.exit(1)

print("\nAll done!")
print(f"https://huggingface.co/{HF_REPO}")
