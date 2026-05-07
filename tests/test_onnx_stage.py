"""End-to-end smoke test for the ONNX pipeline stage.

Quantizes a tiny 135M-param model with Quark INT4 AWQ and builds an
OGA-format ONNX directory. Verifies the output structure is a drop-in
for `lemonade pull`. Skipped if amd-quark or onnxruntime-genai are not
installed (they're heavy optional deps).

Runtime budget: <5 minutes on the dev box (Strix Halo). May be
slower in CI; mark accordingly if/when CI gets one.
"""

import json
import shutil
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
TINY_MODEL = "TinyLlama/TinyLlama-1.1B-Chat-v1.0"  # 1.1B params, hidden_size=2048 (div by 128), OGA-supported


@pytest.fixture(scope="module")
def _quark_deps():
    """Skip the module if the heavy optional deps are not installed.

    This fixture is a dependency of tiny_model_dir so the expensive
    model download never starts if Quark/OGA aren't present.
    """
    # Install compat shim before probing so the import doesn't fail on torch nightlies.
    sys.path.insert(0, str(PROJECT_ROOT / "core"))
    import quark_torch_compat
    quark_torch_compat.install()

    pytest.importorskip("quark.torch", reason="amd-quark not installed")
    pytest.importorskip("onnxruntime_genai", reason="onnxruntime-genai not installed")


@pytest.fixture(scope="module")
def tiny_model_dir(tmp_path_factory, _quark_deps):
    """Download SmolLM2-135M-Instruct as merged FP16 safetensors.

    Only runs if _quark_deps didn't skip (i.e. amd-quark + onnxruntime-genai
    are both installed).
    """
    from huggingface_hub import snapshot_download

    target = tmp_path_factory.mktemp("source_model")
    snapshot_download(
        repo_id=TINY_MODEL,
        local_dir=str(target),
        allow_patterns=["*.safetensors", "*.json", "tokenizer*", "*.txt"],
    )
    return target


@pytest.mark.slow
def test_onnx_stage_end_to_end(tiny_model_dir, tmp_path):
    """Run stage_onnx against the tiny model, verify the output layout.

    Requires amd-quark and onnxruntime-genai to be installed; skipped otherwise
    (via the _quark_deps fixture that tiny_model_dir depends on).
    Tagged @pytest.mark.slow because it takes 2-5 minutes and requires GPU + network.
    """
    output_dir = tmp_path / "run"
    output_dir.mkdir()

    from core.pipeline import (
        Artifacts,
        OnnxConfig,
        PipelineConfig,
        stage_onnx,
        _default_log,
    )

    # Place the source where stage_onnx expects to find it (merged_model/).
    merged_dir = output_dir / "merged_model"
    shutil.copytree(tiny_model_dir, merged_dir)

    cfg = PipelineConfig(
        output_dir=str(output_dir),
        onnx=OnnxConfig(
            num_calib_data=8,            # cut for test speed
            seq_len=128,
            cleanup_intermediates=True,
        ),
    )
    artifacts = Artifacts(str(output_dir))

    ok = stage_onnx(cfg, artifacts, _default_log)
    assert ok, "stage_onnx returned False — see stdout for the failure"

    # Output layout assertions.
    onnx_dir = output_dir / "onnx_model"
    assert onnx_dir.exists(), "onnx_model/ directory not created"
    assert (onnx_dir / "model.onnx").exists(), "model.onnx not produced"
    assert (onnx_dir / "genai_config.json").exists(), "genai_config.json missing"

    # genai_config.json should be valid JSON.
    config = json.loads((onnx_dir / "genai_config.json").read_text())
    assert "model" in config, "genai_config.json missing 'model' top-level key"

    # tokenizer_config.json is critical — Lemonade reads it for OGA inference.
    assert (onnx_dir / "tokenizer_config.json").exists(), \
        "tokenizer_config.json missing — Lemonade needs this for OGA inference"

    # cleanup_intermediates=True should have deleted quark_safetensors/.
    assert not (output_dir / "quark_safetensors").exists(), (
        "quark_safetensors/ was not cleaned up despite cleanup_intermediates=True"
    )


def test_stage_onnx_skips_when_artifact_exists(tmp_path):
    """If onnx_model/model.onnx already exists, the stage skips and returns True.

    This test does NOT require amd-quark or onnxruntime-genai — it only exercises
    the early-exit path in stage_onnx and runs fast + offline.
    """
    from core.pipeline import (
        Artifacts,
        OnnxConfig,
        PipelineConfig,
        stage_onnx,
        _default_log,
    )

    output_dir = tmp_path / "run"
    onnx_dir = output_dir / "onnx_model"
    onnx_dir.mkdir(parents=True)
    (onnx_dir / "model.onnx").write_bytes(b"fake")  # pretend we already built it

    cfg = PipelineConfig(output_dir=str(output_dir), onnx=OnnxConfig())
    artifacts = Artifacts(str(output_dir))

    ok = stage_onnx(cfg, artifacts, _default_log)
    assert ok, "skip-on-existing should return True"
