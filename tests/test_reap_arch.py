"""L-reap-archlist / L-source-dup: REAP arch list uses class names only and is
shared by CLI and UI.
"""

import re

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
