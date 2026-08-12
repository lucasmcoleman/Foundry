"""Field report Issue B, defect 2: export_hash keyed on a field that can't
affect output.

do_export's completion-marker hash used to include cfg.training.model_name
UNCONDITIONALLY, even when training is disabled. With training off, export
output depends only on export.source_model -- so stale browser-form drift
in the (dead-weight, training-off) training section invalidated an
otherwise-good marker and forced a spurious re-export (observed live: Muse
re-exported at 22:03 for exactly this). Fix: fold training.model_name into
the hash ONLY when training_enabled -- when training is on it legitimately
determines the base model being exported.

CAUTION (blast radius, corrected after reviewer catch -- do not restate the
broader "every run dir" claim): a training-ENABLED run dir's hash is
UNCHANGED by this fix (both the old and new formula hash
cfg.training.model_name identically when training is on), so it does NOT
re-export. Only a training-DISABLED run dir's hash actually moves (old
always hashed the live model_name value; new hashes a constant None), so
ONLY those re-export once after the fix lands. Expected and cheap for that
subset; noted here (and in the commit) rather than engineered around. See
test_toggling_training_enabled_alone_moves_hash and the training_enabled
variants of test_model_name_change_* below, which pin this asymmetry
directly.
"""
from pathlib import Path

import markers

ROOT = Path(__file__).resolve().parent.parent


def _export_hash(model_name: str, source_model: str, training_enabled: bool) -> str:
    """Mirrors do_export's export_hash dict construction exactly -- see
    test_do_export_hash_block_matches_this_helper below, which pins the real
    source to this same shape. A real markers.config_hash call, not a mock,
    so this exercises the actual hash function the marker gate uses."""
    return markers.config_hash({
        "model_name": model_name if training_enabled else None,
        "source_model": source_model,
        "training_enabled": training_enabled,
    })


def test_model_name_change_does_not_move_hash_when_training_disabled():
    h1 = _export_hash("modelA", "src/path", training_enabled=False)
    h2 = _export_hash("modelB", "src/path", training_enabled=False)
    assert h1 == h2


def test_model_name_change_moves_hash_when_training_enabled():
    h1 = _export_hash("modelA", "src/path", training_enabled=True)
    h2 = _export_hash("modelB", "src/path", training_enabled=True)
    assert h1 != h2


def test_source_model_change_always_moves_hash_regardless_of_training():
    # source_model is the one field that DOES always determine export output
    # -- the fix must not have accidentally dropped it from the hash too.
    for training_enabled in (True, False):
        h1 = _export_hash("model", "src/a", training_enabled)
        h2 = _export_hash("model", "src/b", training_enabled)
        assert h1 != h2, f"training_enabled={training_enabled}"


def test_toggling_training_enabled_alone_moves_hash():
    # training_enabled is itself part of the hashed dict -- flipping it (with
    # everything else held fixed) must still be visible, independent of
    # whatever model_name happens to be.
    h1 = _export_hash("model", "src/a", training_enabled=True)
    h2 = _export_hash("model", "src/a", training_enabled=False)
    assert h1 != h2


def test_do_export_hash_block_matches_this_helper():
    """Source-inspection guard (mirrors the existing repo convention used
    for do_magicquant's hash block in test_ui_magicquant_budget.py --
    do_export is async and talks to a live WebSocket/state singleton, so
    this pins the actual code shape rather than invoking the function): the
    real export_hash construction must gate model_name on training_enabled
    exactly the way _export_hash() above mirrors it."""
    src = (ROOT / "ui" / "app.py").read_text(encoding="utf-8")
    do_export_start = src.index("async def do_export")
    do_export_body = src[do_export_start:src.index("\nasync def ", do_export_start + 1)]
    hash_block = do_export_body[:do_export_body.index("done, export_key = await _check_marker(")]
    assert '"model_name": cfg.training.model_name if training_enabled else None' in hash_block
    assert '"source_model": cfg.export.source_model if cfg.export else ""' in hash_block
    assert '"training_enabled": training_enabled' in hash_block
