"""Durable publish-stage artifacts: the numbers a model card is built from.

The publish stage measures things a card needs -- ROCmFPX perplexity and
tokens/sec, and which tiers the build refused to produce -- and historically
wrote them only to a log. Logs get rotated, and the cleanup stage deletes them
outright, so the card generator was left regex-scraping text that might not
exist. That is how a fork-only card ended up showing no throughput at all,
despite throughput being the entire reason that family exists.

This module owns the on-disk contract for those records, with the reader and
writer in ONE place so they cannot drift apart. It lives in `core/` and not in
`output/` deliberately: `output/` is gitignored, and publishing criteria kept
there went untested long enough for several model-card bugs to reach public
pages before anyone noticed.

Every write is advisory. A record that fails to persist must never fail a
publish -- the card degrades to showing fewer numbers, which is a far better
outcome than a failed upload.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

MEASUREMENTS_FILENAME = "_measurements.json"


def measurements_path(family_dir: Path | str) -> Path:
    """Where a family's measurement record lives, e.g. ``<out>/rocmfpx/``."""
    return Path(family_dir) / MEASUREMENTS_FILENAME


def write_measurements(family_dir: Path | str, entries, log=None) -> bool:
    """Persist per-file measurements next to the GGUFs they describe.

    Records EVERY candidate, not just the ones that shipped: a rejected tier's
    numbers are exactly what justify the rejection, and the card discloses
    those rejections.

    Idempotent -- keyed on ``name``, so re-running a build replaces an entry
    rather than appending a duplicate or leaving stale figures behind.

    Args:
        family_dir: the quant family's output directory (``<out>/rocmfpx``).
        entries: dicts carrying at least ``name``; conventionally also
            ``tier``, ``gib``, ``ppl``, ``pp512``, ``tg128``, ``mq_peer_tg``.
        log: optional callable for progress/warnings.

    Returns:
        True when the record is on disk, False when it could not be written.
        Never raises.
    """
    def _say(msg):
        if log:
            log(msg)

    path = measurements_path(family_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        merged: dict[str, dict] = {}
        if path.exists():
            try:
                existing = json.loads(path.read_text())
                if isinstance(existing, list):
                    for e in existing:
                        if isinstance(e, dict) and e.get("name"):
                            merged[e["name"]] = e
            except (ValueError, OSError):
                # A corrupt record is not worth failing over, and not worth
                # preserving either -- it gets replaced by this run's data.
                _say(f"  {path.name} was unreadable; rewriting it")
        for e in entries:
            if isinstance(e, dict) and e.get("name"):
                merged[e["name"]] = e
        path.write_text(
            json.dumps(sorted(merged.values(), key=lambda x: x["name"]), indent=2)
            + "\n"
        )
        _say(f"  wrote {len(merged)} measurement(s) to {path.name}")
        return True
    except (OSError, TypeError, ValueError) as e:
        _say(f"  WARNING: could not write {path.name} ({type(e).__name__}) -- "
             f"the card will simply show fewer numbers")
        return False


def read_measurements(family_dir: Path | str) -> dict[str, dict]:
    """Load a family's measurements as ``{filename: entry}``.

    Returns an empty dict when the record is missing, unreadable, or the wrong
    shape. Absent measurements omit a card column; they never block a card.
    """
    path = measurements_path(family_dir)
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(data, list):
        return {}
    return {
        e["name"]: e
        for e in data
        if isinstance(e, dict) and e.get("name")
    }


def find_measurements(
    files_to_upload: list[tuple[Path, str]], family: str = "rocmfpx"
) -> dict[str, dict]:
    """Locate and load measurements given the files about to be uploaded.

    Convenience for the card generator, which knows the files rather than the
    directory.
    """
    for local_path, _ in files_to_upload:
        parent = Path(local_path).parent
        if parent.name == family:
            found = read_measurements(parent)
            if found:
                return found
    return {}
