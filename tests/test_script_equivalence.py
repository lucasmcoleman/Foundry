"""H1 keystone: CLI and UI must generate equivalent stage scripts.

Both entrypoints now build subprocess scripts via the shared Service classes in
core/services.py. This test proves that, given identical config, the CLI and UI
produce byte-identical training scripts (the divergence the audit found), and
asserts the specific properties the audit called out (warmup unification, no
prepare_model_for_kbit_training, rslora present).
"""

from pathlib import Path

import pytest

from services import TrainingService, HereticService

ROOT = Path(__file__).resolve().parent.parent


COMMON = dict(
    model_name="org/some-model",
    datasets=["data/training.jsonl"],
    output_dir="./output/some-model",
    max_seq_length=4096,
    lora_r=32,
    lora_alpha=64,
    lora_dropout=0.05,
    use_rslora=True,
    num_train_epochs=3,
    per_device_train_batch_size=2,
    gradient_accumulation_steps=4,
    learning_rate=2e-4,
    lr_scheduler_type="cosine",
    optim="paged_adamw_8bit",
    packing=False,
)


def _svc():
    return TrainingService(ROOT, "python")


def test_cli_and_ui_training_scripts_are_identical():
    """CLI passes warmup_ratio; the UI now also passes warmup_ratio (+ optional
    warmup_steps=None). With the same config the scripts must be byte-identical."""
    svc = _svc()
    cli_script = svc.build_script(warmup_ratio=0.05, **COMMON)
    ui_script = svc.build_script(warmup_ratio=0.05, warmup_steps=None, **COMMON)
    assert cli_script == ui_script


def test_training_script_compiles():
    svc = _svc()
    script = svc.build_script(warmup_ratio=0.05, **COMMON)
    compile(script, "<train>", "exec")  # raises SyntaxError on a codegen typo


def test_warmup_is_unified_on_ratio():
    svc = _svc()
    script = svc.build_script(warmup_ratio=0.05, **COMMON)
    assert "warmup_ratio=0.05" in script
    assert "warmup_steps" not in script


def test_warmup_steps_only_when_no_ratio():
    svc = _svc()
    script = svc.build_script(warmup_steps=10, **COMMON)
    assert "warmup_steps=10" in script
    assert "warmup_ratio" not in script


def test_warmup_requires_at_least_one():
    svc = _svc()
    with pytest.raises(ValueError):
        svc.build_script(**COMMON)  # neither warmup_ratio nor warmup_steps


def test_no_prepare_model_for_kbit_training():
    """The CLI's old prepare_model_for_kbit_training path (which fp32-upcast MoE
    experts) must be gone — the canonical path is the manual norm-upcast."""
    svc = _svc()
    script = svc.build_script(warmup_ratio=0.05, **COMMON)
    assert "prepare_model_for_kbit_training" not in script
    assert "use_rslora=True" in script
    # Manual kbit prep markers present.
    assert "param.requires_grad = False" in script


def test_string_values_are_repr_escaped():
    """No bare f-string interpolation of model_name into the script body."""
    svc = _svc()
    nasty = "org/m'; import os; os.system('x') #"
    cfg = dict(COMMON)
    cfg["model_name"] = nasty
    script = svc.build_script(warmup_ratio=0.05, **cfg)
    compile(script, "<train>", "exec")  # repr() keeps it a valid string literal
    assert repr(nasty) in script


def test_heretic_script_has_gpt_oss_prefix_and_simple_selection():
    """HereticService must carry the GPT-OSS <|channel|>analysis prefix branch
    (was CLI-only) and the simplified Pareto selection (L-heretic-deadloop)."""
    svc = HereticService(ROOT, "python")
    script = svc.build_script(
        model_path="m", output_path="o", checkpoint_dir="c",
        n_trials=10, n_startup_trials=2, quantization="bnb_4bit",
        kl_divergence_scale=1.0, orthogonalize_direction=False,
        row_normalization="none",
    )
    compile(script, "<heretic>", "exec")
    assert "<|channel|>analysis<|message|>" in script
    assert "best = sorted_trials[0]" in script
    # The dead min_div loop must be gone.
    assert "best_trials.append" not in script
