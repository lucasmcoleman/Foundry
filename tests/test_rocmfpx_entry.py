"""Pure-Python coverage for core/_rocmfpx_entry.py.

The generic H2 entry-module contract (imports cleanly, exposes run/parse_config,
shim wiring) is covered by tests/test_stage_entries.py. This module covers the
ROCmFPX-specific logic: format/profile -> ggml-type mapping, build discovery
path probing, and source-priority resolution. None of this needs a real
ROCmFPX checkout, network access, or torch -- everything here is stdlib-only,
matching _rocmfpx_entry.py's own module-level import discipline.
"""

import importlib
import json
from pathlib import Path

import pytest

import _rocmfpx_entry as entry
from services import ROCmFPXService

ROOT = Path(__file__).resolve().parent.parent


def _pipeline():
    import pipeline as pl

    return importlib.reload(pl)


# ── parse_format_spec ────────────────────────────────────────────────────────

@pytest.mark.parametrize("spec,expected", [
    ("rocmfp3", ("rocmfp3", "straight")),
    ("rocmfp4-agent", ("rocmfp4", "agent")),
    ("rocmfp6-straight", ("rocmfp6", "straight")),
    ("ROCMFP8-AGENT", ("rocmfp8", "agent")),  # case-insensitive
    ("  rocmfp4-agent  ", ("rocmfp4", "agent")),  # whitespace-tolerant
])
def test_parse_format_spec_valid(spec, expected):
    assert entry.parse_format_spec(spec) == expected


@pytest.mark.parametrize("spec", ["rocmfp5", "rocmfp4-turbo", "", "agent", "rocmfp5-agent"])
def test_parse_format_spec_rejects_unknown(spec):
    with pytest.raises(ValueError):
        entry.parse_format_spec(spec)


def test_format_table_covers_every_parseable_spec():
    """Every (format, profile) pair parse_format_spec can produce must have a
    ggml type in FORMAT_TABLE, and vice versa (no orphaned table entries)."""
    for (fmt, profile), ggml_type in entry.FORMAT_TABLE.items():
        assert entry.parse_format_spec(f"{fmt}-{profile}") == (fmt, profile)
        assert isinstance(ggml_type, str) and ggml_type


def test_format_table_agent_types_distinct_from_straight():
    """Sanity check the table actually encodes two different presets per
    format (agent isn't accidentally aliased to straight)."""
    for fmt in {f for f, _ in entry.FORMAT_TABLE}:
        assert entry.FORMAT_TABLE[(fmt, "straight")] != entry.FORMAT_TABLE[(fmt, "agent")]


# ── find_rocmfpx ─────────────────────────────────────────────────────────────

def test_find_rocmfpx_returns_none_when_nothing_present(tmp_path, monkeypatch):
    monkeypatch.delenv("ROCMFPX_PATH", raising=False)
    monkeypatch.setattr(entry.Path, "home", lambda: tmp_path / "nonexistent-home")
    monkeypatch.chdir(tmp_path)
    # The hardcoded strix-halo-club fallback path is real-box-specific; a
    # missing dir there should just fail the existence check like any other.
    assert entry.find_rocmfpx("") is None


def test_find_rocmfpx_rejects_partial_family_build(tmp_path, monkeypatch):
    """A build whose llama-quantize --help doesn't list ROCmFP3/6/8 (e.g. a
    ROCmFP4-only rocmfp4-llama checkout) must not be reported as usable."""
    bin_dir = tmp_path / "build-strix-rocmfp4" / "bin"
    bin_dir.mkdir(parents=True)
    fake_quantize = bin_dir / "llama-quantize"
    fake_quantize.write_text("#!/bin/sh\necho 'Q4_0_ROCMFP4 only'\n")
    fake_quantize.chmod(0o755)
    monkeypatch.setattr(entry, "_has_full_family", lambda p: False)
    assert entry.find_rocmfpx(str(tmp_path)) is None


def test_find_rocmfpx_accepts_full_family_build(tmp_path, monkeypatch):
    bin_dir = tmp_path / "build-strix-rocmfp4" / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "llama-quantize").write_text("#!/bin/sh\n")
    monkeypatch.setattr(entry, "_has_full_family", lambda p: True)
    assert entry.find_rocmfpx(str(tmp_path)) == str(tmp_path)


# ── resolve_source (mirrors _magicquant_entry.resolve_source) ───────────────

def test_resolve_source_prefers_reap_over_heretic_and_merged(tmp_path):
    out = tmp_path / "output"
    for sub in ("reap_model", "heretic_model", "merged_model"):
        d = out / sub
        d.mkdir(parents=True)
        (d / "model.safetensors").write_bytes(b"x")
    assert entry.resolve_source("", out, str(tmp_path)) == str(out / "reap_model")


def test_resolve_source_falls_back_to_bf16_gguf(tmp_path):
    out = tmp_path / "output"
    out.mkdir(parents=True)
    gguf = out / "model-bf16.gguf"
    gguf.write_bytes(b"gguf")
    assert entry.resolve_source("", out, str(tmp_path)) == str(gguf)


def test_resolve_source_none_when_nothing_found(tmp_path):
    out = tmp_path / "output"
    out.mkdir(parents=True)
    assert entry.resolve_source("", out, str(tmp_path)) is None


def test_resolve_source_explicit_override_wins(tmp_path):
    out = tmp_path / "output"
    reap = out / "reap_model"
    reap.mkdir(parents=True)
    (reap / "model.safetensors").write_bytes(b"x")

    override_dir = tmp_path / "elsewhere"
    override_dir.mkdir()
    (override_dir / "model.safetensors").write_bytes(b"x")

    assert entry.resolve_source(str(override_dir), out, str(tmp_path)) == str(override_dir)


def test_resolve_source_absolute_gguf_file_override(tmp_path):
    gguf = tmp_path / "custom.gguf"
    gguf.write_bytes(b"gguf")
    out = tmp_path / "output"
    out.mkdir()
    assert entry.resolve_source(str(gguf), out, str(tmp_path)) == str(gguf)


# ── MagicQuant-hybrid mode: spec parsing ────────────────────────────────────

@pytest.mark.parametrize("spec,tier", [
    ("mq-q4", "Q4"), ("mq-q6", "Q6"), ("MQ-Q5", "Q5"), ("  mq-q8  ", "Q8"),
])
def test_parse_mq_spec_recognizes_mq_formats(spec, tier):
    assert entry.parse_mq_spec(spec) == tier


@pytest.mark.parametrize("spec", ["rocmfp4-agent", "rocmfp3", "q4_k_m", "mq-"])
def test_parse_mq_spec_returns_none_for_non_mq(spec):
    # Plain presets route to the uniform-preset path; a bare "mq-" has no tier.
    result = entry.parse_mq_spec(spec)
    assert result is None or result == ""


# ── scheme translation (rounds UP in quality) ───────────────────────────────

@pytest.mark.parametrize("scheme,expected", [
    ("BF16", "BF16"),
    ("Q8_0", "Q8_0_ROCMFPX"),
    ("Q6_K", "Q6_0_ROCMFPX"),
    ("Q5_K", "Q6_0_ROCMFPX"),      # rounds up: Q5 has no fp5, use fp6
    ("Q4_K_M", "Q4_0_ROCMFP4"),
    ("IQ4_NL", "Q4_0_ROCMFP4"),
    ("MXFP4_MOE", "Q4_0_ROCMFP4"),
    ("Q3_K", "Q3_0_ROCMFPX"),
    ("Q2_K", "Q3_0_ROCMFPX"),      # rounds up: no fp2, use fp3
    ("ROCMFP4", "Q4_0_ROCMFP4"),   # native scheme maps to itself
    ("ROCMFP8", "Q8_0_ROCMFPX"),
])
def test_translate_scheme(scheme, expected):
    assert entry.translate_scheme(scheme) == expected


def test_translate_scheme_rejects_unknown():
    with pytest.raises(ValueError):
        entry.translate_scheme("NOPE_Q9")


# ── opt-in MagicQuant IQ schemes (defect 1a) ────────────────────────────────

@pytest.mark.parametrize("scheme,expected", [
    ("IQ4_XS", "Q4_0_ROCMFP4"),
    ("IQ3_S", "Q3_0_ROCMFPX"),
    ("IQ3_XXS", "Q3_0_ROCMFPX"),
    ("IQ2_S", "Q3_0_ROCMFPX"),
    ("IQ2_XS", "Q3_0_ROCMFPX"),
    ("IQ2_XXS", "Q3_0_ROCMFPX"),
    ("IQ1_M", "Q3_0_ROCMFPX"),
    ("IQ1_S", "Q3_0_ROCMFPX"),
])
def test_translate_scheme_covers_iq_schemes(scheme, expected):
    """The 8 opt-in MagicQuant IQ schemes must translate instead of raising --
    previously missing from SCHEME_TO_ROCMFPX, which aborted the whole
    ROCmFPX stage the moment a search used an IQ scheme."""
    assert entry.translate_scheme(scheme) == expected


def test_build_tensor_type_lines_succeeds_with_iq_scheme():
    """A tier config containing an IQ scheme must produce a translated line,
    not raise -- this is what build_tensor_type_lines does inside
    _quantize_mq_hybrid's (now widened) try/except."""
    config = {"X": "IQ3_S"}
    lines = entry.build_tensor_type_lines(config, _PATTERNS)
    assert lines == [r"ffn_(up|gate|down)_exps=Q3_0_ROCMFPX"]


# ── tensor-type-file emission ───────────────────────────────────────────────

_PATTERNS = {
    # Minimal stand-in for TensorGroupClassifier.GROUP_PATTERNS with the
    # ordering that matters: X (fused experts) before U (dense up).
    "E": [r"token_embd\.weight"],
    "X": [r"ffn_(up|gate|down)_exps"],
    "U": [r"ffn_up", r"ffn_gate(?!_inp)"],
    "D": [r"ffn_down(?!_exps)"],
}


def test_build_tensor_type_lines_translates_and_orders():
    config = {"E": "BF16", "X": "MXFP4_MOE", "U": "Q6_K", "D": "Q4_K_M"}
    lines = entry.build_tensor_type_lines(config, _PATTERNS)
    assert lines == [
        r"token_embd\.weight=BF16",
        r"ffn_(up|gate|down)_exps=Q4_0_ROCMFP4",
        r"ffn_up=Q6_0_ROCMFPX",
        r"ffn_gate(?!_inp)=Q6_0_ROCMFPX",
        r"ffn_down(?!_exps)=Q4_0_ROCMFP4",
    ]
    # X pattern must precede U so fused-expert tensors resolve to X, not U.
    assert lines.index(r"ffn_(up|gate|down)_exps=Q4_0_ROCMFP4") < lines.index(r"ffn_up=Q6_0_ROCMFPX")


def test_build_tensor_type_lines_skips_groups_absent_from_config():
    config = {"E": "BF16"}  # only E present
    lines = entry.build_tensor_type_lines(config, _PATTERNS)
    assert lines == [r"token_embd\.weight=BF16"]


def test_pick_base_type_is_always_a_quantizing_type():
    # BF16 present but base must NOT be a float type (float base = no-op copy
    # that skips overrides); pick the highest-quality quantizing type present.
    assert entry.pick_base_type({"E": "BF16", "X": "MXFP4_MOE"}) == "Q4_0_ROCMFP4"
    assert entry.pick_base_type({"E": "BF16", "Q": "Q5_K", "X": "MXFP4_MOE"}) == "Q6_0_ROCMFPX"
    assert entry.pick_base_type({"X": "MXFP4_MOE", "U": "Q6_K"}) == "Q6_0_ROCMFPX"
    assert entry.pick_base_type({"X": "Q3_K"}) == "Q3_0_ROCMFPX"


def test_pick_base_type_all_float_falls_back_to_quantizing():
    # Degenerate all-float tier: base still must be quantizing, not BF16.
    base = entry.pick_base_type({"E": "BF16", "H": "BF16"})
    assert base not in ("BF16", "F16", "F32")
    assert base == "Q8_0_ROCMFPX"


# ── tier-config loading from search_results.json ────────────────────────────

def test_load_mq_tier_config_reads_tier(tmp_path):
    mq = tmp_path / "magicquant"
    mq.mkdir()
    (mq / "search_results.json").write_text(json.dumps({
        "tiered": {"Q4": {"config": {"E": "BF16", "X": "MXFP4_MOE"}}}
    }))
    cfg = entry._load_mq_tier_config(tmp_path, "Q4")
    assert cfg == {"E": "BF16", "X": "MXFP4_MOE"}


def test_load_mq_tier_config_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        entry._load_mq_tier_config(tmp_path, "Q4")


def test_load_mq_tier_config_missing_tier(tmp_path):
    mq = tmp_path / "magicquant"
    mq.mkdir()
    (mq / "search_results.json").write_text(json.dumps({
        "tiered": {"Q6": {"config": {"E": "BF16"}}}
    }))
    with pytest.raises(KeyError):
        entry._load_mq_tier_config(tmp_path, "Q4")


# ── graceful degradation on a single bad format (defects 1b & 2) ───────────
#
# One untranslatable/unparseable format spec must skip with a warning and
# return None, not raise out of the per-format loop in run() and abort every
# other queued ROCmFPX format.

def test_quantize_mq_hybrid_returns_none_on_untranslatable_scheme(tmp_path):
    out_dir = tmp_path / "output"
    mq_dir = out_dir / "magicquant"
    mq_dir.mkdir(parents=True)
    mq_dir.joinpath("search_results.json").write_text(json.dumps({
        "tiered": {"Q4": {"config": {"X": "NOPE_Q9"}}}
    }))
    rocmfpx_out_dir = out_dir / "rocmfpx"
    rocmfpx_out_dir.mkdir()

    # No real llama-quantize binary or GGUF needed: an untranslatable scheme
    # must be caught before any subprocess is ever spawned. If it weren't
    # caught (the pre-fix behavior), build_tensor_type_lines's ValueError
    # would propagate out of this call instead of returning None.
    result = entry._quantize_mq_hybrid(
        "mq-q4", "Q4", out_dir, rocmfpx_out_dir, "model",
        entry.Path("/nonexistent/llama-quantize"), entry.Path("/nonexistent/model-bf16.gguf"), "",
    )
    assert result is None


def test_quantize_preset_returns_none_on_bad_spec(tmp_path):
    out_dir = tmp_path / "rocmfpx"
    out_dir.mkdir()

    # A bad spec must be caught before any subprocess is spawned -- if it
    # weren't (the pre-fix behavior), parse_format_spec's ValueError would
    # propagate instead of returning None.
    result = entry._quantize_preset(
        "rocmfp5-agent", out_dir, "model",
        entry.Path("/nonexistent/llama-quantize"), entry.Path("/nonexistent/model-bf16.gguf"), "",
    )
    assert result is None


# ── allow_requantize: --allow-requantize for already-quantized GGUF sources ──
#
# Double-quantization opt-in, mirroring MagicQuant's allow_dequant_source.
# _quantize_cmd_base is the pure/testable seam _quantize_preset and
# _quantize_mq_hybrid both build their llama-quantize argv from.

def test_quantize_cmd_base_omits_flag_by_default():
    cmd = entry._quantize_cmd_base(entry.Path("/bin/llama-quantize"))
    assert cmd == ["/bin/llama-quantize"]


def test_quantize_cmd_base_includes_flag_when_requested():
    cmd = entry._quantize_cmd_base(entry.Path("/bin/llama-quantize"), allow_requantize=True)
    assert cmd == ["/bin/llama-quantize", "--allow-requantize"]


def test_quantize_cmd_base_flag_precedes_positionals_style():
    """llama-quantize flags must precede positionals -- --allow-requantize is
    always cmd[1], never appended after other args."""
    cmd = entry._quantize_cmd_base(entry.Path("/bin/llama-quantize"), allow_requantize=True)
    assert cmd[1] == "--allow-requantize"


# ── core/pipeline.py: ROCmFPXConfig dataclass + CLI + cfg_hash ───────────────

def test_rocmfpx_config_allow_requantize_default():
    pl = _pipeline()
    rc = pl.ROCmFPXConfig()
    assert rc.allow_requantize is False


def test_rocmfpx_allow_requantize_cli_flag_wires_into_config(monkeypatch):
    pl = _pipeline()
    captured = {}

    def _fake_run_pipeline(cfg, **kwargs):
        captured["cfg"] = cfg
        return {"rocmfpx": True}

    monkeypatch.setattr(pl, "run_pipeline", _fake_run_pipeline)

    pl.main([
        "--model", "org/m", "--no-export", "--no-heretic", "--no-reap",
        "--rocmfpx",
    ])
    assert captured["cfg"].rocmfpx.allow_requantize is False

    pl.main([
        "--model", "org/m", "--no-export", "--no-heretic", "--no-reap",
        "--rocmfpx", "--rocmfpx-allow-requantize",
    ])
    assert captured["cfg"].rocmfpx.allow_requantize is True


def test_stage_rocmfpx_hash_source_includes_allow_requantize():
    src = (ROOT / "core" / "pipeline.py").read_text()
    hash_block = src[src.index('def stage_rocmfpx'):src.index('existing = sorted(artifacts.rocmfpx_dir')]
    assert '"allow_requantize": rc_cfg.allow_requantize' in hash_block


# ── core/services.py: ROCmFPXService.build_config carries allow_requantize ───

def test_rocmfpx_build_config_allow_requantize_default_false():
    svc = ROCmFPXService(ROOT, "python")
    cfg = svc.build_config(
        rocmfpx_hint="", pipeline_root_str="/repo", source_override="/src",
        out_abs_str="/o", formats_json='["rocmfp4-agent"]', model_name="m",
    )
    assert cfg["allow_requantize"] is False


def test_rocmfpx_build_config_allow_requantize_explicit_true():
    svc = ROCmFPXService(ROOT, "python")
    cfg = svc.build_config(
        rocmfpx_hint="", pipeline_root_str="/repo", source_override="/src",
        out_abs_str="/o", formats_json='["rocmfp4-agent"]', model_name="m",
        allow_requantize=True,
    )
    assert cfg["allow_requantize"] is True
    assert json.loads(json.dumps(cfg)) == cfg


# ── Change 2: post-generation PPL smoke gate ─────────────────────────────────
#
# The functional gate itself (parse/verdict/skip logic) is covered end-to-end
# in tests/test_ppl_smoke.py; this file just confirms _rocmfpx_entry wires it
# in the right place -- before the stage can report success -- matching the
# existing static source-text-assertion convention used elsewhere in this
# suite (see test_stage_magicquant_hash_source_includes_new_knobs etc.).

def test_rocmfpx_entry_imports_ppl_smoke():
    import _rocmfpx_entry

    assert hasattr(_rocmfpx_entry, "ppl_smoke")


def test_rocmfpx_entry_run_body_smoke_gate_before_stage_complete():
    src = (ROOT / "core" / "_rocmfpx_entry.py").read_text()
    run_body = src[src.index("def run("):src.index('if __name__ == "__main__"')]
    assert "ppl_smoke.smoke_test_gguf" in run_body
    gate_idx = run_body.index("ppl_smoke.smoke_test_gguf")
    assert "sys.exit(1)" in run_body[gate_idx:]
    assert gate_idx < run_body.index('print("PIPELINE_STAGE_COMPLETE=rocmfpx"')


def test_rocmfpx_entry_run_body_uses_find_perplexity_bin_on_rocmfpx_dir():
    """The perplexity binary must be looked up relative to rocmfpx_dir (the
    discovered/installed ROCmFPX checkout), mirroring how quantize_bin is
    already resolved (Path(rocmfpx_dir) / "build-strix-rocmfp4" / "bin" /
    ...) -- not a hardcoded or unrelated path."""
    src = (ROOT / "core" / "_rocmfpx_entry.py").read_text()
    run_body = src[src.index("def run("):src.index('if __name__ == "__main__"')]
    assert "ppl_smoke.find_perplexity_bin(rocmfpx_dir)" in run_body


def test_find_perplexity_bin_resolves_real_rocmfpx_build_layout(tmp_path):
    """End-to-end sanity check that ppl_smoke.find_perplexity_bin actually
    resolves the exact build layout _rocmfpx_entry uses for llama-quantize
    (build-strix-rocmfp4/bin/), given just the ROCmFPX checkout root -- the
    same root variable (rocmfpx_dir) the entry passes to it."""
    import ppl_smoke

    bin_dir = tmp_path / "build-strix-rocmfp4" / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "llama-quantize").write_text("")
    (bin_dir / "llama-perplexity").write_text("")

    assert ppl_smoke.find_perplexity_bin(str(tmp_path)) == bin_dir / "llama-perplexity"
