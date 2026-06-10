"""Shared logging helper for Foundry's print/WebSocket-callback model.

The pipeline does not use structured logging libraries; both the CLI and the
upload module log via a small callable that maps a level to a prefix and prints.
This module is the single source of truth for that callable so pipeline.py and
hf_upload.py don't each carry their own copy.
"""

from typing import Callable

# A log callback: (message, level) -> None. ``level`` is one of
# "info"/"error"/"warn"/"success"/"stage" (anything else falls back to info).
LogFn = Callable[[str, str], None]

_PREFIXES = {"error": "ERROR", "warn": "WARN", "success": "OK", "stage": ">>>"}


def default_log(msg: str, level: str = "info") -> None:
    """Print ``msg`` with a level-derived prefix. The default LogFn."""
    prefix = _PREFIXES.get(level, "   ")
    print(f"[{prefix}] {msg}")
