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


def test_reap_emits_default_src_path():
    script = _build()
    assert reap_common.DEFAULT_REAP_SRC in script


def test_reap_src_path_is_env_overridable(monkeypatch):
    monkeypatch.setenv("FOUNDRY_REAP_SRC", "/custom/reap/src")
    assert reap_common.reap_src_path() == "/custom/reap/src"
    block = reap_common.reap_stub_block()
    assert "/custom/reap/src" in block


def test_stub_block_lists_heavy_deps():
    block = reap_common.reap_stub_block("/x")
    for mod in ("vllm", "lm_eval", "evalplus", "deepspeed", "wandb"):
        assert mod in block


def test_reap_args_repr_escaped():
    script = _build(dataset_name="weird'; rm -rf /")
    compile(script, "<reap>", "exec")
    assert repr("weird'; rm -rf /") in script
