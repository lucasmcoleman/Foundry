"""Stage completion markers for resume/skip logic.

Existence-based skips are unreliable: PEFT writes ``adapter_config.json`` early,
so a crash before ``adapter_model.safetensors`` would false-pass an existence
check and skip a re-run that never actually finished. Instead, each stage writes
a ``_stage_complete.json`` marker only after the subprocess exits 0 AND the key
artifact is present and non-empty. Skips are then gated on:

  1. the marker existing,
  2. the recorded ``config_hash`` matching the current run's config, and
  3. the key artifact and recorded artifact inventory remaining unchanged.

Markers are written atomically (tmp file + ``os.replace``).
"""

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any, Optional

MARKER_NAME = "_stage_complete.json"
_ARTIFACT_NAMES = {
    "adapter_model.bin", "config.json", "adapter_config.json", "qat_meta.json",
    "tokenizer.json", "tokenizer_config.json", "tokenizer.model",
    "special_tokens_map.json", "chat_template.jinja",
    "generation_config.json", "spiece.model", "vocab.json", "vocab.txt",
    "merges.txt", "added_tokens.json", "preprocessor_config.json",
    "processor_config.json", "video_preprocessor_config.json",
}


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
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


def _file_state(path: Path) -> dict:
    stat = path.stat()
    return {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns}


def _artifact_inventory(stage_dir: Path) -> dict:
    """Record all model shards and essential metadata without reading weights."""
    return {
        p.name: _file_state(p)
        for p in sorted(stage_dir.iterdir())
        if p.is_file() and (
            p.suffix in {".safetensors", ".gguf"}
            or p.name.endswith(".safetensors.index.json")
            or p.name in _ARTIFACT_NAMES
        )
    }


def source_fingerprint(source: Path | str | None) -> dict | None:
    """Fingerprint local inputs for resume; remote IDs remain config values.

    Uses size and nanosecond mtime instead of hashing multi-GB weight files.
    This detects normal in-place edits/replacements, not adversarial corruption.
    Directories include model artifacts and metadata, excluding runtime logs.
    """
    if not source:
        return None
    path = Path(source)
    try:
        if path.is_file():
            return {"path": str(path.resolve()), **_file_state(path)}
        if path.is_dir():
            return {"path": str(path.resolve()), "files": _artifact_inventory(path)}
    except OSError:
        return {"path": str(path), "unreadable": True}
    return None


def invalidate_marker(stage_dir: Path) -> None:
    """Invalidate a prior success before a stage starts rewriting its outputs."""
    marker_path(stage_dir).unlink(missing_ok=True)


def artifacts_present(stage_dir: Path, key_file: Path) -> bool:
    """Require a nonempty file and every shard declared by a model index."""
    stage_dir = Path(stage_dir)
    if not _nonempty(Path(key_file)):
        return False
    try:
        inventory = _artifact_inventory(stage_dir)
        if any(v["size"] <= 0 for v in inventory.values()):
            return False
        if Path(key_file).name == "qat_meta.json":
            meta = json.loads(Path(key_file).read_text())
            adapter = meta.get("adapter_file", "adapter_model.safetensors") if isinstance(meta, dict) else None
            if not isinstance(adapter, str):
                return False
            path = stage_dir / adapter
            if not adapter or Path(adapter).is_absolute() or ".." in Path(adapter).parts or not _nonempty(path):
                return False
        for index in stage_dir.glob("*.safetensors.index.json"):
            data = json.loads(index.read_text())
            weight_map = data.get("weight_map") if isinstance(data, dict) else None
            if not isinstance(weight_map, dict) or not weight_map:
                return False
            for shard in weight_map.values():
                if not isinstance(shard, str):
                    return False
                path = stage_dir / shard
                if not shard or Path(shard).is_absolute() or ".." in Path(shard).parts or not _nonempty(path):
                    return False
    except (OSError, ValueError):
        return False
    return True


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
        "key_file": str(key_file.resolve()),
        "size": size,
        "artifacts": _artifact_inventory(stage_dir),
        "key_state": _file_state(key_file) if key_file.is_file() else None,
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
        data = json.loads(p.read_text())
        return data if isinstance(data, dict) else None
    except (ValueError, OSError):
        return None


def is_stage_complete(
    stage_dir: Path,
    key_file: Path,
    cfg_hash: str,
    force: bool = False,
) -> bool:
    """Decide whether ``stage_dir`` can be skipped.

    Returns True (skip) only when ``force`` is False AND a valid marker exists
    AND its ``config_hash`` matches AND the requested key file and all recorded
    artifacts remain present and unchanged. Legacy markers validate key size;
    newly written markers also check timestamps and every sibling artifact.
    """
    if force:
        return False
    marker = read_marker(stage_dir)
    if marker is None:
        return False
    if marker.get("config_hash") != cfg_hash:
        return False
    recorded = marker.get("key_file")
    if not isinstance(recorded, str) or not recorded:
        return False
    key_file = Path(key_file)
    try:
        if Path(recorded).resolve() != key_file.resolve() or not artifacts_present(stage_dir, key_file):
            return False
        if marker.get("size") != key_file.stat().st_size:
            return False
        if "key_state" in marker and marker["key_state"] != _file_state(key_file):
            return False
        if "artifacts" in marker:
            current = _artifact_inventory(Path(stage_dir))
            if marker["artifacts"] != current or any(v["size"] <= 0 for v in current.values()):
                return False
    except (OSError, ValueError):
        return False
    return True
