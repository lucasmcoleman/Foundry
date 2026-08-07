"""Guards the BUDGET_FILE_RE import-fallback fix in core/hf_upload.py itself.

Context: core.publish_criteria imports magicquant.quant.tiers at MODULE
SCOPE, so importing BUDGET_FILE_RE from it fails in a magicquant-less env.
The fix (see the loud comment above `_load_budget_file_re` in
core/hf_upload.py) falls back to `_BUDGET_FILE_RE_FALLBACK`, a local copy of
the same pattern, with a warning -- instead of the old silent
`BUDGET_FILE_RE = None`, which made size-target disclosure vanish with no
trace.

A re-review of that fix found it was itself unguarded: nothing pinned the
fallback pattern to the real one (so they could drift apart silently), and
nothing exercised the ImportError branch at all -- reverting the fix to the
old silent-None behavior passed the *entire* suite. This file closes that
gap with three things:

1. Pattern parity -- the fallback and the real regex can never drift apart
   unnoticed.
2. A forced-ImportError test of `_load_budget_file_re` (the loader extracted
   out of generate_model_card precisely so this could be tested directly,
   without fragile module-reload tricks) -- proving the fallback path is
   actually reachable and actually warns.
3. An end-to-end check that a budget file still gets its Size-Target card
   section when the import is forced to fail.
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core"))

import hf_upload
import publish_criteria
from hf_upload import (
    HFUploadConfig,
    _BUDGET_FILE_RE_FALLBACK,
    _load_budget_file_re,
    generate_model_card,
)

_96_GIB = 96 * 1024 ** 3


def _fake_gguf(path: Path, size_bytes: int = _96_GIB) -> Path:
    with open(path, "wb") as f:
        f.truncate(size_bytes)
    return path


def _cfg(**overrides):
    base = dict(
        repo_id="user/model-MagicQuant-GGUF",
        base_model="org/base",
        dataset_name="mydata",
        did_training=True,
        did_magicquant=True,
    )
    base.update(overrides)
    return HFUploadConfig(**base)


class _PoisonImport:
    """Forces `from publish_criteria import ...` / `from core.publish_criteria
    import ...` to raise ImportError, the way a magicquant-less environment
    would -- by setting the sys.modules entry to None, which the import
    system treats as "halt with ImportError" (verified: this is the standard
    mechanism, not a guess). Restores whatever was there before on exit,
    including the *absence* of a prior entry, so this can't leak state into
    other tests.
    """

    NAMES = ("publish_criteria", "core.publish_criteria")

    def __enter__(self):
        self._saved = {}
        for name in self.NAMES:
            self._saved[name] = sys.modules.get(name, "__ABSENT__")
            sys.modules[name] = None
        return self

    def __exit__(self, *exc):
        for name, val in self._saved.items():
            if val == "__ABSENT__":
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = val
        return False


# ── 1. Pattern parity: fallback can never silently drift from the real thing ─

def test_fallback_pattern_matches_real_publish_criteria_regex():
    assert _BUDGET_FILE_RE_FALLBACK.pattern == publish_criteria.BUDGET_FILE_RE.pattern


# ── 2. Forced ImportError: the loader itself, tested directly ──────────────

def test_loader_falls_back_and_still_matches_on_forced_import_error():
    warnings = []
    with _PoisonImport():
        regex = _load_budget_file_re(log=lambda msg, level="info": warnings.append((msg, level)))

    assert regex is not None
    # It's a working, equivalent regex -- not just "truthy".
    m = regex.search("Model-BUDGET-100GiB.gguf")
    assert m is not None
    assert m.group(1) == "100"

    assert len(warnings) == 1
    msg, level = warnings[0]
    assert level == "warn"
    assert "publish_criteria" in msg
    assert "fallback" in msg.lower()


def test_loader_does_not_warn_when_import_succeeds():
    warnings = []
    regex = _load_budget_file_re(log=lambda msg, level="info": warnings.append((msg, level)))
    assert regex is not None
    assert regex.search("Model-BUDGET-100GiB.gguf") is not None
    assert warnings == []


# ── 3. End-to-end: a budget file still gets disclosed when the import fails ─

def test_budget_file_still_gets_size_target_section_when_import_fails(tmp_path):
    mq_dir = tmp_path / "magicquant"
    mq_dir.mkdir()
    p = _fake_gguf(mq_dir / "Model-BUDGET-100GiB.gguf")

    warnings = []

    def _log(msg, level="info"):
        if level == "warn":
            warnings.append(msg)

    with _PoisonImport():
        card = generate_model_card(
            _cfg(), [(p, "Model-BUDGET-100GiB.gguf")], log=_log
        )

    assert "## Size-Target Build" in card
    section = card.split("## Size-Target Build", 1)[1].split("\n## ", 1)[0]
    assert "Model-BUDGET-100GiB.gguf" in section
    assert "100 GiB" in section
    assert "96.00 GiB" in section
    # The fallback path was actually exercised, not silently no-op'd.
    assert any("publish_criteria" in w for w in warnings), warnings
