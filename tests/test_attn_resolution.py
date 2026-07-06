"""Auto-selection of attention backend + packing safety gate.

Regression: packing + a non-flash attention (the only kind available on
gfx1151/ROCm) lets packed samples attend across boundaries — cross-sample
contamination, empirically confirmed. resolve_packing must force packing off
whenever the chosen attention isn't packing-safe.
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "core"))
from fast_train_zeroclaw import (
    resolve_attn_implementation, resolve_packing, _PACKING_SAFE_ATTN,
)


def test_resolver_falls_back_to_sdpa_when_no_flash(monkeypatch):
    import transformers.utils as u
    monkeypatch.setattr(u, "is_flash_attn_2_available", lambda: False)
    assert resolve_attn_implementation() == "sdpa"


def test_resolver_picks_flash_when_available(monkeypatch):
    import transformers.utils as u
    monkeypatch.setattr(u, "is_flash_attn_2_available", lambda: True)
    assert resolve_attn_implementation() == "flash_attention_2"


def test_resolver_prefer_flash_false_stays_sdpa(monkeypatch):
    import transformers.utils as u
    monkeypatch.setattr(u, "is_flash_attn_2_available", lambda: True)
    assert resolve_attn_implementation(prefer_flash=False) == "sdpa"


def test_packing_forced_off_without_flash():
    eff, note = resolve_packing(True, "sdpa")
    assert eff is False and note and "contamination" in note


def test_packing_preserved_with_flash():
    for impl in _PACKING_SAFE_ATTN:
        eff, note = resolve_packing(True, impl)
        assert eff is True and note is None


def test_packing_off_stays_off_and_quiet():
    eff, note = resolve_packing(False, "sdpa")
    assert eff is False and note is None
