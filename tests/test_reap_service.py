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


# --- observer_only (opt-in, default False) -----------------------------------
# Enables reap.prune's own --run_observer_only: stop after the calibration
# pass, skip pruning/save/eval. Needed for huge MoE models (e.g. Laguna-S)
# where the real pruned checkpoint gets produced out-of-band by streaming
# safetensors surgery instead of an in-memory prune()+save_pretrained().


def test_build_config_observer_only_defaults_false():
    """Opt-in: existing callers that never pass observer_only are unaffected."""
    svc = ReapService(Path("/repo"), "python")
    cfg = svc.build_config(
        input_dir="/in", output_dir="/out", cwd_dir="/cwd",
        compression_ratio=0.25, prune_method="reap", samples_per_category=512,
        model_max_length=2048, dataset_name="ds", seed=42,
    )
    assert cfg["observer_only"] is False


def test_build_config_observer_only_true_flows_through():
    svc = ReapService(Path("/repo"), "python")
    cfg = svc.build_config(
        input_dir="/in", output_dir="/out", cwd_dir="/cwd",
        compression_ratio=0.25, prune_method="reap", samples_per_category=512,
        model_max_length=2048, dataset_name="ds", seed=42,
        observer_only=True,
    )
    assert cfg["observer_only"] is True


def test_build_argv_observer_only_false_by_default():
    import _reap_entry
    cfg = ReapService(Path("/repo"), "python").build_config(
        input_dir="/in", output_dir="/out", cwd_dir="/cwd",
        compression_ratio=0.25, prune_method="reap", samples_per_category=512,
        model_max_length=2048, dataset_name="ds", seed=42,
    )
    argv = _reap_entry.build_argv(cfg)
    assert "--run_observer_only" in argv
    i = argv.index("--run_observer_only")
    assert argv[i + 1] == "false"


def test_build_argv_observer_only_true_maps_to_reap_flag():
    """Maps onto reap.prune's ReapArgs.run_observer_only, not a Foundry-invented
    flag name -- must match what reap.prune's HfArgumentParser actually reads."""
    import _reap_entry
    cfg = ReapService(Path("/repo"), "python").build_config(
        input_dir="/in", output_dir="/out", cwd_dir="/cwd",
        compression_ratio=0.25, prune_method="reap", samples_per_category=512,
        model_max_length=2048, dataset_name="ds", seed=42,
        observer_only=True,
    )
    argv = _reap_entry.build_argv(cfg)
    i = argv.index("--run_observer_only")
    assert argv[i + 1] == "true"


def test_build_config_observer_only_is_json_serializable():
    svc = ReapService(Path("/repo"), "python")
    cfg = svc.build_config(
        input_dir="/in", output_dir="/out", cwd_dir="/cwd",
        compression_ratio=0.25, prune_method="reap", samples_per_category=512,
        model_max_length=2048, dataset_name="ds", seed=42,
        observer_only=True,
    )
    import json
    assert json.loads(json.dumps(cfg))["observer_only"] is True


def test_observer_only_script_still_compiles():
    """observer_only threads through build_script (JSON-embedded in the shim)
    without breaking shim generation."""
    script = _build(observer_only=True)
    compile(script, "<reap>", "exec")


def test_run_observer_only_relocates_observations_not_pruned_model(tmp_path):
    """reap.prune.main() returns right after the observer pass when
    --run_observer_only is set -- it never writes a pruned_models/ directory.
    run() must relocate the saved observations_*.pt file(s) instead of
    crashing while searching for a pruned model that was never produced."""
    import sys
    import types
    import json as json_mod

    import _reap_entry

    cwd_dir = tmp_path / "cwd"
    reap_dir = tmp_path / "out" / "reap_model"
    cwd_dir.mkdir(parents=True)

    cfg = {
        "pipeline_root": "/repo",
        "input_dir": "/in",
        "output_dir": str(reap_dir),
        "cwd_dir": str(cwd_dir),
        "compression_ratio": 0.25,
        "prune_method": "reap",
        "samples_per_category": 4,
        "model_max_length": 128,
        "dataset_name": "ds",
        "seed": 42,
        "observer_only": True,
    }
    cfg_path = tmp_path / "cfg.json"
    cfg_path.write_text(json_mod.dumps(cfg))

    # Fake reap.prune.main(): simulate reap.prune writing an observation file
    # under its relative artifacts/ tree (mirrors
    # reap.main.record_activations()'s real category_dir / output_file_name
    # layout) and then returning early, exactly like the real
    # --run_observer_only path does.
    def _fake_main():
        obs_dir = Path.cwd() / "artifacts" / "m" / "ds" / "all"
        obs_dir.mkdir(parents=True, exist_ok=True)
        (obs_dir / "observations_1024_cosine.pt").write_bytes(b"fake-observer-state")

    fake_reap = types.ModuleType("reap")
    fake_reap_prune = types.ModuleType("reap.prune")
    fake_reap_prune.main = _fake_main
    fake_reap.prune = fake_reap_prune

    # install_reap_stubs() also runs for real (it's orthogonal: stubs
    # third-party deps + appends the REAP src dir to sys.path). Snapshot +
    # restore sys.path/sys.modules by hand (not via monkeypatch.setitem,
    # which would fight this same-key restore) exactly like
    # test_install_reap_stubs_uses_configured_path does above, so this test
    # doesn't leak reap-importability into whatever test runs next.
    orig_path = list(sys.path)
    orig_modules = dict(sys.modules)
    orig_cwd = Path.cwd()
    sys.modules["reap"] = fake_reap
    sys.modules["reap.prune"] = fake_reap_prune
    try:
        _reap_entry.run(str(cfg_path))
    finally:
        import os
        os.chdir(orig_cwd)
        sys.path[:] = orig_path
        sys.modules.clear()
        sys.modules.update(orig_modules)

    # reap.main.create_results_directory() nests results under
    # artifacts/<model_clean>/<dataset_clean>/ (here "m"/"ds", matching the
    # fake main()'s layout) -- the relocation preserves that path relative to
    # artifacts_root, same as the pruned-model branch preserves pruned_models/.
    moved = reap_dir / "m" / "ds" / "all" / "observations_1024_cosine.pt"
    assert moved.exists(), f"expected relocated observation file at {moved}"
    assert moved.read_bytes() == b"fake-observer-state"
    assert not (cwd_dir / "artifacts").exists(), "artifacts/ should be cleaned up"
