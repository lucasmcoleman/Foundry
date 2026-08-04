"""QAT consumes a budget build through the same load_hybrid_config path as a
tier build. This is the spec's 'QAT consumes v2' requirement: expected to
pass with ZERO code changes (tier is a free string end-to-end). If any of
these fail, that assumption was wrong -- fix the plumbing, don't delete the
test."""
import json

import pytest

qat_config = pytest.importorskip("magicquant.qat.config")


def _results(tmp_path):
    p = tmp_path / "search_results.json"
    p.write_text(json.dumps({
        "tier_scheme_version": 2,
        "tiered": {"BUDGET-12.5GiB": {
            "config": {"U": "MXFP4_MOE", "Q": "Q5_K"},
            "tensor_config": {"blk.0.ffn_up.weight": "MXFP4_MOE"},
            "algo": "v2-budget",
        }}}))
    return p


def test_load_hybrid_config_resolves_budget_key(tmp_path):
    got = qat_config.load_hybrid_config(_results(tmp_path), "BUDGET-12.5GiB")
    # scheme -> ggml type name mapping is applied for every group, not just
    # passed through verbatim: MXFP4_MOE (a MoE-specific scheme identifier)
    # resolves to ggml block type "MXFP4" (confirmed against the live scheme
    # registry, magicquant.quant.schemes.get_scheme_by_name), and Q5_K
    # resolves to itself because its ggml type name equals its scheme name.
    assert got == {"U": "MXFP4", "Q": "Q5_K"}


def test_missing_budget_key_error_lists_available(tmp_path):
    with pytest.raises(KeyError) as e:
        qat_config.load_hybrid_config(_results(tmp_path), "BUDGET-99GiB")
    assert "BUDGET-12.5GiB" in str(e.value)
    assert "BUDGET-99GiB" in str(e.value)
