"""A build-time ROCmFPX band-guard refusal must leave a structured record.

Before this, the ONLY trace of a "Refusing mq-qN: ..." band-guard decision
(core/_rocmfpx_entry.py's _quantize_mq_hybrid, around the tier-band check) was
a stdout log line. The publish stage recovered it by regex-scraping run logs,
which breaks the moment logs are rotated, cleaned up (cleanup DOES delete
logs), or reworded -- and produced disclosure gaps on public model cards
(REFUSED tiers disclosed nowhere at all).

_record_refusal appends a machine-readable twin to
``<out>/rocmfpx/_refusals.json`` alongside the existing log line, which is
left byte-for-byte unchanged (other things match on it).
"""

import json
import os
import stat
from pathlib import Path

import pytest

import _rocmfpx_entry as entry


# Card voice, not log voice: the consumer renders this as "- **Q5** -- <reason>",
# so the log line's "Refusing mq-q5: " prefix is NOT part of the stored reason.
REAL_REASON_Q5 = (
    "rendering MagicQuant's Q5 config into ROCmFPX types "
    "predicts 20.68 GiB against a 50.89 GiB BF16 baseline (ratio 0.4063), "
    "which is the Q6 band, not Q5."
)

REAL_REASON_Q4 = (
    "rendering MagicQuant's Q4 config into ROCmFPX types "
    "predicts 17.09 GiB against a 50.89 GiB BF16 baseline (ratio 0.3358), "
    "which is the Q5 band, not Q4."
)


def _record(rocmfpx_out_dir, tier="Q5", reason=REAL_REASON_Q5,
            predicted_gib=20.68, baseline_gib=50.89,
            predicted_band="Q6", claimed_band="Q5", family="rocmfpx"):
    entry._record_refusal(
        rocmfpx_out_dir,
        tier=tier, family=family, reason=reason,
        predicted_gib=predicted_gib, baseline_gib=baseline_gib,
        predicted_band=predicted_band, claimed_band=claimed_band,
    )


def _read(rocmfpx_out_dir):
    return json.loads((rocmfpx_out_dir / "_refusals.json").read_text())


# ── write + read back ────────────────────────────────────────────────────────

def test_write_then_read_back_round_trips_all_fields(tmp_path):
    _record(tmp_path)
    records = _read(tmp_path)
    assert records == [{
        "tier": "Q5",
        "family": "rocmfpx",
        "reason": REAL_REASON_Q5,
        "predicted_gib": 20.68,
        "baseline_gib": 50.89,
        "predicted_band": "Q6",
        "claimed_band": "Q5",
    }]


def test_reason_string_carries_the_numbers_verbatim(tmp_path):
    """The reason must be complete enough to render on a public model card
    verbatim -- predicted GiB, baseline, ratio, and both bands."""
    _record(tmp_path)
    reason = _read(tmp_path)[0]["reason"]
    assert "20.68 GiB" in reason
    assert "50.89 GiB" in reason
    assert "0.4063" in reason
    assert "Q6 band" in reason
    assert "not Q5" in reason


def test_second_real_example_round_trips(tmp_path):
    """Pin the second REAL EXAMPLE from the incident report too."""
    _record(
        tmp_path, tier="Q4", reason=REAL_REASON_Q4,
        predicted_gib=17.09, baseline_gib=50.89,
        predicted_band="Q5", claimed_band="Q4",
    )
    rec = _read(tmp_path)[0]
    assert rec["predicted_band"] == "Q5"
    assert rec["claimed_band"] == "Q4"
    assert "17.09 GiB" in rec["reason"]
    assert "0.3358" in rec["reason"]


def test_creates_the_output_directory_if_missing(tmp_path):
    rocmfpx_out_dir = tmp_path / "does" / "not" / "exist" / "rocmfpx"
    assert not rocmfpx_out_dir.exists()
    _record(rocmfpx_out_dir)
    assert (rocmfpx_out_dir / "_refusals.json").exists()


def test_two_different_tiers_both_recorded(tmp_path):
    _record(tmp_path, tier="Q5", reason=REAL_REASON_Q5)
    _record(
        tmp_path, tier="Q4", reason=REAL_REASON_Q4,
        predicted_gib=17.09, baseline_gib=50.89,
        predicted_band="Q5", claimed_band="Q4",
    )
    records = _read(tmp_path)
    assert {r["tier"] for r in records} == {"Q4", "Q5"}
    assert len(records) == 2


# ── idempotency ──────────────────────────────────────────────────────────────

def test_rerunning_the_same_tier_does_not_duplicate(tmp_path):
    """Re-running a build must not duplicate entries for the same tier."""
    _record(tmp_path, tier="Q5", reason=REAL_REASON_Q5)
    _record(tmp_path, tier="Q5", reason=REAL_REASON_Q5)
    _record(tmp_path, tier="Q5", reason=REAL_REASON_Q5)
    records = _read(tmp_path)
    assert len(records) == 1
    assert records[0]["tier"] == "Q5"


def test_rerun_with_updated_numbers_replaces_rather_than_appends(tmp_path):
    """A re-run after a config/model change updates the recorded numbers in
    place instead of accumulating stale duplicates for the same tier."""
    _record(tmp_path, tier="Q5", predicted_gib=20.68, baseline_gib=50.89)
    _record(tmp_path, tier="Q5", predicted_gib=21.00, baseline_gib=51.00)
    records = _read(tmp_path)
    assert len(records) == 1
    assert records[0]["predicted_gib"] == 21.00
    assert records[0]["baseline_gib"] == 51.00


def test_other_tiers_untouched_by_a_rerun(tmp_path):
    _record(tmp_path, tier="Q4", reason=REAL_REASON_Q4, predicted_band="Q5", claimed_band="Q4")
    _record(tmp_path, tier="Q5", reason=REAL_REASON_Q5, predicted_band="Q6", claimed_band="Q5")
    _record(tmp_path, tier="Q5", reason=REAL_REASON_Q5, predicted_band="Q6", claimed_band="Q5")
    records = _read(tmp_path)
    assert len(records) == 2
    q4 = next(r for r in records if r["tier"] == "Q4")
    assert q4["reason"] == REAL_REASON_Q4


def test_dedup_key_is_tier_and_family_not_tier_alone(tmp_path):
    """Different families refusing the same tier band name are distinct
    entries, not a collision (the file is keyed on (tier, family))."""
    _record(tmp_path, tier="Q5", family="rocmfpx")
    _record(tmp_path, tier="Q5", family="some-other-family")
    records = _read(tmp_path)
    assert len(records) == 2
    assert {r["family"] for r in records} == {"rocmfpx", "some-other-family"}


# ── never raise ──────────────────────────────────────────────────────────────

def test_unwritable_directory_does_not_raise(tmp_path, capsys):
    """A refusal record failing to write must not fail the build."""
    locked_parent = tmp_path / "locked"
    locked_parent.mkdir()
    rocmfpx_out_dir = locked_parent / "rocmfpx"
    original_mode = locked_parent.stat().st_mode
    locked_parent.chmod(stat.S_IREAD | stat.S_IEXEC)
    try:
        if os.access(str(rocmfpx_out_dir.parent), os.W_OK):
            pytest.skip("test runs as a user that bypasses directory permissions (e.g. root)")
        _record(rocmfpx_out_dir)  # must not raise
    finally:
        locked_parent.chmod(original_mode)
    assert "Warning" in capsys.readouterr().out


def test_unwritable_file_does_not_raise(tmp_path, capsys):
    """Directory is writable but the record file itself is not."""
    rocmfpx_out_dir = tmp_path / "rocmfpx"
    rocmfpx_out_dir.mkdir()
    record_path = rocmfpx_out_dir / "_refusals.json"
    record_path.write_text("[]")
    record_path.chmod(stat.S_IREAD)
    try:
        if os.access(str(record_path), os.W_OK):
            pytest.skip("test runs as a user that bypasses file permissions (e.g. root)")
        _record(rocmfpx_out_dir)  # must not raise
    finally:
        record_path.chmod(stat.S_IREAD | stat.S_IWRITE)
    assert "Warning" in capsys.readouterr().out


def test_corrupt_existing_file_recovers_loudly_and_does_not_raise(tmp_path, capsys):
    """A truncated _refusals.json must not crash the build -- and must not
    discard the refusals it held in silence.

    A torn write can hold a real, previously-recorded refusal. Replacing it
    with no output is the "disclosed nowhere" failure (bug #4) recurring
    inside the mechanism built to fix it, so the recovery has to say what it
    dropped.
    """
    rocmfpx_out_dir = tmp_path / "rocmfpx"
    rocmfpx_out_dir.mkdir()
    (rocmfpx_out_dir / "_refusals.json").write_text(
        '[{"tier": "Q4", "reason": "HISTORY A"')      # truncated mid-write
    _record(rocmfpx_out_dir)                           # must not raise

    out = capsys.readouterr().out
    assert "Warning" in out
    assert "unreadable" in out
    assert str(rocmfpx_out_dir / "_refusals.json") in out
    # The new refusal is still recorded -- an unreadable file must not block
    # this run's disclosure either.
    assert [r["tier"] for r in _read(rocmfpx_out_dir)] == ["Q5"]


def test_non_utf8_existing_file_recovers_instead_of_blocking_forever(tmp_path, capsys):
    """A non-UTF-8 _refusals.json used to escape the inner handler as a
    UnicodeDecodeError, hit the outer catch-all, and permanently block every
    later refusal from being recorded -- a warning each time and never a
    record. It must recover the same way a truncated file does."""
    rocmfpx_out_dir = tmp_path / "rocmfpx"
    rocmfpx_out_dir.mkdir()
    (rocmfpx_out_dir / "_refusals.json").write_bytes(b"\xff\xfe garbage")
    _record(rocmfpx_out_dir)

    assert "Warning" in capsys.readouterr().out
    assert [r["tier"] for r in _read(rocmfpx_out_dir)] == ["Q5"]


def test_non_list_existing_file_is_treated_as_empty(tmp_path):
    """A _refusals.json that somehow holds a non-list (corrupt/foreign write)
    is treated as empty rather than crashing on iteration."""
    rocmfpx_out_dir = tmp_path / "rocmfpx"
    rocmfpx_out_dir.mkdir()
    (rocmfpx_out_dir / "_refusals.json").write_text('{"not": "a list"}')
    _record(rocmfpx_out_dir, tier="Q5")
    records = _read(rocmfpx_out_dir)
    assert records == [{
        "tier": "Q5", "family": "rocmfpx", "reason": REAL_REASON_Q5,
        "predicted_gib": 20.68, "baseline_gib": 50.89,
        "predicted_band": "Q6", "claimed_band": "Q5",
    }]


# ── integration: the real refusal path through _quantize_mq_hybrid ─────────

def test_quantize_mq_hybrid_writes_the_record_on_band_refusal(tmp_path, monkeypatch, capsys):
    """End-to-end: the actual band-guard branch in _quantize_mq_hybrid (not a
    hand-built call to _record_refusal) produces the record."""
    out_dir = tmp_path / "output"
    rocmfpx_out_dir = out_dir / "rocmfpx"
    magicquant_dir = out_dir / "magicquant"
    magicquant_dir.mkdir(parents=True)
    (magicquant_dir / "search_results.json").write_text(json.dumps({
        "tier_scheme_version": 2,
        "tiered": {"Q5": {"config": {"X": "Q5_K"}}},
    }))

    monkeypatch.setattr(
        entry, "predict_rendered_tier",
        lambda config, bf16_gguf: (20.68, 50.89, "Q6"),
    )

    class _StubTGC:
        GROUP_PATTERNS = {}

    import sys
    import types
    fake_pkg = types.ModuleType("magicquant.gguf.tensor_groups")
    fake_pkg.TensorGroupClassifier = _StubTGC
    monkeypatch.setitem(sys.modules, "magicquant.gguf.tensor_groups", fake_pkg)

    result = entry._quantize_mq_hybrid(
        spec="mq-q5", tier="Q5", out_dir=out_dir, rocmfpx_out_dir=rocmfpx_out_dir,
        model_name="stub-model", quantize_bin=Path("/nonexistent/llama-quantize"),
        bf16_gguf="stub.gguf", imatrix="",
    )

    assert result is None
    out = capsys.readouterr().out
    assert "Refusing mq-q5" in out

    records = _read(rocmfpx_out_dir)
    assert len(records) == 1
    rec = records[0]
    assert rec["tier"] == "Q5"
    assert rec["family"] == "rocmfpx"
    assert rec["predicted_band"] == "Q6"
    assert rec["claimed_band"] == "Q5"
    assert rec["predicted_gib"] == pytest.approx(20.68)
    assert rec["baseline_gib"] == pytest.approx(50.89)
    assert "20.68 GiB" in rec["reason"]
    assert "50.89 GiB" in rec["reason"]
    assert "Q6 band" in rec["reason"]
    # Card voice, not log voice -- the log line keeps its prefix, the record
    # must not: a card renders this as "- **Q5** -- <reason>".
    assert "Refusing" not in rec["reason"]
    assert "mq-q5" not in rec["reason"]
    assert rec["reason"].startswith("rendering")


# ── a refusal must not outlive the condition that caused it ────────────────

def test_clear_refusal_removes_the_entry(tmp_path):
    _record(tmp_path, tier="Q4", reason=REAL_REASON_Q4)
    _record(tmp_path, tier="Q5")
    entry._clear_refusal(tmp_path, tier="Q5", family="rocmfpx")
    assert [r["tier"] for r in _read(tmp_path)] == ["Q4"]


def test_clearing_the_last_refusal_removes_the_file(tmp_path):
    """An empty list still reads as "there is a refusal record here"; a
    consumer should see no file at all rather than an empty one."""
    _record(tmp_path, tier="Q5")
    entry._clear_refusal(tmp_path, tier="Q5", family="rocmfpx")
    assert not (tmp_path / "_refusals.json").exists()


def test_clearing_a_tier_that_was_never_refused_is_a_no_op(tmp_path):
    entry._clear_refusal(tmp_path, tier="Q5", family="rocmfpx")
    assert not (tmp_path / "_refusals.json").exists()


def test_clear_is_keyed_on_tier_and_family(tmp_path):
    _record(tmp_path, tier="Q5", family="rocmfpx")
    _record(tmp_path, tier="Q5", family="magicquant")
    entry._clear_refusal(tmp_path, tier="Q5", family="rocmfpx")
    assert [r["family"] for r in _read(tmp_path)] == ["magicquant"]


def test_a_successful_build_clears_that_tier_stale_refusal(tmp_path, monkeypatch):
    """Refuse-then-succeed across two runs. Without this, _refusals.json keeps
    asserting "Q5 was not built" while the Q5 GGUF sits right next to it --
    bug #2 (a card calling a tier unpublished with the file present)
    reproduced inside the mechanism meant to prevent it.
    """
    out_dir = tmp_path / "output"
    rocmfpx_out_dir = out_dir / "rocmfpx"
    magicquant_dir = out_dir / "magicquant"
    magicquant_dir.mkdir(parents=True)
    (magicquant_dir / "search_results.json").write_text(json.dumps({
        "tier_scheme_version": 2,
        "tiered": {"Q5": {"config": {"X": "Q5_K"}}},
    }))

    import sys
    import types
    fake_pkg = types.ModuleType("magicquant.gguf.tensor_groups")

    class _StubTGC:
        GROUP_PATTERNS = {}

    fake_pkg.TensorGroupClassifier = _StubTGC
    monkeypatch.setitem(sys.modules, "magicquant.gguf.tensor_groups", fake_pkg)
    monkeypatch.setattr(entry, "build_tensor_type_lines", lambda c, g: ["x=y"])
    monkeypatch.setattr(entry, "pick_base_type", lambda c: "ROCMFP4")
    monkeypatch.setattr(entry, "translate_scheme", lambda s: "ROCMFP4")
    monkeypatch.setattr(entry, "validate_types_supported", lambda t, b: None)

    def _call():
        return entry._quantize_mq_hybrid(
            spec="mq-q5", tier="Q5", out_dir=out_dir,
            rocmfpx_out_dir=rocmfpx_out_dir, model_name="m",
            quantize_bin=Path("/nonexistent/llama-quantize"),
            bf16_gguf="stub.gguf", imatrix="",
        )

    # Run 1: the band guard refuses.
    monkeypatch.setattr(entry, "predict_rendered_tier",
                        lambda config, bf16_gguf: (20.68, 50.89, "Q6"))
    assert _call() is None
    assert [r["tier"] for r in _read(rocmfpx_out_dir)] == ["Q5"]

    # Run 2: a re-search made Q5 buildable, and the quantize succeeds.
    monkeypatch.setattr(entry, "predict_rendered_tier",
                        lambda config, bf16_gguf: (18.0, 50.89, "Q5"))
    built = rocmfpx_out_dir / "m-ROCMFPX-MQ-Q5.gguf"

    def _fake_run(cmd, *a, **kw):
        built.write_bytes(b"gguf")
        return types.SimpleNamespace(returncode=0)

    monkeypatch.setattr("subprocess.run", _fake_run)
    assert _call() == built
    assert built.exists()
    assert not (rocmfpx_out_dir / "_refusals.json").exists(), (
        "STALE: _refusals.json still claims Q5 was refused while the Q5 GGUF "
        "sits next to it -- original bug #2 shape")


def test_recorded_reason_renders_on_a_card_the_way_the_consumer_expects(tmp_path):
    """The only consumer contract that exists: hf_upload renders refused
    tiers as "- **<tier>** -- <reason>". Pin the record against that, so a
    reason shaped for the build log cannot reach a public page."""
    from core.hf_upload import HFUploadConfig, generate_model_card

    _record(tmp_path, tier="Q5")
    rec = _read(tmp_path)[0]
    cfg = HFUploadConfig(
        repo_id="u/m-ROCmFPX-GGUF",
        refused_tiers=[{"tier": rec["tier"], "family": rec["family"],
                        "reason": rec["reason"]}],
    )
    card = generate_model_card(cfg, [], rocmfpx=True)
    assert "- **Q5** -- rendering MagicQuant's Q5 config" in card
    assert "Refusing" not in card
