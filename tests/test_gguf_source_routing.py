"""GGUF source detection, filtered download, and export pass-through.

Regression for the Qwopus3.6-27B-v2-MTP run (2026-07-04): pointing the
pipeline at a GGUF quant-collection repo snapshot-downloaded all 245 GB /
15 files (only the 54.7 GB BF16 was needed), then export crashed on the
missing safetensors index. GGUF sources must download only the best float
file and skip the merge entirely, landing where the MagicQuant/ROCmFPX
resolvers already look (<out>/model-bf16.gguf).
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "core"))

from fast_export import detect_gguf_source, pick_best_gguf, resolve_gguf_source


# ── pick_best_gguf ───────────────────────────────────────────────────────────

QWOPUS_REPO = [
    "README.md",
    "mmproj-F32.gguf",
    "Qwopus3.6-27B-v2-MTP-BF16.gguf",
    "Qwopus3.6-27B-v2-MTP-Q2_K.gguf",
    "Qwopus3.6-27B-v2-MTP-Q3_K_L.gguf",
    "Qwopus3.6-27B-v2-MTP-Q4_K_M.gguf",
    "Qwopus3.6-27B-v2-MTP-Q5_K_M.gguf",
    "Qwopus3.6-27B-v2-MTP-Q8_0.gguf",
]


def test_picks_bf16_from_quant_collection():
    assert pick_best_gguf(QWOPUS_REPO) == ["Qwopus3.6-27B-v2-MTP-BF16.gguf"]


def test_prefers_bf16_over_f16_over_f32():
    files = ["m-F32.gguf", "m-F16.gguf", "m-BF16.gguf"]
    assert pick_best_gguf(files) == ["m-BF16.gguf"]
    assert pick_best_gguf(["m-F32.gguf", "m-F16.gguf"]) == ["m-F16.gguf"]
    assert pick_best_gguf(["m-F32.gguf"]) == ["m-F32.gguf"]


def test_f16_pattern_does_not_match_bf16_only():
    # "f16" must not be satisfied BY the bf16 file when scanning for plain F16.
    assert pick_best_gguf(["m-BF16.gguf", "m-Q4_K_M.gguf"]) == ["m-BF16.gguf"]


def test_mmproj_never_selected():
    assert pick_best_gguf(["mmproj-F32.gguf", "m-BF16.gguf"]) == ["m-BF16.gguf"]
    # mmproj alone counts as "only quantized/unusable": no source candidates.
    with pytest.raises(ValueError, match="no BF16/F16/F32"):
        pick_best_gguf(["mmproj-F32.gguf", "m-Q4_K_M.gguf"])


def test_only_quantized_ggufs_raises_with_listing():
    with pytest.raises(ValueError, match="Q4_K_M"):
        pick_best_gguf(["m-Q4_K_M.gguf", "m-Q8_0.gguf"])


def test_no_ggufs_returns_empty():
    assert pick_best_gguf(["model.safetensors", "config.json"]) == []


def test_split_gguf_returns_all_parts_sorted():
    files = [
        "m-BF16-00002-of-00003.gguf",
        "m-BF16-00001-of-00003.gguf",
        "m-BF16-00003-of-00003.gguf",
        "m-Q4_K_M.gguf",
    ]
    assert pick_best_gguf(files) == [
        "m-BF16-00001-of-00003.gguf",
        "m-BF16-00002-of-00003.gguf",
        "m-BF16-00003-of-00003.gguf",
    ]


# ── detect_gguf_source ───────────────────────────────────────────────────────

def test_local_gguf_file_detected(tmp_path):
    f = tmp_path / "model-bf16.gguf"
    f.write_bytes(b"gguf")
    assert detect_gguf_source(str(f)) == [str(f)]


def test_local_safetensors_file_not_gguf_source(tmp_path):
    f = tmp_path / "model.safetensors"
    f.write_bytes(b"st")
    assert detect_gguf_source(str(f)) is None


def test_local_dir_with_safetensors_takes_normal_path(tmp_path):
    (tmp_path / "model.safetensors").write_bytes(b"st")
    (tmp_path / "also-BF16.gguf").write_bytes(b"gguf")
    assert detect_gguf_source(str(tmp_path)) is None


def test_local_gguf_only_dir_detected(tmp_path):
    (tmp_path / "m-BF16.gguf").write_bytes(b"gguf")
    (tmp_path / "m-Q4_K_M.gguf").write_bytes(b"gguf")
    assert detect_gguf_source(str(tmp_path)) == [str(tmp_path / "m-BF16.gguf")]


def test_hf_repo_listing_is_filtered(monkeypatch):
    import fast_export as fe

    fake = type(sys)("huggingface_hub")
    fake.list_repo_files = lambda repo_id: QWOPUS_REPO
    monkeypatch.setitem(sys.modules, "huggingface_hub", fake)
    assert fe.detect_gguf_source("Jackrong/Qwopus-GGUF") == [
        "Qwopus3.6-27B-v2-MTP-BF16.gguf"
    ]


def test_hf_repo_with_safetensors_takes_normal_path(monkeypatch):
    fake = type(sys)("huggingface_hub")
    fake.list_repo_files = lambda repo_id: ["model.safetensors", "x-BF16.gguf"]
    monkeypatch.setitem(sys.modules, "huggingface_hub", fake)
    import fast_export as fe

    assert fe.detect_gguf_source("org/model") is None


# ── resolve_gguf_source ──────────────────────────────────────────────────────

def test_resolve_local_file_no_download(tmp_path):
    f = tmp_path / "m-BF16.gguf"
    f.write_bytes(b"gguf")
    assert resolve_gguf_source(str(f)) == str(f)


def test_resolve_downloads_only_the_picked_file(monkeypatch, tmp_path):
    import fast_export as fe

    downloaded = []

    def fake_download(repo_id, filename):
        downloaded.append((repo_id, filename))
        p = tmp_path / filename
        p.write_bytes(b"gguf")
        return str(p)

    fake = type(sys)("huggingface_hub")
    fake.list_repo_files = lambda repo_id: QWOPUS_REPO
    fake.hf_hub_download = fake_download
    monkeypatch.setitem(sys.modules, "huggingface_hub", fake)

    result = fe.resolve_gguf_source("Jackrong/Qwopus-GGUF")
    assert downloaded == [("Jackrong/Qwopus-GGUF", "Qwopus3.6-27B-v2-MTP-BF16.gguf")]
    assert result.endswith("Qwopus3.6-27B-v2-MTP-BF16.gguf")


def test_resolve_split_gguf_rejected(monkeypatch):
    fake = type(sys)("huggingface_hub")
    fake.list_repo_files = lambda repo_id: [
        "m-BF16-00001-of-00002.gguf", "m-BF16-00002-of-00002.gguf",
    ]
    monkeypatch.setitem(sys.modules, "huggingface_hub", fake)
    import fast_export as fe

    with pytest.raises(RuntimeError, match="split"):
        fe.resolve_gguf_source("org/split-model")


# ── streaming_merge pass-through ─────────────────────────────────────────────

def test_streaming_merge_gguf_passthrough_symlinks_and_skips_merge(tmp_path):
    from fast_export import streaming_merge

    src = tmp_path / "cache" / "m-BF16.gguf"
    src.parent.mkdir()
    src.write_bytes(b"gguf-bytes")
    merged_dir = tmp_path / "out" / "merged_model"

    streaming_merge(model_id=str(src), lora_dir=None, merged_dir=str(merged_dir))

    link = tmp_path / "out" / "model-bf16.gguf"
    assert link.is_symlink()
    assert link.resolve() == src.resolve()
    assert not merged_dir.exists(), "no merged_model dir for a pass-through"


def test_streaming_merge_gguf_with_lora_raises(tmp_path):
    from fast_export import streaming_merge

    src = tmp_path / "m-BF16.gguf"
    src.write_bytes(b"gguf")
    with pytest.raises(RuntimeError, match="LoRA"):
        streaming_merge(model_id=str(src), lora_dir=str(tmp_path),
                        merged_dir=str(tmp_path / "out" / "merged_model"))


def test_passthrough_replaces_stale_link(tmp_path):
    from fast_export import streaming_merge

    old = tmp_path / "old.gguf"; old.write_bytes(b"old")
    new = tmp_path / "new-BF16.gguf"; new.write_bytes(b"new")
    out = tmp_path / "out"; out.mkdir()
    link = out / "model-bf16.gguf"
    link.symlink_to(old)

    streaming_merge(model_id=str(new), lora_dir=None,
                    merged_dir=str(out / "merged_model"))
    assert link.resolve() == new.resolve()


# ── training guard ───────────────────────────────────────────────────────────

def test_train_entry_rejects_gguf_source(tmp_path, monkeypatch):
    import json
    import _train_entry as te

    f = tmp_path / "m-BF16.gguf"
    f.write_bytes(b"gguf")
    cfg = {
        "pipeline_root": str(Path(__file__).resolve().parent.parent),
        "model_name": str(f), "output_dir": str(tmp_path / "out"),
        "max_seq_length": 2048,
    }
    cfg_path = tmp_path / "cfg.json"
    cfg_path.write_text(json.dumps(cfg))
    with pytest.raises(RuntimeError, match="GGUF source"):
        te.run(str(cfg_path))
