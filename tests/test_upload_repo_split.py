"""Sibling-repo upload: MagicQuant and ROCmFPX GGUFs go to separate repos.

Covers discover_upload_files gguf_family routing, plan_gguf_repos sibling
naming, and the ROCmFPX-flavored model card (fork warning, tags, cross-links).
"""
from pathlib import Path

from hf_upload import (
    HFUploadConfig,
    discover_upload_files,
    generate_model_card,
    plan_gguf_repos,
)


def _touch(path: Path, size: int = 8) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\0" * size)
    return path


def _out_with(tmp_path, mq=False, fpx=False, bf16=False, mmproj=False) -> str:
    if mq:
        _touch(tmp_path / "magicquant" / "Model-Q4_K_M.gguf")
        _touch(tmp_path / "magicquant" / "Model-Q6_K.gguf")
    if fpx:
        _touch(tmp_path / "rocmfpx" / "Model-Q4_0_ROCMFP4.gguf")
        _touch(tmp_path / "rocmfpx" / "Model-Q3_0_ROCMFPX_AGENT.gguf")
    if bf16:
        _touch(tmp_path / "model-bf16.gguf")
    if mmproj:
        _touch(tmp_path / "mmproj" / "mmproj-Model-f16.gguf")
    return str(tmp_path)


# ── discover_upload_files: gguf_family routing ───────────────────────────

def test_auto_prefers_magicquant(tmp_path):
    out = _out_with(tmp_path, mq=True, fpx=True)
    names = [r for _, r in discover_upload_files(out)]
    assert "Model-Q4_K_M.gguf" in names
    assert not any("ROCMFP" in n for n in names)


def test_auto_falls_back_to_rocmfpx(tmp_path):
    out = _out_with(tmp_path, fpx=True, bf16=True)
    names = [r for _, r in discover_upload_files(out)]
    assert "Model-Q4_0_ROCMFP4.gguf" in names
    assert "model-bf16.gguf" not in names


def test_auto_falls_back_to_bf16_last(tmp_path):
    out = _out_with(tmp_path, bf16=True)
    names = [r for _, r in discover_upload_files(out)]
    assert names == ["model-bf16.gguf"]


def test_family_selects_only_that_family(tmp_path):
    out = _out_with(tmp_path, mq=True, fpx=True)
    mq_names = [r for _, r in discover_upload_files(out, gguf_family="magicquant")]
    fpx_names = [r for _, r in discover_upload_files(out, gguf_family="rocmfpx")]
    assert all("ROCMFP" not in n for n in mq_names)
    assert all("ROCMFP" in n for n in fpx_names)


def test_mmproj_ships_with_every_family(tmp_path):
    out = _out_with(tmp_path, mq=True, fpx=True, mmproj=True)
    for family in ("magicquant", "rocmfpx"):
        names = [r for _, r in discover_upload_files(out, gguf_family=family)]
        assert "mmproj-Model-f16.gguf" in names


# ── plan_gguf_repos: sibling repo naming ─────────────────────────────────

def test_both_families_split_into_siblings(tmp_path):
    out = _out_with(tmp_path, mq=True, fpx=True)
    plan = plan_gguf_repos(out, "user/Model-GGUF")
    assert plan == [
        ("user/Model-MagicQuant-GGUF", "magicquant"),
        ("user/Model-ROCmFPX-GGUF", "rocmfpx"),
    ]


def test_split_reuses_base_from_suffixed_repo_id(tmp_path):
    out = _out_with(tmp_path, mq=True, fpx=True)
    for configured in ("user/Model-ROCmFPX-GGUF", "user/Model-MagicQuant-GGUF"):
        plan = plan_gguf_repos(out, configured)
        assert [r for r, _ in plan] == [
            "user/Model-MagicQuant-GGUF",
            "user/Model-ROCmFPX-GGUF",
        ]


def test_single_family_keeps_configured_repo(tmp_path):
    out = _out_with(tmp_path, fpx=True)
    assert plan_gguf_repos(out, "user/Model-ROCmFPX-GGUF") == [
        ("user/Model-ROCmFPX-GGUF", "rocmfpx"),
    ]


def test_no_quants_stays_auto(tmp_path):
    out = _out_with(tmp_path, bf16=True)
    assert plan_gguf_repos(out, "user/Model-GGUF") == [("user/Model-GGUF", "auto")]


# ── generate_model_card: ROCmFPX flavor + sibling links ──────────────────

def _cfg(repo_id="user/Model-ROCmFPX-GGUF"):
    return HFUploadConfig(
        repo_id=repo_id,
        base_model="org/Base-Model",
        did_training=False,
        did_magicquant=False,
    )


def _fpx_files(tmp_path):
    out = Path(_out_with(tmp_path, fpx=True))
    return [(p, p.name) for p in sorted((out / "rocmfpx").glob("*.gguf"))]


def test_rocmfpx_card_has_fork_warning_and_tags(tmp_path):
    card = generate_model_card(_cfg(), _fpx_files(tmp_path), rocmfpx=True)
    assert "do NOT load on standard llama.cpp" in card
    assert "ciru-ai/ROCmFPX" in card
    assert "- rocmfpx" in card and "- gfx1151" in card
    assert "quantized_by: ROCmFPX" in card
    assert "### LM Studio" not in card  # no LM Studio usage section: files never load there


def test_rocmfpx_card_links_sibling(tmp_path):
    card = generate_model_card(
        _cfg(), _fpx_files(tmp_path),
        rocmfpx=True, sibling_repo_id="user/Model-MagicQuant-GGUF",
    )
    assert "huggingface.co/user/Model-MagicQuant-GGUF" in card


def test_magicquant_card_links_rocmfpx_sibling(tmp_path):
    out = Path(_out_with(tmp_path, mq=True))
    files = [(p, p.name) for p in sorted((out / "magicquant").glob("*.gguf"))]
    cfg = _cfg(repo_id="user/Model-MagicQuant-GGUF")
    cfg.did_magicquant = True
    card = generate_model_card(cfg, files, sibling_repo_id="user/Model-ROCmFPX-GGUF")
    assert "huggingface.co/user/Model-ROCmFPX-GGUF" in card
    assert "do NOT load" not in card  # stock files: no fork warning
    assert "### LM Studio" in card


def test_rocmfpx_quant_hints_in_file_table(tmp_path):
    card = generate_model_card(_cfg(), _fpx_files(tmp_path), rocmfpx=True)
    assert "ROCmFP4 (fork-only)" in card
    assert "ROCmFP3 (fork-only), agent preset" in card


# ── generate_model_card: fixed defects (empty quant cell, fabricated usage
# filename, --chat-template chatml, missing vision section, missing fork
# pin) ─────────────────────────────────────────────────────────────────────

def _mq_hybrid_files(tmp_path):
    """MagicQuant-hybrid-in-ROCmFPX files: `_rocmfpx_entry._quantize_mq_hybrid`'s
    "<model>-ROCMFPX-MQ-<tier>.gguf" naming convention. MagicQuant Q5 has no
    direct Qn->ROCmFPn mapping (it rounds UP to FP6), which is exactly the
    case that used to leave the quant-hint cell empty."""
    out = tmp_path
    names = [
        "Model-ROCMFPX-MQ-Q4.gguf",
        "Model-ROCMFPX-MQ-Q5.gguf",
        "Model-ROCMFPX-MQ-Q6.gguf",
    ]
    files = []
    for n in names:
        p = out / n
        _touch(p)
        files.append((p, n))
    return files


def test_mq_hybrid_quant_label_non_empty():
    """Defect 1: MQ-Q5 (and Q4/Q6) filenames must get a non-empty, descriptive
    label instead of an empty table cell."""
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        files = _mq_hybrid_files(Path(d))
        card = generate_model_card(_cfg(), files, rocmfpx=True)
        assert "| [Model-ROCMFPX-MQ-Q5.gguf](./Model-ROCMFPX-MQ-Q5.gguf) | " in card
        # No empty quant cell for the Q5 hybrid row.
        assert "Model-ROCMFPX-MQ-Q5.gguf) | 0.0 GB |  |" not in card
        assert "MagicQuant Q5 layout in ROCmFPX types (hybrid, fork-only)" in card
        assert "MagicQuant Q4 layout in ROCmFPX types (hybrid, fork-only)" in card
        assert "MagicQuant Q6 layout in ROCmFPX types (hybrid, fork-only)" in card


def test_unmatched_filename_gets_fallback_label_not_empty():
    """Defect 1: any filename matching no known hint must still get a
    non-empty label (fallback "hybrid"/em-dash), never an empty cell."""
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "Model-mystery-format.gguf"
        _touch(p)
        card = generate_model_card(_cfg(), [(p, p.name)])
        assert "Model-mystery-format.gguf) | 0.0 GB |  |" not in card
        # A row with a truly empty quant cell looks like "| 0.0 GB | |".
        assert "| 0.0 GB | |" not in card


def test_usage_snippets_use_real_filename_not_fabricated(tmp_path):
    """Defect 2: usage snippets must reference an actual planned filename,
    never a synthesized "<repo_name>-Q5.gguf"."""
    out = Path(_out_with(tmp_path, mq=True))
    files = [(p, p.name) for p in sorted((out / "magicquant").glob("*.gguf"))]
    cfg = _cfg(repo_id="user/Model-MagicQuant-GGUF")
    cfg.did_magicquant = True
    card = generate_model_card(cfg, files)
    real_names = [rp for _, rp in files]
    repo_name = cfg.repo_id.split("/")[-1]
    assert f"{repo_name}-Q5.gguf" not in card  # the old fabricated pattern
    assert any(name in card for name in real_names)
    # Every llama-cli / llama-server / python model_path snippet in the
    # Usage section should use one of the real names.
    usage = card.split("## Usage", 1)[1]
    assert any(name in usage for name in real_names)


def test_usage_uses_jinja_not_chat_template_chatml(tmp_path):
    """Defect 3: `--chat-template chatml` overrides the embedded template;
    usage snippets should pass `--jinja` instead."""
    out = Path(_out_with(tmp_path, mq=True))
    files = [(p, p.name) for p in sorted((out / "magicquant").glob("*.gguf"))]
    cfg = _cfg(repo_id="user/Model-MagicQuant-GGUF")
    cfg.did_magicquant = True
    card = generate_model_card(cfg, files)
    assert "--jinja" in card
    assert "chatml" not in card
    assert "--chat-template" not in card


def test_rocmfpx_usage_also_uses_jinja(tmp_path):
    card = generate_model_card(_cfg(), _fpx_files(tmp_path), rocmfpx=True)
    assert "--jinja" in card
    assert "chatml" not in card


def test_vision_section_present_iff_mmproj(tmp_path):
    """Defect 4: a 'Vision (image input)' snippet appears exactly when an
    mmproj file is part of the upload, pairing a real text quant with the
    real mmproj filename."""
    out_no_mmproj = Path(_out_with(tmp_path, mq=True))
    files_no_mmproj = [(p, p.name) for p in sorted((out_no_mmproj / "magicquant").glob("*.gguf"))]
    cfg = _cfg(repo_id="user/Model-MagicQuant-GGUF")
    cfg.did_magicquant = True
    card_no_mmproj = generate_model_card(cfg, files_no_mmproj)
    assert "Vision (image input)" not in card_no_mmproj

    tmp_path2 = tmp_path / "with_mmproj"
    out_mmproj = Path(_out_with(tmp_path2, mq=True, mmproj=True))
    files_mmproj = [(p, p.name) for p in sorted((out_mmproj / "magicquant").glob("*.gguf"))]
    files_mmproj += [(p, p.name) for p in sorted((out_mmproj / "mmproj").glob("*.gguf"))]
    card_mmproj = generate_model_card(cfg, files_mmproj)
    assert "Vision (image input)" in card_mmproj
    assert "mmproj-Model-f16.gguf" in card_mmproj
    assert "--mmproj mmproj-Model-f16.gguf" in card_mmproj


def test_rocmfpx_card_contains_fork_pin(tmp_path):
    """Defect 5: the ROCmFPX card must name a pinned commit to check out,
    single-sourced from core/_rocmfpx_entry.py (not a copied SHA literal)."""
    import _rocmfpx_entry
    import hf_upload

    assert hf_upload.ROCMFPX_PIN is _rocmfpx_entry.ROCMFPX_PIN

    card = generate_model_card(_cfg(), _fpx_files(tmp_path), rocmfpx=True)
    assert _rocmfpx_entry.ROCMFPX_PIN in card
    assert "git checkout" in card
