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
    find_measurements,
    read_measurements,
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
