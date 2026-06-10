"""L-reap-path-hardcoded / L-source-dup: ReapService emits a configurable src
path and uses the shared stub block.
"""

from pathlib import Path

import reap_common
from services import ReapService


def _build(**over):
    svc = ReapService(Path("/repo"), "python")
    kwargs = dict(
        input_dir="/in", output_dir="/out", cwd_dir="/cwd",
        compression_ratio=0.25, prune_method="reap", samples_per_category=512,
        model_max_length=2048, dataset_name="ds", seed=42,
    )
    kwargs.update(over)
    return svc.build_script(**kwargs)


def test_reap_script_compiles():
    compile(_build(), "<reap>", "exec")


def test_reap_shim_invokes_entry_module():
    """Audit H2: the REAP stage body moved into core/_reap_entry.py; the shim
    just writes config + calls it."""
    script = _build()
    assert "import _reap_entry" in script
    assert "_reap_entry.run(" in script


def test_reap_src_path_is_resolved_at_runtime(monkeypatch):
    """The configurable REAP src path is now applied by install_reap_stubs() at
    runtime (not embedded in the generated script). Default + override both work."""
    assert reap_common.reap_src_path() == reap_common.DEFAULT_REAP_SRC
    monkeypatch.setenv("FOUNDRY_REAP_SRC", "/custom/reap/src")
    assert reap_common.reap_src_path() == "/custom/reap/src"
    # reap_stub_block (kept for back-compat) still honors the override.
    block = reap_common.reap_stub_block()
    assert "/custom/reap/src" in block


def test_install_reap_stubs_uses_configured_path(monkeypatch):
    """install_reap_stubs (used by core/_reap_entry.py) inserts the configured
    src path onto sys.path and stubs the heavy deps."""
    import sys
    monkeypatch.setenv("FOUNDRY_REAP_SRC", "/custom/reap/src")
    # Snapshot + restore sys.path and stubbed modules so we don't leak state.
    orig_path = list(sys.path)
    orig_modules = dict(sys.modules)
    try:
        reap_common.install_reap_stubs()
        assert "/custom/reap/src" in sys.path
        for mod in ("vllm", "lm_eval", "evalplus", "deepspeed", "wandb"):
            assert mod in sys.modules
    finally:
        sys.path[:] = orig_path
        for k in list(sys.modules):
            if k not in orig_modules:
                del sys.modules[k]


def test_stub_block_lists_heavy_deps():
    block = reap_common.reap_stub_block("/x")
    for mod in ("vllm", "lm_eval", "evalplus", "deepspeed", "wandb"):
        assert mod in block


def test_reap_config_carries_args_verbatim():
    """Args flow through a JSON config (json.dumps + repr in the shim), so the
    shim compiles and the built config preserves the value verbatim."""
    svc = ReapService(Path("/repo"), "python")
    cfg = svc.build_config(
        input_dir="/in", output_dir="/out", cwd_dir="/cwd",
        compression_ratio=0.25, prune_method="reap", samples_per_category=512,
        model_max_length=2048, dataset_name="weird'; rm -rf /", seed=42,
    )
    assert cfg["dataset_name"] == "weird'; rm -rf /"
    script = _build(dataset_name="weird'; rm -rf /")
    compile(script, "<reap>", "exec")


def test_reap_build_argv():
    """Audit H2: argv construction is now a pure, importable function."""
    import _reap_entry
    cfg = ReapService(Path("/repo"), "python").build_config(
        input_dir="/in", output_dir="/out", cwd_dir="/cwd",
        compression_ratio=0.25, prune_method="reap", samples_per_category=512,
        model_max_length=2048, dataset_name="ds", seed=42,
    )
    argv = _reap_entry.build_argv(cfg)
    assert argv[0] == "reap-prune"
    assert "--model-name" in argv and "/in" in argv
    assert "--compression-ratio" in argv and "0.25" in argv
    assert "--dataset-name" in argv and "ds" in argv
