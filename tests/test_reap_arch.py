"""L-reap-archlist / L-source-dup: REAP arch list uses class names only and is
shared by CLI and UI.
"""

import logging
import re
import types

import reap_common


CAUSAL_LM_RE = re.compile(r"^[A-Za-z0-9_]+ForCausalLM$")


def test_every_entry_looks_like_a_causallm_class_name():
    """_detect_model_arch returns architectures[0] (a class name). Every entry
    must therefore look like a *ForCausalLM class, not an HF repo-id."""
    for arch in reap_common.REAP_SUPPORTED_ARCHS:
        assert CAUSAL_LM_RE.match(arch), f"{arch!r} is not a CausalLM class name"


def test_repo_id_entries_are_gone():
    bad = {"Qwen3-Coder-30B-A3B-Instruct", "gpt-oss-20b"}
    assert not (bad & set(reap_common.REAP_SUPPORTED_ARCHS))


def test_gpt_oss_class_name_present():
    assert "GptOssForCausalLM" in reap_common.REAP_SUPPORTED_ARCHS


def test_cli_and_ui_share_one_object():
    """The CLI (pipeline) and UI (app) must reference the same shared set."""
    import pipeline
    import app as ui_app
    assert pipeline.REAP_SUPPORTED_ARCHS is reap_common.REAP_SUPPORTED_ARCHS
    assert ui_app.REAP_SUPPORTED_ARCHS is reap_common.REAP_SUPPORTED_ARCHS


def test_detect_model_arch_reads_config(tmp_path):
    (tmp_path / "config.json").write_text('{"architectures": ["Qwen3MoeForCausalLM"]}')
    assert reap_common.detect_model_arch(tmp_path) == "Qwen3MoeForCausalLM"


def test_detect_model_arch_missing_config(tmp_path):
    assert reap_common.detect_model_arch(tmp_path) is None


# ---------------------------------------------------------------------------
# R2: upstream drift check (_reap_supported_archs_diff /
# warn_if_reap_supported_archs_stale). REAP_SUPPORTED_ARCHS stays a
# hand-curated policy literal -- these test the *comparison* logic against
# a fake reap.model_util-shaped module, not any real reap install.
# ---------------------------------------------------------------------------

def _fake_model_util(model_attrs):
    return types.SimpleNamespace(MODEL_ATTRS=model_attrs)


def test_diff_none_when_module_has_no_model_attrs():
    fake = types.SimpleNamespace()  # no MODEL_ATTRS attribute at all
    assert reap_common._reap_supported_archs_diff(fake) is None


def test_diff_empty_when_upstream_matches_literal_exactly():
    fake = _fake_model_util({name: {} for name in reap_common.REAP_SUPPORTED_ARCHS})
    diff = reap_common._reap_supported_archs_diff(fake)
    assert diff == {"missing_from_literal": [], "extra_in_literal": []}


def test_diff_reports_identifier_shaped_keys_missing_from_literal():
    fake = _fake_model_util({"BrandNewArchForCausalLM": {}})
    diff = reap_common._reap_supported_archs_diff(fake)
    assert diff["missing_from_literal"] == ["BrandNewArchForCausalLM"]


def test_diff_ignores_repo_id_shaped_keys_as_missing():
    """Non-identifier keys (repo-id strings, e.g. upstream's real-world
    'gpt-oss-20b' / 'Qwen3-Coder-30B-A3B-Instruct' quirk) can never equal
    detect_model_arch()'s output and must not be flagged as missing."""
    fake = _fake_model_util({"gpt-oss-20b": {}, "Qwen3-Coder-30B-A3B-Instruct": {}})
    diff = reap_common._reap_supported_archs_diff(fake)
    assert diff["missing_from_literal"] == []


def test_diff_reports_literal_entries_absent_upstream():
    # An upstream registry with none of the literal's entries -- everything
    # in REAP_SUPPORTED_ARCHS should come back as "extra".
    fake = _fake_model_util({})
    diff = reap_common._reap_supported_archs_diff(fake)
    assert set(diff["extra_in_literal"]) == set(reap_common.REAP_SUPPORTED_ARCHS)


def test_warn_logs_on_mismatch(caplog):
    fake = _fake_model_util({"BrandNewArchForCausalLM": {}})
    with caplog.at_level(logging.WARNING):
        reap_common.warn_if_reap_supported_archs_stale(fake)
    assert any("BrandNewArchForCausalLM" in rec.message for rec in caplog.records)
    assert any("REAP_SUPPORTED_ARCHS" in rec.message for rec in caplog.records)


def test_warn_does_not_log_when_upstream_matches(caplog):
    fake = _fake_model_util({name: {} for name in reap_common.REAP_SUPPORTED_ARCHS})
    with caplog.at_level(logging.WARNING):
        reap_common.warn_if_reap_supported_archs_stale(fake)
    assert caplog.records == []


def test_warn_is_a_silent_noop_when_reap_not_importable(caplog, monkeypatch):
    """Default argument path: reap is not installed in this venv (verified
    separately), so the derive path must not raise or log -- REAP_SUPPORTED_
    ARCHS stays the documented literal fallback."""
    with caplog.at_level(logging.WARNING):
        reap_common.warn_if_reap_supported_archs_stale()  # must not raise
    assert caplog.records == []


def test_literal_stays_authoritative_even_when_upstream_disagrees():
    """REAP_SUPPORTED_ARCHS itself must never be mutated/replaced by the
    diff/warn machinery -- it is Foundry policy, upstream is only consulted
    for the drift warning."""
    before = reap_common.REAP_SUPPORTED_ARCHS
    fake = _fake_model_util({"SomethingElseForCausalLM": {}})
    reap_common.warn_if_reap_supported_archs_stale(fake)
    assert reap_common.REAP_SUPPORTED_ARCHS is before
