#!/usr/bin/env python3
"""Produce a ROCmFPX MagicQuant-hybrid (mq-q4) of the pre-quantized
Qwen-AgentWorld-35B-A3B: MagicQuant's Q4 per-group layout expressed in
AMD-native ROCmFPX types. Validates the Layer 1 composition end-to-end."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "core"))
from pipeline import (
    PipelineConfig, TrainingConfig, ROCmFPXConfig, Artifacts,
    stage_rocmfpx, _default_log,
)
config = PipelineConfig(
    training=TrainingConfig(model_name="Qwen/Qwen-AgentWorld-35B-A3B"),
    magicquant=None,
    rocmfpx=ROCmFPXConfig(formats=["mq-q4"]),
)
artifacts = Artifacts("./output/agentworld-35b")
print("=" * 60); print("Stage: ROCmFPX (MagicQuant-hybrid mq-q4)"); print("=" * 60)
ok = stage_rocmfpx(config, artifacts, _default_log)
print("MQHYBRID_DONE" if ok else "MQHYBRID_FAILED", flush=True)
sys.exit(0 if ok else 1)
