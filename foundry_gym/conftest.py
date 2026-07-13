"""Pytest bootstrap: make ``import foundry_gym`` work regardless of cwd.

Inserts the Foundry repo root (the parent of this foundry_gym/ package) at
the front of sys.path, so ``pytest foundry_gym/`` works whether invoked from
the repo root or elsewhere.
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
