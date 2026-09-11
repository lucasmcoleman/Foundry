"""Completion markers must not bless partial or replaced model artifacts."""

import json
import os

import pytest

import markers


@pytest.mark.parametrize("payload", ["[]", "null", "17", '"bad"', "{", "\udcff"])
def test_malformed_marker_is_a_cache_miss(tmp_path, payload):
    (tmp_path / markers.MARKER_NAME).write_bytes(payload.encode("utf-8", errors="surrogateescape"))
    assert markers.is_stage_complete(tmp_path, tmp_path / "model.safetensors", "h") is False


def test_recorded_artifact_cannot_substitute_for_missing_requested_file(tmp_path):
    first = tmp_path / "first.safetensors"
    first.write_bytes(b"weights")
    markers.write_marker(tmp_path, "export", first, "h")
    assert not markers.is_stage_complete(tmp_path, tmp_path / "missing.safetensors", "h")


@pytest.mark.parametrize("change", ["remove", "truncate", "replace"])
def test_every_shard_is_verified(tmp_path, change):
    first = tmp_path / "model-00001.safetensors"
    second = tmp_path / "model-00002.safetensors"
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    markers.write_marker(tmp_path, "export", first, "h")
    if change == "remove":
        second.unlink()
    elif change == "truncate":
        second.write_bytes(b"x")
    else:
        mtime = second.stat().st_mtime_ns
        second.write_bytes(b"edited")  # Same byte count, changed content.
        os.utime(second, ns=(mtime + 1, mtime + 1))
    assert not markers.is_stage_complete(tmp_path, first, "h")


def test_index_with_missing_shard_cannot_be_completed(tmp_path):
    first = tmp_path / "first.safetensors"
    first.write_bytes(b"first")
    (tmp_path / "model.safetensors.index.json").write_text(json.dumps({
        "weight_map": {"a": "first.safetensors", "b": "missing.safetensors"},
    }))
    assert not markers.artifacts_present(tmp_path, first)


def test_directory_is_not_a_key_artifact(tmp_path):
    markers.write_marker(tmp_path, "export", tmp_path, "h")
    assert not markers.is_stage_complete(tmp_path, tmp_path, "h")


def test_local_input_fingerprint_detects_dataset_edits(tmp_path):
    dataset = tmp_path / "data.jsonl"
    dataset.write_text("first\n")
    before = markers.source_fingerprint(dataset)
    dataset.write_text("second\n")
    assert markers.source_fingerprint(dataset) != before
    assert markers.source_fingerprint("org/remote-model") is None


def test_invalidation_prevents_reusing_previous_success_after_failed_rerun(tmp_path):
    key = tmp_path / "model.safetensors"
    key.write_bytes(b"weights")
    markers.write_marker(tmp_path, "export", key, "h")
    markers.invalidate_marker(tmp_path)
    assert not markers.is_stage_complete(tmp_path, key, "h")
