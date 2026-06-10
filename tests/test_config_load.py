"""L-config-fragmentation: every configs/*.yaml must actually populate the config.

Previously configs/default.yaml was flat (no `training:` wrapper) and the CLI
loader only read `data['training']`, so `--config configs/default.yaml` was a
silent no-op. The loader now accepts both flat and nested layouts and populates
all sections.
"""

from pathlib import Path

import pytest

from pipeline import PipelineConfig, load_yaml_into_config

ROOT = Path(__file__).resolve().parent.parent
CONFIGS = ROOT / "configs"


@pytest.mark.parametrize("yaml_file", sorted(CONFIGS.glob("*.yaml")), ids=lambda p: p.name)
def test_every_config_loads_without_error(yaml_file):
    cfg = PipelineConfig()
    load_yaml_into_config(str(yaml_file), cfg)  # must not raise


def test_default_yaml_is_no_longer_a_noop():
    """default.yaml is flat; it sets model_name/lora_r/max_seq_length etc."""
    cfg = PipelineConfig()
    # Sanity: a fresh config does NOT have these flat-file values.
    assert cfg.training.model_name != "unsloth/Qwen3-8B"
    load_yaml_into_config(str(CONFIGS / "default.yaml"), cfg)
    assert cfg.training.model_name == "unsloth/Qwen3-8B"
    assert cfg.training.lora_r == 32
    assert cfg.training.max_seq_length == 4096
    assert cfg.training.gradient_accumulation_steps == 8


def test_nested_yaml_populates_training_section():
    """bf16-zeroclaw.yaml uses the nested `training:` layout."""
    cfg = PipelineConfig()
    load_yaml_into_config(str(CONFIGS / "bf16-zeroclaw.yaml"), cfg)
    assert cfg.training.per_device_train_batch_size == 1
    assert cfg.training.gradient_accumulation_steps == 8
    assert cfg.training.max_seq_length == 4096
    assert cfg.training.load_in_4bit is False


def test_unknown_keys_are_ignored(tmp_path):
    """Flat extras like target_modules/save_strategy don't crash the loader."""
    p = tmp_path / "extra.yaml"
    p.write_text("model_name: org/x\nlora_r: 16\nsave_strategy: epoch\nbogus_key: 1\n")
    cfg = PipelineConfig()
    load_yaml_into_config(str(p), cfg)
    assert cfg.training.model_name == "org/x"
    assert cfg.training.lora_r == 16
    assert not hasattr(cfg.training, "bogus_key")


def test_nested_optional_sections_are_instantiated(tmp_path):
    p = tmp_path / "n.yaml"
    p.write_text(
        "training:\n  lora_r: 8\n"
        "heretic:\n  n_trials: 42\n"
        "magicquant:\n  generations: 7\n"
    )
    cfg = PipelineConfig()
    load_yaml_into_config(str(p), cfg)
    assert cfg.training.lora_r == 8
    assert cfg.heretic is not None and cfg.heretic.n_trials == 42
    assert cfg.magicquant.generations == 7
