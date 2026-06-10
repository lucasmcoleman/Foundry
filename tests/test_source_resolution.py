"""L-source-dup: artifact-source priority is centralized and deterministic.

Priority: reap_model > heretic_model > merged_model > model-bf16.gguf.
"""

import reap_common


def _mk_safetensors(d):
    d.mkdir(parents=True, exist_ok=True)
    (d / "model.safetensors").write_bytes(b"x")


def test_reap_wins(tmp_path):
    for name in ("reap_model", "heretic_model", "merged_model"):
        _mk_safetensors(tmp_path / name)
    assert reap_common.resolve_artifact_source(tmp_path) == tmp_path / "reap_model"


def test_heretic_over_merged(tmp_path):
    _mk_safetensors(tmp_path / "heretic_model")
    _mk_safetensors(tmp_path / "merged_model")
    assert reap_common.resolve_artifact_source(tmp_path) == tmp_path / "heretic_model"


def test_merged_when_only_merged(tmp_path):
    _mk_safetensors(tmp_path / "merged_model")
    assert reap_common.resolve_artifact_source(tmp_path) == tmp_path / "merged_model"


def test_gguf_fallback(tmp_path):
    (tmp_path / "model-bf16.gguf").write_bytes(b"GGUF")
    res = reap_common.resolve_artifact_source(tmp_path, require_safetensors=False)
    assert res == tmp_path / "model-bf16.gguf"


def test_none_when_empty(tmp_path):
    assert reap_common.resolve_artifact_source(tmp_path) is None


def test_dir_without_safetensors_is_skipped(tmp_path):
    (tmp_path / "merged_model").mkdir()  # empty, no *.safetensors
    assert reap_common.resolve_artifact_source(tmp_path) is None
