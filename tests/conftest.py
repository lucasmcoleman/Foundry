"""Shared pytest fixtures / path setup for the offline Foundry test suite.

Adds ``core`` to sys.path so test modules can ``import pipeline``/``services``
the same way the production code does (it inserts ``core`` onto sys.path).
"""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CORE = REPO_ROOT / "core"
UI = REPO_ROOT / "ui"

for p in (str(CORE), str(UI), str(REPO_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)
