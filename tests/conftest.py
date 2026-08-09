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

# ── HF-upload test helpers ──────────────────────────────────────────────────
# Shared by test_hf_upload_budget_card.py, test_hf_upload_budget_regex_fallback.py,
# and test_card_repo_consistency.py, which previously each carried byte-identical
# copies.

GIB_96 = 96 * 1024 ** 3


def fake_gguf(path: Path, size_bytes: int = GIB_96) -> Path:
    """A sparse file of the given size -- stat() reports it accurately
    without actually allocating/writing gigabytes of real bytes (which
    hung the test run the first time this was tried with b"x" * N)."""
    with open(path, "wb") as f:
        f.truncate(size_bytes)
    return path


def hf_upload_cfg(**overrides):
    from hf_upload import HFUploadConfig

    base = dict(
        repo_id="user/model-MagicQuant-GGUF",
        base_model="org/base",
        dataset_name="mydata",
        did_training=True,
        did_magicquant=True,
    )
    base.update(overrides)
    return HFUploadConfig(**base)
