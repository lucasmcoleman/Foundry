"""Pure-Python coverage for core/_rocmfpx_entry.py.

The generic H2 entry-module contract (imports cleanly, exposes run/parse_config,
shim wiring) is covered by tests/test_stage_entries.py. This module covers the
ROCmFPX-specific logic: format/profile -> ggml-type mapping, build discovery
path probing, and source-priority resolution. None of this needs a real
ROCmFPX checkout, network access, or torch -- everything here is stdlib-only,
matching _rocmfpx_entry.py's own module-level import discipline.
"""

import json

import pytest

import _rocmfpx_entry as entry


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


# ── tg-safe steering (opt-in, default off) ──────────────────────────────────

def test_tg_safe_rocmfpx_steers_the_slow_types():
    """Q3_0_ROCMFPX/Q6_0_ROCMFPX pay a bit-stitch + LUT decode tax on tg
    (no AMD-ISA fast path); steer them to their tg-fast siblings."""
    assert entry.tg_safe_rocmfpx("Q3_0_ROCMFPX") == "Q4_0_ROCMFP4"
    assert entry.tg_safe_rocmfpx("Q6_0_ROCMFPX") == "Q8_0_ROCMFPX"


@pytest.mark.parametrize("already_fast", [
    "Q4_0_ROCMFP4", "Q4_0_ROCMFP4_FAST", "Q8_0_ROCMFPX",
])
def test_tg_safe_rocmfpx_leaves_already_fast_types_unchanged(already_fast):
    assert entry.tg_safe_rocmfpx(already_fast) == already_fast


def test_tg_safe_rocmfpx_passes_through_types_outside_the_map():
    # Float/other types not covered by the steering map pass through as-is.
    assert entry.tg_safe_rocmfpx("BF16") == "BF16"
    assert entry.tg_safe_rocmfpx("F32") == "F32"


def test_translate_scheme_tg_safe_steers_the_slow_schemes():
    # Q3_K -> Q3_0_ROCMFPX (faithful) -> Q4_0_ROCMFP4 (steered)
    assert entry.translate_scheme("Q3_K", tg_safe=True) == "Q4_0_ROCMFP4"
    # Q6_K -> Q6_0_ROCMFPX (faithful) -> Q8_0_ROCMFPX (steered)
    assert entry.translate_scheme("Q6_K", tg_safe=True) == "Q8_0_ROCMFPX"


def test_translate_scheme_tg_safe_false_matches_faithful_default():
    """tg_safe=False must be byte-identical to today's (no-tg_safe-arg)
    behavior for every known scheme."""
    for scheme in entry.SCHEME_TO_ROCMFPX:
        assert entry.translate_scheme(scheme, tg_safe=False) == entry.translate_scheme(scheme)


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
    # ordering that matters: X (fused experts) before U (dense up). Q/K/O/H
    # (attention + head groups) are appended after D so the append doesn't
    # disturb the X-before-U ordering the tests above rely on.
    "E": [r"token_embd\.weight"],
    "X": [r"ffn_(up|gate|down)_exps"],
    "U": [r"ffn_up", r"ffn_gate(?!_inp)"],
    "D": [r"ffn_down(?!_exps)"],
    "H": [r"output\.weight"],
    "Q": [r"attn_q"],
    "K": [r"attn_k"],
    "O": [r"attn_output"],
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


# ── build_tensor_type_lines tg-safe steering (opt-in, default off) ─────────

def test_build_tensor_type_lines_tg_safe_steers_only_high_traffic_groups():
    """tg_safe=True must steer U/D/X (the bandwidth-heavy FFN/expert bulk
    that dominates tg) to their tg-fast siblings, while attention/embedding/
    head groups (Q/K/O/E/H) keep the faithful translation untouched."""
    config = {
        "E": "BF16", "H": "Q6_K",           # untouched groups
        "Q": "Q3_K", "K": "Q3_K", "O": "Q6_K",  # untouched groups
        "X": "Q3_K", "U": "Q6_K", "D": "Q3_K",  # steered groups
    }
    lines = entry.build_tensor_type_lines(config, _PATTERNS, tg_safe=True)
    by_pattern = dict(line.split("=", 1) for line in lines)

    # Attention/embedding/head groups: faithful (unsteered) translation.
    assert by_pattern[r"token_embd\.weight"] == "BF16"
    assert by_pattern[r"output\.weight"] == "Q6_0_ROCMFPX"
    assert by_pattern[r"attn_q"] == "Q3_0_ROCMFPX"
    assert by_pattern[r"attn_k"] == "Q3_0_ROCMFPX"
    assert by_pattern[r"attn_output"] == "Q6_0_ROCMFPX"

    # High-traffic FFN/expert groups: steered to their tg-fast siblings.
    assert by_pattern[r"ffn_(up|gate|down)_exps"] == "Q4_0_ROCMFP4"  # X: fp3 -> fp4
    assert by_pattern[r"ffn_up"] == "Q8_0_ROCMFPX"                    # U: fp6 -> Q8
    assert by_pattern[r"ffn_gate(?!_inp)"] == "Q8_0_ROCMFPX"          # U: fp6 -> Q8
    assert by_pattern[r"ffn_down(?!_exps)"] == "Q4_0_ROCMFP4"         # D: fp3 -> fp4


def test_build_tensor_type_lines_tg_safe_false_is_byte_identical_to_default():
    """tg_safe=False must be byte-identical to calling without the arg at all
    (today's faithful-translation behavior stays the default)."""
    config = {
        "E": "BF16", "H": "Q6_K", "Q": "Q3_K", "K": "Q3_K", "O": "Q6_K",
        "X": "Q3_K", "U": "Q6_K", "D": "Q3_K",
    }
    assert (
        entry.build_tensor_type_lines(config, _PATTERNS, tg_safe=False)
        == entry.build_tensor_type_lines(config, _PATTERNS)
    )


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


# ── _quantize_mq_hybrid tg-safe threading (opt-in, default off) ────────────

def test_quantize_mq_hybrid_tg_safe_steers_the_written_type_file(tmp_path, monkeypatch):
    """tg_safe=True must reach build_tensor_type_lines and steer the
    high-traffic groups in the emitted --tensor-type-file, while attention
    groups in the same tier stay faithful. The type file is written before
    the quantize subprocess call, so stub subprocess.run to a no-op (no real
    llama-quantize binary needed) and inspect the file it wrote."""
    import subprocess
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 1))

    out_dir = tmp_path / "output"
    mq_dir = out_dir / "magicquant"
    mq_dir.mkdir(parents=True)
    mq_dir.joinpath("search_results.json").write_text(json.dumps({
        "tiered": {"Q4": {"config": {"X": "Q3_K", "Q": "Q3_K"}}}
    }))
    rocmfpx_out_dir = out_dir / "rocmfpx"
    rocmfpx_out_dir.mkdir()

    entry._quantize_mq_hybrid(
        "mq-q4", "Q4", out_dir, rocmfpx_out_dir, "model",
        entry.Path("/nonexistent/llama-quantize"), entry.Path("/nonexistent/model-bf16.gguf"), "",
        tg_safe=True,
    )
    lines = (rocmfpx_out_dir / "_ttf-mq-Q4.txt").read_text().splitlines()
    by_pattern = dict(line.split("=", 1) for line in lines if line)
    # X (fused experts, high-traffic): steered fp3 -> fp4.
    assert by_pattern[r"ffn_(up|gate|down)_exps"] == "Q4_0_ROCMFP4"
    # Q (attention, not high-traffic): faithful, unsteered fp3.
    assert by_pattern[r"attn_q\.weight"] == "Q3_0_ROCMFPX"


def test_quantize_mq_hybrid_tg_safe_default_false_matches_faithful_output(tmp_path, monkeypatch):
    """Calling _quantize_mq_hybrid without tg_safe (today's call sites) must
    produce the byte-identical type file to an explicit tg_safe=False."""
    import subprocess
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 1))

    out_dir = tmp_path / "output"
    mq_dir = out_dir / "magicquant"
    mq_dir.mkdir(parents=True)
    mq_dir.joinpath("search_results.json").write_text(json.dumps({
        "tiered": {"Q4": {"config": {"X": "Q3_K", "U": "Q6_K"}}}
    }))

    rocmfpx_out_dir_a = out_dir / "rocmfpx_a"
    rocmfpx_out_dir_a.mkdir()
    entry._quantize_mq_hybrid(
        "mq-q4", "Q4", out_dir, rocmfpx_out_dir_a, "model",
        entry.Path("/nonexistent/llama-quantize"), entry.Path("/nonexistent/model-bf16.gguf"), "",
    )
    rocmfpx_out_dir_b = out_dir / "rocmfpx_b"
    rocmfpx_out_dir_b.mkdir()
    entry._quantize_mq_hybrid(
        "mq-q4", "Q4", out_dir, rocmfpx_out_dir_b, "model",
        entry.Path("/nonexistent/llama-quantize"), entry.Path("/nonexistent/model-bf16.gguf"), "",
        tg_safe=False,
    )
    assert (
        (rocmfpx_out_dir_a / "_ttf-mq-Q4.txt").read_text()
        == (rocmfpx_out_dir_b / "_ttf-mq-Q4.txt").read_text()
    )


# ── run() cfg threading (opt-in, default off) ───────────────────────────────

def test_run_reads_tg_safe_from_cfg_and_forwards_it(tmp_path, monkeypatch):
    """run() must read cfg.get("tg_safe") and pass it through to
    _quantize_mq_hybrid for an mq-* format spec."""
    out_dir = tmp_path / "output"
    out_dir.mkdir()
    pipeline_root = tmp_path / "pipeline"
    (pipeline_root / "core").mkdir(parents=True)

    cfg_path = tmp_path / "cfg.json"
    cfg_path.write_text(json.dumps({
        "pipeline_root": str(pipeline_root),
        "pipeline_root_str": str(pipeline_root),
        "out_abs_str": str(out_dir),
        "formats_json": json.dumps(["mq-q4"]),
        "model_name": "model",
        "rocmfpx_hint": "",
        "imatrix": "",
        "tg_safe": True,
    }))

    monkeypatch.setattr(entry, "ensure_rocmfpx", lambda hint: str(tmp_path / "rocmfpx-build"))
    monkeypatch.setattr(entry, "resolve_source", lambda override, out, root: str(out_dir / "model.safetensors"))
    monkeypatch.setattr(entry, "_ensure_bf16_gguf", lambda rocmfpx_dir, source, out: str(out_dir / "model-bf16.gguf"))

    captured = {}

    def fake_quantize_mq_hybrid(spec, tier, out, rocmfpx_out, model_name,
                                 quantize_bin, bf16_gguf, imatrix, tg_safe=False):
        captured["tg_safe"] = tg_safe
        return rocmfpx_out / "fake.gguf"

    monkeypatch.setattr(entry, "_quantize_mq_hybrid", fake_quantize_mq_hybrid)

    entry.run(str(cfg_path))
    assert captured["tg_safe"] is True


# ── build_config threading (core/services.py -> _rocmfpx_entry.py) ─────────

def test_rocmfpx_service_build_config_threads_tg_safe():
    """The flag must reach the JSON config _rocmfpx_entry.run() reads, via
    ROCmFPXService.build_config -- the same shim path CLI/UI both use."""
    from pathlib import Path as _Path

    from services import ROCmFPXService

    svc = ROCmFPXService(_Path("/tmp/pipeline-root"), "/usr/bin/python3")
    common = dict(
        rocmfpx_hint="", pipeline_root_str="/tmp/pipeline-root",
        source_override="", out_abs_str="/tmp/out",
        formats_json="[]", model_name="model",
    )
    assert svc.build_config(**common)["tg_safe"] is False  # default off
    assert svc.build_config(**common, tg_safe=True)["tg_safe"] is True
    assert svc.build_config(**common, tg_safe=False)["tg_safe"] is False
