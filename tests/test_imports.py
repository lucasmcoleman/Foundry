"""H4 / M-entrypoint: offline import smoke tests.

These guard the two install/test breakages: the `foundry` console entrypoint
(core.pipeline:main must exist and be callable) and a clean importable core.
"""

import pytest


def test_pipeline_imports_main_run_and_config():
    from pipeline import main, run_pipeline, PipelineConfig, load_yaml_into_config
    assert callable(main)
    assert callable(run_pipeline)
    assert callable(load_yaml_into_config)


def test_pipeline_stage_functions_import():
    from pipeline import (
        stage_training, stage_export, stage_heretic,
        stage_reap, stage_magicquant, stage_upload,
    )
    for fn in (stage_training, stage_export, stage_heretic,
               stage_reap, stage_magicquant, stage_upload):
        assert callable(fn)


def test_detect_response_template_is_gone():
    """L-dead-detect: the dead symbol must be removed (and not break imports)."""
    import fast_train_zeroclaw as ft
    assert not hasattr(ft, "detect_response_template")
    assert hasattr(ft, "fast_load_quantized_model")
    assert hasattr(ft, "find_latest_checkpoint")


def test_shared_log_is_single_source():
    """L-logging-dead: pipeline and hf_upload share one _default_log."""
    from pipeline import _default_log as p_log
    from hf_upload import _default_log as h_log
    assert p_log is h_log


def test_logging_config_removed():
    """structlog dead module must be gone (no importer)."""
    with pytest.raises(ImportError):
        import logging_config  # noqa: F401


def test_version_matches_pyproject():
    import tomllib
    from pathlib import Path
    from __version__ import __version__
    pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
    data = tomllib.loads(pyproject.read_text())
    assert __version__ == data["project"]["version"]
    assert __version__ == "0.3.0"
