"""Unit tests for core/onnx_quark.py — argv construction and path resolution.

We mock subprocess.run because actually invoking Quark or the OGA builder
would require GPU + multi-GB downloads. End-to-end is covered in
tests/test_onnx_stage.py.
"""

import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "core"))

from onnx_quark import (
    build_quark_argv,
    build_oga_builder_argv,
    find_quantize_quark_script,
)


def test_quark_argv_uses_uint4_wo_128_awq_by_default():
    argv = build_quark_argv(
        script_path="/tmp/quantize_quark.py",
        model_dir="/srv/merged",
        output_dir="/srv/quark",
        quant_scheme="uint4_wo_128",
        quant_algo="awq",
        data_type="float16",
        num_calib_data=128,
        seq_len=512,
        calib_dataset="pileval_for_awq_benchmark",
    )
    assert argv[0] == sys.executable
    assert "/tmp/quantize_quark.py" in argv
    assert "--model_dir" in argv and "/srv/merged" in argv
    assert "--output_dir" in argv and "/srv/quark" in argv
    assert "--quant_scheme" in argv and "uint4_wo_128" in argv
    assert "--quant_algo" in argv and "awq" in argv
    assert "--data_type" in argv and "float16" in argv
    assert "--num_calib_data" in argv and "128" in argv
    assert "--seq_len" in argv and "512" in argv
    assert "--dataset" in argv and "pileval_for_awq_benchmark" in argv
    assert "--model_export" in argv and "hf_format" in argv
    assert "--custom_mode" in argv and "awq" in argv


def test_oga_builder_argv_hybrid_dml():
    argv = build_oga_builder_argv(
        input_dir="/srv/quark",
        output_dir="/srv/onnx_model",
        precision="int4",
        execution_provider="dml",
    )
    assert argv[:3] == [sys.executable, "-m", "onnxruntime_genai.models.builder"]
    assert "-i" in argv and "/srv/quark" in argv
    assert "-o" in argv and "/srv/onnx_model" in argv
    assert "-p" in argv and "int4" in argv
    assert "-e" in argv and "dml" in argv


def test_find_quantize_quark_script_returns_none_when_repo_missing(tmp_path, monkeypatch):
    """If QUARK_HOME points at a directory that doesn't have the script, return None."""
    import onnx_quark
    monkeypatch.setattr(onnx_quark, "QUARK_HOME", tmp_path / "quark-amd-nope")
    assert onnx_quark.find_quantize_quark_script() is None


def test_find_quantize_quark_script_returns_path_when_present(tmp_path, monkeypatch):
    """If QUARK_HOME contains the expected script, return its path."""
    import onnx_quark
    fake_home = tmp_path / "quark-amd"
    script = fake_home / "examples/torch/language_modeling/llm_ptq/quantize_quark.py"
    script.parent.mkdir(parents=True)
    script.write_text("# fake")

    monkeypatch.setattr(onnx_quark, "QUARK_HOME", fake_home)
    result = onnx_quark.find_quantize_quark_script()
    assert result == script
