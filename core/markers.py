"""Stage completion markers for resume/skip logic.

Existence-based skips are unreliable: PEFT writes ``adapter_config.json`` early,
so a crash before ``adapter_model.safetensors`` would false-pass an existence
check and skip a re-run that never actually finished. Instead, each stage writes
a ``_stage_complete.json`` marker only after the subprocess exits 0 AND the key
artifact is present and non-empty. Skips are then gated on:

  1. the marker existing,
  2. the recorded ``config_hash`` matching the current run's config, and
  3. the key artifact still being present and non-empty.

Markers are written atomically (tmp file + ``os.replace``).
"""

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any, Optional

MARKER_NAME = "_stage_complete.json"


def config_hash(config: Any) -> str:
    """Return a stable short hash for an arbitrary JSON-serializable config.

    Used to detect when a stage's inputs changed between runs (which must force
    a re-run rather than a false skip). Falls back to ``repr`` for values that
    are not directly JSON-serializable so it never raises.
    """
    try:
        blob = json.dumps(config, sort_keys=True, default=repr)
    except (TypeError, ValueError):
        blob = repr(config)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def marker_path(stage_dir: Path) -> Path:
    return Path(stage_dir) / MARKER_NAME


def _nonempty(path: Path) -> bool:
    try:
        return path.exists() and path.stat().st_size > 0
    except OSError:
        return False


def write_marker(
    stage_dir: Path,
    stage: str,
    key_file: Path,
    cfg_hash: str,
) -> Path:
    """Atomically write a completion marker for ``stage`` into ``stage_dir``.

    ``key_file`` is the primary artifact whose presence/size proves the stage
    finished. Returns the marker path.
    """
    stage_dir = Path(stage_dir)
    stage_dir.mkdir(parents=True, exist_ok=True)
    key_file = Path(key_file)
    try:
        size = key_file.stat().st_size
    except OSError:
        size = 0
    data = {
        "stage": stage,
        "timestamp": time.time(),
        "config_hash": cfg_hash,
        "key_file": str(key_file),
        "size": size,
    }
    dest = marker_path(stage_dir)
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    os.replace(tmp, dest)
    return dest


def read_marker(stage_dir: Path) -> Optional[dict]:
    """Return the parsed marker dict, or None if missing/corrupt."""
    p = marker_path(stage_dir)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def is_stage_complete(
    stage_dir: Path,
    key_file: Path,
    cfg_hash: str,
    force: bool = False,
) -> bool:
    """Decide whether ``stage_dir`` can be skipped.

    Returns True (skip) only when ``force`` is False AND a valid marker exists
    AND its ``config_hash`` matches AND the recorded key file is still present
    and non-empty. Any mismatch returns False so the stage re-runs.
    """
    if force:
        return False
    marker = read_marker(stage_dir)
    if marker is None:
        return False
    if marker.get("config_hash") != cfg_hash:
        return False
    # Prefer the key_file recorded in the marker, but also accept the caller's.
    recorded = marker.get("key_file")
    candidates = []
    if recorded:
        candidates.append(Path(recorded))
    candidates.append(Path(key_file))
    return any(_nonempty(c) for c in candidates)
