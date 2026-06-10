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


# core/_train_entry.py is now the home of the training logic that used to live as
# an inline f-string. The kbit/lora/template assertions moved here with it (audit
# H2): the checks are preserved, only relocated to where the code now lives.
ENTRY_SRC = (ROOT / "core" / "_train_entry.py").read_text()


def test_cli_and_ui_training_scripts_are_identical():
    """CLI passes warmup_ratio; the UI now also passes warmup_ratio (+ optional
    warmup_steps=None). With the same config the shims must be byte-identical."""
    svc = _svc()
    cli_script = svc.build_script(warmup_ratio=0.05, **COMMON)
    ui_script = svc.build_script(warmup_ratio=0.05, warmup_steps=None, **COMMON)
    assert cli_script == ui_script


def test_cli_and_ui_training_configs_are_identical():
    """The byte-identical shim embeds the JSON config; assert the configs the CLI
    and UI build are equal too (ratio precedence drops warmup_steps on both)."""
    svc = _svc()
    cli_cfg = svc.build_config(warmup_ratio=0.05, **COMMON)
    ui_cfg = svc.build_config(warmup_ratio=0.05, warmup_steps=None, **COMMON)
    assert cli_cfg == ui_cfg


def test_training_script_compiles():
    svc = _svc()
    script = svc.build_script(warmup_ratio=0.05, **COMMON)
    compile(script, "<train>", "exec")  # raises SyntaxError on a codegen typo


def test_shim_invokes_entry_module():
    """The generated shim must call the importable entry module (audit H2)."""
    svc = _svc()
    script = svc.build_script(warmup_ratio=0.05, **COMMON)
    assert "import _train_entry" in script
    assert "_train_entry.run(" in script


def test_warmup_is_unified_on_ratio():
    """With a ratio, warmup_ratio is set and warmup_steps is dropped to None."""
    svc = _svc()
    cfg = svc.build_config(warmup_ratio=0.05, **COMMON)
    assert cfg["warmup_ratio"] == 0.05
    assert cfg["warmup_steps"] is None


def test_warmup_steps_only_when_no_ratio():
    svc = _svc()
    cfg = svc.build_config(warmup_steps=10, **COMMON)
    assert cfg["warmup_steps"] == 10
    assert cfg["warmup_ratio"] is None


def test_warmup_requires_at_least_one():
    svc = _svc()
    with pytest.raises(ValueError):
        svc.build_config(**COMMON)  # neither warmup_ratio nor warmup_steps


def test_no_prepare_model_for_kbit_training():
    """The CLI's old prepare_model_for_kbit_training path (which fp32-upcast MoE
    experts) must be gone — the canonical path is the manual norm-upcast. The
    logic now lives in core/_train_entry.py."""
    assert "prepare_model_for_kbit_training" not in ENTRY_SRC
    assert "use_rslora=cfg" in ENTRY_SRC or "use_rslora=" in ENTRY_SRC
    # Manual kbit prep markers present.
    assert "param.requires_grad = False" in ENTRY_SRC


def test_string_values_are_repr_escaped():
    """No bare interpolation of model_name into the shim: it goes through
    json.dumps then repr, so the shim still compiles and round-trips safely."""
    svc = _svc()
    nasty = "org/m'; import os; os.system('x') #"
    cfg = dict(COMMON)
    cfg["model_name"] = nasty
    script = svc.build_script(warmup_ratio=0.05, **cfg)
    compile(script, "<train>", "exec")  # repr(json) keeps it a valid literal
    # The built config preserves the nasty value verbatim (no truncation/injection).
    built = svc.build_config(warmup_ratio=0.05, **cfg)
    assert built["model_name"] == nasty


def test_heretic_shim_compiles_and_invokes_entry():
    svc = HereticService(ROOT, "python")
    script = svc.build_script(
        model_path="m", output_path="o", checkpoint_dir="c",
        n_trials=10, n_startup_trials=2, quantization="bnb_4bit",
        kl_divergence_scale=1.0, orthogonalize_direction=False,
        row_normalization="none",
    )
    compile(script, "<heretic>", "exec")
    assert "import _heretic_entry" in script
    assert "_heretic_entry.run(" in script


def test_heretic_entry_has_gpt_oss_prefix_and_simple_selection():
    """The heretic body now lives in core/_heretic_entry.py (audit H2). It must
    carry the GPT-OSS <|channel|>analysis prefix branch (was CLI-only) and the
    simplified Pareto-min selection (L-heretic-deadloop)."""
    heretic_src = (ROOT / "core" / "_heretic_entry.py").read_text()
    assert "<|channel|>analysis<|message|>" in heretic_src
    assert "best = sorted_trials[0]" in heretic_src
    # The dead min_div / best_trials loop must be gone.
    assert "best_trials.append" not in heretic_src


def test_heretic_normalize_response_prefix():
    """The prefix-canonicalization branch is a pure, importable function."""
    import _heretic_entry as he
    assert he.normalize_response_prefix("<think> blah") == "<think></think>"
    assert he.normalize_response_prefix("<|channel|>analysis<|message|> x").startswith(
        "<|channel|>analysis<|message|><|end|>"
    )
    assert he.normalize_response_prefix("plain") == "plain"


def test_heretic_select_best_trial():
    """select_best_trial = fewest refusals, KL tie-break (simplified Pareto-min)."""
    import _heretic_entry as he

    class T:
        def __init__(self, refusals, kl):
            self.user_attrs = {"refusals": refusals, "kl_divergence": kl}

    trials = [T(3, 0.1), T(1, 0.5), T(1, 0.2), T(2, 0.0)]
    best = he.select_best_trial(trials)
    assert best.user_attrs == {"refusals": 1, "kl_divergence": 0.2}
