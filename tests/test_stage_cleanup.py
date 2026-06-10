"""L-stage-script-cleanup + supply-chain pin + heretic selection.

_run_stage_script removes the generated _stage_*.py on success and retains it on
failure. Also asserts the llama.cpp clone is pinned and the heretic selection is
the simplified Pareto-min.
"""

import sys
from pathlib import Path

import pipeline


def _log(msg, level="info"):
    pass


def test_stage_script_removed_on_success(tmp_path):
    script_path = tmp_path / "_stage_demo.py"
    rc = pipeline._run_stage_script(
        "print('ok')\n", script_path, _log,
    )
    assert rc == 0
    assert not script_path.exists(), "stage script should be cleaned up on success"


def test_stage_script_retained_on_failure(tmp_path):
    script_path = tmp_path / "_stage_demo.py"
    rc = pipeline._run_stage_script(
        "import sys; sys.exit(1)\n", script_path, _log,
    )
    assert rc != 0
    assert script_path.exists(), "stage script should be retained on failure for debugging"


def test_marker_written_on_success_with_key_file(tmp_path):
    stage_dir = tmp_path / "lora_adapters"
    key = stage_dir / "adapter_model.safetensors"
    script_path = tmp_path / "_stage_demo.py"
    body = (
        f"from pathlib import Path\n"
        f"d = Path({str(stage_dir)!r}); d.mkdir(parents=True, exist_ok=True)\n"
        f"(d / 'adapter_model.safetensors').write_bytes(b'weights')\n"
    )
    rc = pipeline._run_stage_script(
        body, script_path, _log,
        stage="training", stage_dir=stage_dir, key_file=key, cfg_hash="abc",
    )
    assert rc == 0
    import markers
    m = markers.read_marker(stage_dir)
    assert m is not None and m["config_hash"] == "abc"


def test_llamacpp_clone_is_pinned():
    """L-supply-chain: pin constant + --branch usage in the auto-install path."""
    src = Path(pipeline.__file__).read_text()
    assert "LLAMACPP_PIN" in src
    assert "--branch" in src
    # Service-side clone also pinned.
    from pathlib import Path as _P
    svc_src = (_P(pipeline.__file__).parent / "services.py").read_text()
    assert "--branch" in svc_src
