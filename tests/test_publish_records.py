"""Measurement records must survive the things that ate their predecessors.

These numbers -- ROCmFPX perplexity and tokens/sec -- were previously written
only to a log, which the cleanup stage deletes, so a fork-only model card ended
up showing no throughput at all despite throughput being the whole reason that
quant family exists.

Pinned here because the reader and writer form an on-disk contract between two
processes (publish, then upload) that never run in the same interpreter, so
nothing else would catch them drifting apart.
"""
import json
import os
import stat

import pytest

from core.publish_records import (
    MEASUREMENTS_FILENAME,
    REFUSALS_FILENAME,
    find_measurements,
    read_measurements,
    read_refusals,
    write_measurements,
)

ENTRY = {
    "name": "Model-ROCMFPX-MQ-Q4.gguf", "tier": "Q4", "gib": 14.64,
    "ppl": 6.3949, "pp512": 248.52, "tg128": 12.45, "mq_peer_tg": 8.3,
}


def test_round_trips_every_field(tmp_path):
    assert write_measurements(tmp_path, [ENTRY]) is True
    got = read_measurements(tmp_path)
    assert got[ENTRY["name"]] == ENTRY


def test_records_rejected_candidates_too(tmp_path):
    """A rejected tier's numbers are what justify the rejection the card
    discloses, so the record is not filtered to survivors."""
    rejected = {**ENTRY, "name": "Model-ROCMFPX-MQ-Q6.gguf", "tier": "Q6",
                "tg128": 6.33, "mq_peer_tg": 6.8}
    write_measurements(tmp_path, [ENTRY, rejected])
    got = read_measurements(tmp_path)
    assert len(got) == 2
    assert got["Model-ROCMFPX-MQ-Q6.gguf"]["tg128"] == 6.33


def test_rerun_replaces_rather_than_duplicating(tmp_path):
    write_measurements(tmp_path, [ENTRY])
    write_measurements(tmp_path, [{**ENTRY, "ppl": 6.4001}])
    got = read_measurements(tmp_path)
    assert len(got) == 1, "a re-run must not append a second entry"
    assert got[ENTRY["name"]]["ppl"] == 6.4001, "and must carry the new value"


def test_other_entries_survive_a_partial_rerun(tmp_path):
    other = {**ENTRY, "name": "Model-Q8_0_ROCMFPX.gguf", "tier": "Q8"}
    write_measurements(tmp_path, [ENTRY, other])
    write_measurements(tmp_path, [{**ENTRY, "ppl": 1.0}])
    got = read_measurements(tmp_path)
    assert set(got) == {ENTRY["name"], other["name"]}


def test_creates_the_directory_if_missing(tmp_path):
    target = tmp_path / "rocmfpx"
    assert write_measurements(target, [ENTRY]) is True
    assert (target / MEASUREMENTS_FILENAME).is_file()


def test_missing_record_reads_as_empty_not_an_error(tmp_path):
    assert read_measurements(tmp_path / "nope") == {}


def test_corrupt_record_reads_as_empty(tmp_path):
    (tmp_path / MEASUREMENTS_FILENAME).write_text("{not json")
    assert read_measurements(tmp_path) == {}


def test_wrong_shape_reads_as_empty(tmp_path):
    (tmp_path / MEASUREMENTS_FILENAME).write_text('{"name": "x"}')  # dict, not list
    assert read_measurements(tmp_path) == {}


def test_corrupt_record_is_rewritten_rather_than_failing_a_publish(tmp_path):
    (tmp_path / MEASUREMENTS_FILENAME).write_text("garbage")
    assert write_measurements(tmp_path, [ENTRY]) is True
    assert read_measurements(tmp_path)[ENTRY["name"]] == ENTRY


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores permission bits")
def test_unwritable_directory_returns_false_and_does_not_raise(tmp_path):
    """A record that cannot persist must never fail the publish around it."""
    d = tmp_path / "locked"
    d.mkdir()
    # Deliberately do NOT pre-create the record: writing to an EXISTING file
    # needs no directory write permission, so seeding one made this test pass
    # for the wrong reason. Creating a new file is what the mode bits block.
    d.chmod(stat.S_IRUSR | stat.S_IXUSR)
    try:
        assert write_measurements(d, [ENTRY]) is False
    finally:
        d.chmod(stat.S_IRWXU)


def test_find_measurements_locates_by_family_dir(tmp_path):
    fx = tmp_path / "rocmfpx"
    write_measurements(fx, [ENTRY])
    files = [(fx / ENTRY["name"], ENTRY["name"])]
    assert find_measurements(files, family="rocmfpx")[ENTRY["name"]]["tg128"] == 12.45
    # A different family must not pick it up.
    assert find_measurements(files, family="magicquant") == {}


def test_written_json_is_a_sorted_list(tmp_path):
    """Stable ordering keeps re-runs from producing spurious file churn."""
    write_measurements(tmp_path, [
        {**ENTRY, "name": "b.gguf"}, {**ENTRY, "name": "a.gguf"},
    ])
    data = json.loads((tmp_path / MEASUREMENTS_FILENAME).read_text())
    assert [e["name"] for e in data] == ["a.gguf", "b.gguf"]


# ── read_refusals: forwards records INTACT, unlike output/_publish_tiers.py's
# find_refusals(), which projects a fixed {tier, family, reason} set and
# silently drops rule/requested_budget_gib/predicted_gib -- the exact bug
# that made a budget refusal's card closing unreachable on a real run. ──────

REFUSAL_BAND = {
    "tier": "Q5", "family": "rocmfpx", "rule": "band",
    "reason": "predicts 14.10 GiB, which is the Q6 band, not Q5.",
    "predicted_gib": 14.10, "baseline_gib": 11.50,
    "predicted_band": "Q6", "claimed_band": "Q5",
}
# The shape a future budget size-guard would actually write (Task 6): tier is
# a "BUDGET-<N>GiB" label, not None, and unclaimed band fields are "" not
# None.
REFUSAL_BUDGET = {
    "tier": "BUDGET-100GiB", "family": "magicquant", "rule": "budget",
    "reason": "predicted to exceed the requested 100 GiB budget by 18.4%.",
    "requested_budget_gib": 100.0, "predicted_gib": 118.4,
    "predicted_band": "", "claimed_band": "",
}


def test_read_refusals_forwards_every_field_intact(tmp_path):
    (tmp_path / REFUSALS_FILENAME).write_text(json.dumps([REFUSAL_BUDGET]))
    got = read_refusals(tmp_path, family="magicquant")
    assert len(got) == 1
    assert got[0] == REFUSAL_BUDGET
    # The three fields the CRITICAL bug dropped, pinned individually so a
    # partial-forwarding regression is caught even if the dict-equality
    # check above were ever loosened.
    assert got[0]["rule"] == "budget"
    assert got[0]["requested_budget_gib"] == 100.0
    assert got[0]["predicted_gib"] == 118.4


def test_read_refusals_filters_by_family(tmp_path):
    (tmp_path / REFUSALS_FILENAME).write_text(
        json.dumps([REFUSAL_BAND, REFUSAL_BUDGET]))
    assert [r["tier"] for r in read_refusals(tmp_path, family="rocmfpx")] == ["Q5"]
    assert [r["tier"] for r in read_refusals(tmp_path, family="magicquant")] == [
        "BUDGET-100GiB"
    ]


def test_read_refusals_dedupes_by_tier_keeping_first(tmp_path):
    older = {**REFUSAL_BAND, "reason": "older reason"}
    newer = {**REFUSAL_BAND, "reason": "newer reason"}
    (tmp_path / REFUSALS_FILENAME).write_text(json.dumps([older, newer]))
    got = read_refusals(tmp_path, family="rocmfpx")
    assert len(got) == 1
    assert got[0]["reason"] == "older reason"


def test_read_refusals_missing_record_reads_as_empty(tmp_path):
    assert read_refusals(tmp_path / "nope", family="rocmfpx") == []


def test_read_refusals_corrupt_record_reads_as_empty(tmp_path):
    (tmp_path / REFUSALS_FILENAME).write_text("{not json")
    assert read_refusals(tmp_path, family="rocmfpx") == []


def test_read_refusals_wrong_shape_reads_as_empty(tmp_path):
    (tmp_path / REFUSALS_FILENAME).write_text('{"tier": "Q5"}')  # dict, not list
    assert read_refusals(tmp_path, family="rocmfpx") == []


def test_read_refusals_skips_entries_with_no_tier(tmp_path):
    (tmp_path / REFUSALS_FILENAME).write_text(json.dumps(
        [{"family": "rocmfpx", "rule": "band", "reason": "no tier key"}]))
    assert read_refusals(tmp_path, family="rocmfpx") == []
