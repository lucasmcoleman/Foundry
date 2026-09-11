"""Local training provenance: never resume optimizer state from another run."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

try:
    from markers import config_hash, source_fingerprint
except ImportError:
    from core.markers import config_hash, source_fingerprint

STATE_NAME = "_training_identity.json"
# Changing supervision semantics must never silently resume an older trainer.
LOSS_MASK_SCHEMA = "assistant-token-offsets-v1"
_CHECKPOINT_RE = re.compile(r"checkpoint-(\d+)$")


def prepare_training_run(output_dir: str, cfg: dict) -> str | None:
    """Return a complete, compatible checkpoint or initialize run provenance.

    Existing checkpoints with unknown/different provenance are preserved and
    rejected. Use a new output directory to start a different configuration.
    This runs before model loading, so a mismatch costs no GPU time.
    """
    output = Path(output_dir)
    candidates = sorted(
        (p for p in output.glob("checkpoint-*")
         if p.is_dir() and _CHECKPOINT_RE.fullmatch(p.name)),
        key=lambda p: int(p.name.removeprefix("checkpoint-")),
        reverse=True,
    )
    root = Path(cfg.get("pipeline_root", "."))

    def fingerprint(source):
        local = Path(source)
        if not local.is_absolute() and (root / local).exists():
            local = root / local
        return source_fingerprint(local)

    identity = config_hash({
        "schema": LOSS_MASK_SCHEMA,
        "config": {k: v for k, v in cfg.items() if k not in {"output_dir", "pipeline_root"}},
        "model": fingerprint(cfg["model_name"]),
        "datasets": [fingerprint(p) for p in (cfg.get("datasets") or [])],
    })
    state_path = output / STATE_NAME
    if candidates:
        try:
            state = json.loads(state_path.read_text())
        except (OSError, ValueError):
            state = None
        if not isinstance(state, dict) or state.get("identity") != identity:
            raise ValueError(
                f"Existing training checkpoints in {output} have missing or different "
                "configuration, data, or loss-mask provenance. Use a new output_dir "
                "for this run; existing checkpoints have been preserved."
            )
        for checkpoint in candidates:
            required = [checkpoint / name for name in (
                "trainer_state.json", "optimizer.pt", "scheduler.pt",
            )]
            weights = [checkpoint / name for name in (
                "adapter_model.safetensors", "adapter_model.bin", "model.safetensors",
            )]
            if not (all(p.is_file() and p.stat().st_size > 0 for p in required)
                    and any(p.is_file() and p.stat().st_size > 0 for p in weights)):
                continue
            try:
                trainer_state = json.loads(required[0].read_text())
            except (OSError, ValueError):
                continue
            if isinstance(trainer_state, dict) and trainer_state.get("global_step") == int(checkpoint.name[11:]):
                return str(checkpoint)
        raise ValueError(f"No complete trainer checkpoint in {output}; use a new output_dir")

    output.mkdir(parents=True, exist_ok=True)
    temporary = state_path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps({"identity": identity, "loss_mask_schema": LOSS_MASK_SCHEMA}, indent=2))
    os.replace(temporary, state_path)
    return None
