"""mq-budget renders a v2 per-tensor allocation in ROCmFPX types. The
per-tensor path must emit anchored, escaped, whitespace-free lines (the fork
parses `<regex>=<TYPE>` tokens split on whitespace and matches with unanchored
regex_search); the size guard shares BUDGET_TOLERANCE with publish so
build-time and publish-time can never disagree."""
import json
import re
import types
from pathlib import Path

import pytest

from core._rocmfpx_entry import (build_tensor_type_lines_per_tensor,
                                 parse_mq_spec, predict_rendered_budget,
                                 _quantize_mq_budget, _record_refusal,
                                 _resolve_budget_key, _type_bpw,
                                 translate_scheme)
from core.publish_criteria import BUDGET_TOLERANCE

import core._rocmfpx_entry as entry


# ── parse_mq_spec: the budget sentinel + explicit-key forms ────────────────

def test_parse_mq_spec_budget_sentinel_and_explicit_key():
    assert parse_mq_spec("mq-budget") == "BUDGET"
    assert parse_mq_spec("mq-budget=BUDGET-12.5GiB") == "BUDGET-12.5GiB"
    assert parse_mq_spec("mq-q4") == "Q4"          # unchanged
    assert parse_mq_spec("rocmfp4-agent") is None  # unchanged


def test_parse_mq_spec_budget_key_is_case_exact_not_uppercased():
    """Budget keys are case-sensitive as written by budget_tier_key -- the
    generic mq-<tier> path uppercases, but the budget key form must NOT, or
    'BUDGET-12.5GiB' would come back mangled as 'BUDGET-12.5GIB' and never
    match what's actually in search_results.json."""
    key = parse_mq_spec("mq-budget=BUDGET-12.5GiB")
    assert key == "BUDGET-12.5GiB"
    assert key != key.upper()


def test_parse_mq_spec_budget_prefix_case_insensitive():
    assert parse_mq_spec("MQ-BUDGET=BUDGET-7GiB") == "BUDGET-7GiB"


# ── _resolve_budget_key ──────────────────────────────────────────────────────

def _write_results(tmp_path, keys):
    d = tmp_path / "magicquant"
    d.mkdir(parents=True, exist_ok=True)
    (d / "search_results.json").write_text(json.dumps(
        {"tier_scheme_version": 2,
         "tiered": {k: {"config": {"U": "MXFP4_MOE"},
                        "tensor_config": {"blk.0.ffn_up.weight": "MXFP4_MOE"}}
                    for k in keys}}))


def test_resolve_single_budget_key(tmp_path):
    _write_results(tmp_path, ["Q4", "BUDGET-12.5GiB"])
    assert _resolve_budget_key(tmp_path, "BUDGET") == "BUDGET-12.5GiB"


def test_resolve_zero_keys_is_loud(tmp_path):
    _write_results(tmp_path, ["Q4"])
    with pytest.raises(Exception) as e:
        _resolve_budget_key(tmp_path, "BUDGET")
    assert "Q4" in str(e.value)          # lists what IS available


def test_resolve_multiple_requires_explicit(tmp_path):
    _write_results(tmp_path, ["BUDGET-10GiB", "BUDGET-20GiB"])
    with pytest.raises(Exception) as e:
        _resolve_budget_key(tmp_path, "BUDGET")
    assert "BUDGET-10GiB" in str(e.value) and "mq-budget=" in str(e.value)
    assert _resolve_budget_key(tmp_path, "BUDGET-20GiB") == "BUDGET-20GiB"


def test_resolve_explicit_key_absent_lists_candidates(tmp_path):
    _write_results(tmp_path, ["BUDGET-10GiB"])
    with pytest.raises(Exception) as e:
        _resolve_budget_key(tmp_path, "BUDGET-999GiB")
    assert "BUDGET-10GiB" in str(e.value)


def test_resolve_missing_search_results_file_is_loud(tmp_path):
    with pytest.raises(FileNotFoundError):
        _resolve_budget_key(tmp_path, "BUDGET")


# ── build_tensor_type_lines_per_tensor ──────────────────────────────────────

def test_per_tensor_lines_are_anchored_and_escaped():
    lines = build_tensor_type_lines_per_tensor(
        {"blk.0.ffn_up.weight": "MXFP4_MOE"})
    assert len(lines) == 1
    pattern, _, ggml = lines[0].partition("=")
    assert pattern.startswith("^") and pattern.endswith("$")
    assert re.search(pattern, "blk.0.ffn_up.weight")
    # Same length as the real name (dots -> 'X'), so this only fails to match
    # if '.' is escaped to a literal -- an unescaped '.' (regex "any char")
    # would match here too, since it's a same-position, same-length swap.
    # The old probe ("blk.10.ffn_up.weight") differed in LENGTH from the
    # anchored pattern and so proved nothing about escaping specifically.
    assert not re.search(pattern, "blkX0Xffn_upXweight")  # '.' escaped
    assert " " not in lines[0]
    assert ggml == translate_scheme("MXFP4_MOE")


def test_per_tensor_lines_refuse_hostile_names():
    with pytest.raises(ValueError, match="bad name"):
        build_tensor_type_lines_per_tensor({"bad name": "Q5_K"})
    with pytest.raises(ValueError, match=re.escape("bad=name")):
        build_tensor_type_lines_per_tensor({"bad=name": "Q5_K"})


def test_per_tensor_lines_cover_every_tensor_in_the_map():
    cfg = {"blk.0.ffn_up.weight": "MXFP4_MOE", "blk.0.attn_q.weight": "Q6_K"}
    lines = build_tensor_type_lines_per_tensor(cfg)
    assert len(lines) == 2
    names = {re.search(r"\^(.+)\$=", line).group(1) for line in lines}
    assert names == {re.escape(n) for n in cfg}


# ── predict_rendered_budget / _type_bpw ─────────────────────────────────────

class _StubReader:
    """One synthetic tensor covered by tensor_config, one untouched."""

    _SHAPES = {"blk.0.ffn_up.weight": [1000], "blk.0.attn_norm.weight": [100]}

    def __init__(self, path):
        self.path = path

    def open(self):
        pass

    def close(self):
        pass

    def get_tensor_names(self):
        return list(self._SHAPES)

    def get_tensor_info(self, name):
        return {"shape": self._SHAPES[name]}


@pytest.fixture
def patched_reader(monkeypatch):
    import magicquant.gguf.reader as reader_mod
    monkeypatch.setattr(reader_mod, "GGUFReader", _StubReader)


def test_predict_rendered_budget_sums_per_tensor_scheme(patched_reader):
    pred_gib, base_gib = predict_rendered_budget(
        {"blk.0.ffn_up.weight": "MXFP4_MOE"}, "stub.gguf")

    ggml_type = translate_scheme("MXFP4_MOE")
    bpw = _type_bpw(ggml_type)
    expected_bits = 1000 * bpw + 100 * 32.0  # untouched tensor counts as 32-bit
    expected_gib = expected_bits / 8.0 / 2 ** 30
    assert pred_gib == pytest.approx(expected_gib, rel=1e-9)

    expected_base = 1100 * 16.0 / 8.0 / 2 ** 30
    assert base_gib == pytest.approx(expected_base, rel=1e-9)


def test_predict_rendered_budget_untouched_tensors_are_32bit_not_16bit(patched_reader):
    """Pin the convention against predict_rendered_tier's own -- an empty
    tensor_config must NOT collapse to the BF16 baseline math."""
    pred_gib, base_gib = predict_rendered_budget({}, "stub.gguf")
    expected = 1100 * 32.0 / 8.0 / 2 ** 30
    assert pred_gib == pytest.approx(expected, rel=1e-9)
    assert pred_gib > base_gib  # 32-bit-everywhere is bigger than the BF16 baseline


def test_type_bpw_matches_fork_type_facts():
    from magicquant.quant.ggml_facts import FORK_TYPES
    fact = FORK_TYPES["Q4_0_ROCMFP4"]
    assert _type_bpw("Q4_0_ROCMFP4") == pytest.approx(fact["size"] * 8.0 / fact["block"])


# ── _record_refusal: additive rule/requested_budget_gib kwargs ─────────────

def test_record_refusal_omits_additive_fields_by_default(tmp_path):
    """Old (band-guard) callers that don't pass rule/requested_budget_gib get
    the EXACT original record shape -- tests/test_rocmfpx_refusal_record.py
    pins this with a strict dict-equality check, so these new kwargs must be
    additive, never always-present."""
    _record_refusal(
        tmp_path, tier="Q5", family="rocmfpx", reason="r",
        predicted_gib=1.0, baseline_gib=2.0,
        predicted_band="Q6", claimed_band="Q5",
    )
    rec = json.loads((tmp_path / "_refusals.json").read_text())[0]
    assert "rule" not in rec
    assert "requested_budget_gib" not in rec


def test_record_refusal_budget_rule_adds_requested_budget_gib(tmp_path):
    _record_refusal(
        tmp_path, tier="BUDGET-10GiB", family="rocmfpx", reason="r",
        predicted_gib=15.0, baseline_gib=50.0,
        predicted_band="", claimed_band="",
        rule="budget", requested_budget_gib=10.0,
    )
    rec = json.loads((tmp_path / "_refusals.json").read_text())[0]
    assert rec["rule"] == "budget"
    assert rec["requested_budget_gib"] == pytest.approx(10.0)


# ── _quantize_mq_budget: end-to-end guard + success paths ──────────────────

def _write_budget_block(out_dir, key, *, config, tensor_config, budget_gib):
    magicquant_dir = out_dir / "magicquant"
    magicquant_dir.mkdir(parents=True, exist_ok=True)
    (magicquant_dir / "search_results.json").write_text(json.dumps({
        "tier_scheme_version": 2,
        "tiered": {key: {
            "config": config,
            "tensor_config": tensor_config,
            "budget_bytes": budget_gib * 1024 ** 3,
        }},
    }))


def test_quantize_mq_budget_refuses_over_budget_and_records_it(tmp_path, monkeypatch, capsys):
    out_dir = tmp_path / "output"
    rocmfpx_out_dir = out_dir / "rocmfpx"
    key = "BUDGET-10GiB"
    _write_budget_block(
        out_dir, key,
        config={"U": "MXFP4_MOE"},
        tensor_config={"blk.0.ffn_up.weight": "MXFP4_MOE"},
        budget_gib=10,
    )

    monkeypatch.setattr(entry, "predict_rendered_budget",
                        lambda tensor_config, bf16_gguf: (15.0, 50.0))

    result = _quantize_mq_budget(
        spec="mq-budget", requested="BUDGET", out_dir=out_dir,
        rocmfpx_out_dir=rocmfpx_out_dir, model_name="stub-model",
        quantize_bin=Path("/nonexistent/llama-quantize"),
        bf16_gguf="stub.gguf", imatrix="",
    )

    assert result is None
    out = capsys.readouterr().out
    assert "Refusing mq-budget" in out

    records = json.loads((rocmfpx_out_dir / "_refusals.json").read_text())
    assert len(records) == 1
    rec = records[0]
    assert rec["tier"] == key
    assert rec["family"] == "rocmfpx"
    assert rec["rule"] == "budget"
    assert rec["requested_budget_gib"] == pytest.approx(10.0)
    assert rec["predicted_gib"] == pytest.approx(15.0)
    assert rec["baseline_gib"] == pytest.approx(50.0)


def test_quantize_mq_budget_within_tolerance_does_not_refuse(tmp_path, monkeypatch):
    """BUDGET_TOLERANCE (2%) is shared with publish -- a build just inside it
    must not be refused."""
    out_dir = tmp_path / "output"
    rocmfpx_out_dir = out_dir / "rocmfpx"
    key = "BUDGET-10GiB"
    _write_budget_block(
        out_dir, key,
        config={"U": "MXFP4_MOE"},
        tensor_config={"blk.0.ffn_up.weight": "MXFP4_MOE"},
        budget_gib=10,
    )
    just_inside = 10 * (1 + BUDGET_TOLERANCE) - 0.001
    monkeypatch.setattr(entry, "predict_rendered_budget",
                        lambda tensor_config, bf16_gguf: (just_inside, 50.0))
    monkeypatch.setattr(entry, "validate_types_supported", lambda t, b: None)

    built = rocmfpx_out_dir / f"stub-model-ROCMFPX-MQ-{key}.gguf"

    def _fake_run(cmd, *a, **kw):
        built.write_bytes(b"gguf")
        return types.SimpleNamespace(returncode=0)

    monkeypatch.setattr("subprocess.run", _fake_run)

    result = _quantize_mq_budget(
        spec="mq-budget", requested="BUDGET", out_dir=out_dir,
        rocmfpx_out_dir=rocmfpx_out_dir, model_name="stub-model",
        quantize_bin=Path("/nonexistent/llama-quantize"),
        bf16_gguf="stub.gguf", imatrix="",
    )
    assert result == built
    assert not (rocmfpx_out_dir / "_refusals.json").exists()


def test_quantize_mq_budget_just_above_tolerance_refuses(tmp_path, monkeypatch, capsys):
    """Mirror of the just-inside case above, from the other side of the
    boundary: a prediction just ABOVE budget_gib * (1 + BUDGET_TOLERANCE)
    must still refuse. Pins the tolerance itself -- a threshold silently
    loosened (e.g. multiplied by 10) would let this ship green."""
    out_dir = tmp_path / "output"
    rocmfpx_out_dir = out_dir / "rocmfpx"
    key = "BUDGET-10GiB"
    _write_budget_block(
        out_dir, key,
        config={"U": "MXFP4_MOE"},
        tensor_config={"blk.0.ffn_up.weight": "MXFP4_MOE"},
        budget_gib=10,
    )
    just_above = 10 * (1 + BUDGET_TOLERANCE) + 0.001
    monkeypatch.setattr(entry, "predict_rendered_budget",
                        lambda tensor_config, bf16_gguf: (just_above, 50.0))

    result = _quantize_mq_budget(
        spec="mq-budget", requested="BUDGET", out_dir=out_dir,
        rocmfpx_out_dir=rocmfpx_out_dir, model_name="stub-model",
        quantize_bin=Path("/nonexistent/llama-quantize"),
        bf16_gguf="stub.gguf", imatrix="",
    )
    assert result is None
    out = capsys.readouterr().out
    assert "Refusing mq-budget" in out
    records = json.loads((rocmfpx_out_dir / "_refusals.json").read_text())
    assert records[0]["predicted_gib"] == pytest.approx(just_above)


def test_quantize_mq_budget_exact_boundary_ships_not_refuses(tmp_path, monkeypatch):
    """Pins the comparison operator itself: at EXACT equality with
    budget_gib * (1 + BUDGET_TOLERANCE) the build must ship, i.e. the guard
    is strictly '>', not '>='. A silent '>' -> '>=' flip would refuse a
    build that is, by the tolerance's own definition, still inside it."""
    out_dir = tmp_path / "output"
    rocmfpx_out_dir = out_dir / "rocmfpx"
    key = "BUDGET-10GiB"
    _write_budget_block(
        out_dir, key,
        config={"U": "MXFP4_MOE"},
        tensor_config={"blk.0.ffn_up.weight": "MXFP4_MOE"},
        budget_gib=10,
    )
    exact_boundary = 10 * (1 + BUDGET_TOLERANCE)
    monkeypatch.setattr(entry, "predict_rendered_budget",
                        lambda tensor_config, bf16_gguf: (exact_boundary, 50.0))
    monkeypatch.setattr(entry, "validate_types_supported", lambda t, b: None)

    built = rocmfpx_out_dir / f"stub-model-ROCMFPX-MQ-{key}.gguf"

    def _fake_run(cmd, *a, **kw):
        built.write_bytes(b"gguf")
        return types.SimpleNamespace(returncode=0)

    monkeypatch.setattr("subprocess.run", _fake_run)

    result = _quantize_mq_budget(
        spec="mq-budget", requested="BUDGET", out_dir=out_dir,
        rocmfpx_out_dir=rocmfpx_out_dir, model_name="stub-model",
        quantize_bin=Path("/nonexistent/llama-quantize"),
        bf16_gguf="stub.gguf", imatrix="",
    )
    assert result == built
    assert not (rocmfpx_out_dir / "_refusals.json").exists()


def test_quantize_mq_budget_success_clears_a_stale_refusal(tmp_path, monkeypatch):
    out_dir = tmp_path / "output"
    rocmfpx_out_dir = out_dir / "rocmfpx"
    key = "BUDGET-10GiB"
    _write_budget_block(
        out_dir, key,
        config={"U": "MXFP4_MOE"},
        tensor_config={"blk.0.ffn_up.weight": "MXFP4_MOE"},
        budget_gib=10,
    )
    _record_refusal(
        rocmfpx_out_dir, tier=key, family="rocmfpx", reason="stale",
        predicted_gib=15.0, baseline_gib=50.0,
        predicted_band="", claimed_band="",
        rule="budget", requested_budget_gib=10.0,
    )

    monkeypatch.setattr(entry, "predict_rendered_budget",
                        lambda tensor_config, bf16_gguf: (9.5, 50.0))
    monkeypatch.setattr(entry, "validate_types_supported", lambda t, b: None)

    built = rocmfpx_out_dir / f"stub-model-ROCMFPX-MQ-{key}.gguf"

    def _fake_run(cmd, *a, **kw):
        built.write_bytes(b"gguf")
        return types.SimpleNamespace(returncode=0)

    monkeypatch.setattr("subprocess.run", _fake_run)

    result = _quantize_mq_budget(
        spec="mq-budget", requested="BUDGET", out_dir=out_dir,
        rocmfpx_out_dir=rocmfpx_out_dir, model_name="stub-model",
        quantize_bin=Path("/nonexistent/llama-quantize"),
        bf16_gguf="stub.gguf", imatrix="",
    )
    assert result == built
    assert built.exists()
    assert not (rocmfpx_out_dir / "_refusals.json").exists()


def test_quantize_mq_budget_output_name_bakes_in_the_key(tmp_path, monkeypatch):
    out_dir = tmp_path / "output"
    rocmfpx_out_dir = out_dir / "rocmfpx"
    key = "BUDGET-12.5GiB"
    _write_budget_block(
        out_dir, key,
        config={"U": "MXFP4_MOE"},
        tensor_config={"blk.0.ffn_up.weight": "MXFP4_MOE"},
        budget_gib=12.5,
    )
    monkeypatch.setattr(entry, "predict_rendered_budget",
                        lambda tensor_config, bf16_gguf: (12.0, 50.0))
    monkeypatch.setattr(entry, "validate_types_supported", lambda t, b: None)
    # llama-quantize is stubbed to a no-op, so fabricate the file it would
    # have produced to exercise the existence check.
    rocmfpx_out_dir.mkdir(parents=True, exist_ok=True)
    expected = rocmfpx_out_dir / f"stub-model-ROCMFPX-MQ-{key}.gguf"

    def _fake_run(cmd, *a, **kw):
        expected.write_bytes(b"gguf")
        return types.SimpleNamespace(returncode=0)

    monkeypatch.setattr("subprocess.run", _fake_run)

    result = _quantize_mq_budget(
        spec="mq-budget=BUDGET-12.5GiB", requested="BUDGET-12.5GiB", out_dir=out_dir,
        rocmfpx_out_dir=rocmfpx_out_dir, model_name="stub-model",
        quantize_bin=Path("/nonexistent/llama-quantize"),
        bf16_gguf="stub.gguf", imatrix="",
    )
    assert result == expected


def test_quantize_mq_budget_falls_back_to_config_when_no_tensor_config(tmp_path, monkeypatch):
    """Prefer tensor_config; fall back to the per-group config (existing
    build_tensor_type_lines) when a block has none."""
    out_dir = tmp_path / "output"
    rocmfpx_out_dir = out_dir / "rocmfpx"
    magicquant_dir = out_dir / "magicquant"
    magicquant_dir.mkdir(parents=True)
    key = "BUDGET-10GiB"
    (magicquant_dir / "search_results.json").write_text(json.dumps({
        "tier_scheme_version": 2,
        "tiered": {key: {
            "config": {"U": "MXFP4_MOE"},
            "tensor_config": {},
            "budget_bytes": 10 * 1024 ** 3,
        }},
    }))
    monkeypatch.setattr(entry, "predict_rendered_tier",
                        lambda config, bf16_gguf: (9.0, 50.0, "Q4"))
    monkeypatch.setattr(entry, "validate_types_supported", lambda t, b: None)
    built = rocmfpx_out_dir / f"stub-model-ROCMFPX-MQ-{key}.gguf"

    def _fake_run(cmd, *a, **kw):
        built.write_bytes(b"gguf")
        return types.SimpleNamespace(returncode=0)

    monkeypatch.setattr("subprocess.run", _fake_run)

    result = _quantize_mq_budget(
        spec="mq-budget", requested="BUDGET", out_dir=out_dir,
        rocmfpx_out_dir=rocmfpx_out_dir, model_name="stub-model",
        quantize_bin=Path("/nonexistent/llama-quantize"),
        bf16_gguf="stub.gguf", imatrix="",
    )
    assert result == built


def test_quantize_mq_budget_validates_union_of_config_and_tensor_config_types(tmp_path, monkeypatch):
    """A scheme appearing only in tensor_config must still reach
    validate_types_supported -- not just the per-group config's schemes."""
    out_dir = tmp_path / "output"
    rocmfpx_out_dir = out_dir / "rocmfpx"
    key = "BUDGET-10GiB"
    _write_budget_block(
        out_dir, key,
        config={"U": "MXFP4_MOE"},
        tensor_config={"blk.0.ffn_up.weight": "MXFP4_MOE",
                       "blk.0.attn_q.weight": "Q6_K"},  # scheme only here
        budget_gib=10,
    )
    monkeypatch.setattr(entry, "predict_rendered_budget",
                        lambda tensor_config, bf16_gguf: (9.0, 50.0))

    captured = {}

    def _capture(required_types, qbin):
        captured["required_types"] = set(required_types)

    monkeypatch.setattr(entry, "validate_types_supported", _capture)
    monkeypatch.setattr(
        "subprocess.run",
        lambda cmd, *a, **kw: types.SimpleNamespace(returncode=1),  # fail fast
    )

    _quantize_mq_budget(
        spec="mq-budget", requested="BUDGET", out_dir=out_dir,
        rocmfpx_out_dir=rocmfpx_out_dir, model_name="stub-model",
        quantize_bin=Path("/nonexistent/llama-quantize"),
        bf16_gguf="stub.gguf", imatrix="",
    )
    assert translate_scheme("Q6_K") in captured["required_types"]
    assert translate_scheme("MXFP4_MOE") in captured["required_types"]


def test_quantize_mq_budget_prediction_failure_is_advisory_not_fatal(tmp_path, monkeypatch):
    """Mirrors _quantize_mq_hybrid: if the size prediction itself blows up,
    that's advisory (printed), not a build-blocking error."""
    out_dir = tmp_path / "output"
    rocmfpx_out_dir = out_dir / "rocmfpx"
    key = "BUDGET-10GiB"
    _write_budget_block(
        out_dir, key,
        config={"U": "MXFP4_MOE"},
        tensor_config={"blk.0.ffn_up.weight": "MXFP4_MOE"},
        budget_gib=10,
    )
    monkeypatch.setattr(entry, "predict_rendered_budget",
                        lambda tensor_config, bf16_gguf: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(entry, "validate_types_supported", lambda t, b: None)
    built = rocmfpx_out_dir / f"stub-model-ROCMFPX-MQ-{key}.gguf"

    def _fake_run(cmd, *a, **kw):
        built.write_bytes(b"gguf")
        return types.SimpleNamespace(returncode=0)

    monkeypatch.setattr("subprocess.run", _fake_run)

    result = _quantize_mq_budget(
        spec="mq-budget", requested="BUDGET", out_dir=out_dir,
        rocmfpx_out_dir=rocmfpx_out_dir, model_name="stub-model",
        quantize_bin=Path("/nonexistent/llama-quantize"),
        bf16_gguf="stub.gguf", imatrix="",
    )
    assert result == built


# ── run() dispatch: BUDGET/BUDGET-* specs route to the budget path ─────────

def test_run_dispatch_routes_budget_specs_to_quantize_mq_budget(tmp_path, monkeypatch):
    """A bare mq-budget spec (tier == 'BUDGET' from parse_mq_spec) must be
    dispatched to the budget path, not the per-group hybrid path -- a
    per-tensor allocation cannot be read as a per-group config."""
    called = {}

    def _fake_budget(spec, requested, out_dir, rocmfpx_out_dir, model_name,
                     quantize_bin, bf16_gguf, imatrix, allow_requantize=False):
        called["requested"] = requested
        return Path("/fake/out.gguf")

    def _fake_hybrid(*a, **kw):
        called["hybrid_called"] = True
        return None

    monkeypatch.setattr(entry, "_quantize_mq_budget", _fake_budget)
    monkeypatch.setattr(entry, "_quantize_mq_hybrid", _fake_hybrid)
    monkeypatch.setattr(entry, "ensure_rocmfpx", lambda hint="": str(tmp_path))
    monkeypatch.setattr(entry, "resolve_source", lambda *a, **kw: str(tmp_path / "model.gguf"))
    monkeypatch.setattr(entry, "_ensure_bf16_gguf", lambda *a, **kw: "stub.gguf")
    monkeypatch.setattr(entry.ppl_smoke, "find_perplexity_bin", lambda *a, **kw: None)
    monkeypatch.setattr(entry.ppl_smoke, "smoke_test_gguf", lambda *a, **kw: True)

    out_dir = tmp_path / "out"
    out_dir.mkdir()
    cfg_path = tmp_path / "cfg.json"
    cfg_path.write_text(json.dumps({
        "pipeline_root": str(tmp_path), "pipeline_root_str": str(tmp_path),
        "out_abs_str": str(out_dir), "formats_json": json.dumps(["mq-budget"]),
        "model_name": "stub-model",
    }))

    entry.run(str(cfg_path))
    assert called.get("requested") == "BUDGET"
    assert "hybrid_called" not in called
