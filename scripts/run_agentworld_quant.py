#!/usr/bin/env python3
"""Run MagicQuant + ROCmFPX against the pre-downloaded Qwen-AgentWorld-35B-A3B
base model (training/export skipped -- no dataset involved, quantizing the
base model directly), mirroring scripts/run_magicquant_upload.py's pattern of
calling pipeline stage functions directly."""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "core"))

from pipeline import (
    PipelineConfig, TrainingConfig, MagicQuantConfig, ROCmFPXConfig, Artifacts,
    stage_magicquant, stage_rocmfpx, _default_log,
)

MODEL_ID = "Qwen/Qwen-AgentWorld-35B-A3B"
OUTPUT_DIR = "./output/agentworld-35b"

config = PipelineConfig(
    training=TrainingConfig(model_name=MODEL_ID),
    magicquant=MagicQuantConfig(),
    rocmfpx=ROCmFPXConfig(),
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
print("Stage: ROCmFPX")
print("=" * 60)

ok = stage_rocmfpx(config, artifacts, _default_log)
if not ok:
    print("ROCmFPX failed!")
    sys.exit(1)

print("\nAll done!")
