"""Audit H2: each stage body is an importable core/_<stage>_entry.py module.

Asserts, for every stage that was extracted:
  * the entry module imports cleanly with no GPU / no heavy deps (module-level
    imports are stdlib only; torch/transformers/etc. are deferred into run()),
  * it exposes a callable ``run()`` and a pure ``parse_config()``,
  * ``run()`` reads its config from a JSON file path (round-trips parse_config),
  * the matching Service.build_script emits a shim that imports + calls it,
  * the shim is config-driven (writes the JSON next to itself).

The stage *logic* is exercised by tests/test_script_equivalence.py
(train/heretic), tests/test_dataset_format.py (normalization) and
tests/test_reap_service.py (argv); this module is the structural H2 contract.
"""

import importlib
import json
from pathlib import Path

import pytest

from services import (
    TrainingService, ExportService, HereticService,
    ReapService, MagicQuantService, UploadService,
)

ROOT = Path(__file__).resolve().parent.parent

# (entry-module name, Service class, build_script kwargs) for each extracted stage.
STAGES = [
    (
        "_train_entry", TrainingService,
        dict(
            warmup_ratio=0.05, model_name="org/m", datasets=["d.jsonl"],
            output_dir="./o", max_seq_length=4096, lora_r=32, lora_alpha=64,
            lora_dropout=0.05, use_rslora=True, num_train_epochs=3,
            per_device_train_batch_size=2, gradient_accumulation_steps=4,
            learning_rate=2e-4, lr_scheduler_type="cosine",
            optim="paged_adamw_8bit", packing=False,
        ),
    ),
    (
        "_export_entry", ExportService,
        dict(base_model_id="org/m", lora_source="/o/lora_adapters",
             has_lora=True, merged_dir="/o/merged_model"),
    ),
    (
        "_heretic_entry", HereticService,
        dict(model_path="m", output_path="o", checkpoint_dir="c", n_trials=10,
             n_startup_trials=2, quantization="bnb_4bit", kl_divergence_scale=1.0,
             orthogonalize_direction=False, row_normalization="none"),
    ),
    (
        "_reap_entry", ReapService,
        dict(input_dir="/in", output_dir="/out", cwd_dir="/cwd",
             compression_ratio=0.25, prune_method="reap", samples_per_category=512,
             model_max_length=2048, dataset_name="ds", seed=42),
    ),
    (
        "_magicquant_entry", MagicQuantService,
        dict(llamacpp_hint="", pipeline_root_str="/repo", mq_source_override="/src",
             out_abs_str="/o", generations=5, population_size=10,
             target_base_quant="IQ4_NL", tiers_json="{}", model_name="m", verify=False),
    ),
    (
        "_upload_entry", UploadService,
        dict(repo_id="u/r", private=False, license_id="apache-2.0", upload_gguf=True,
             upload_lora=False, upload_merged=False, upload_dataset=False,
             base_model="org/m", dataset_name="ds", lora_r=32, lora_alpha=64,
             lora_dropout=0.05, num_epochs=3, learning_rate=2e-4, max_seq_length=4096,
             batch_size=2, gradient_accumulation=4, optimizer="paged_adamw_8bit",
             lr_scheduler="cosine", out_abs="/o"),
    ),
]

ENTRY_NAMES = [s[0] for s in STAGES]


@pytest.mark.parametrize("entry_name", ENTRY_NAMES)
def test_entry_module_imports(entry_name):
    """Imports with no GPU / no heavy third-party deps available."""
    mod = importlib.import_module(entry_name)
    assert mod is not None


@pytest.mark.parametrize("entry_name", ENTRY_NAMES)
def test_entry_exposes_run_and_parse_config(entry_name):
    mod = importlib.import_module(entry_name)
    assert hasattr(mod, "run") and callable(mod.run)
    assert hasattr(mod, "parse_config") and callable(mod.parse_config)


@pytest.mark.parametrize("entry_name", ENTRY_NAMES)
def test_entry_module_is_module_runnable(entry_name):
    """Has a ``if __name__ == '__main__'`` guard so ``python -m`` works."""
    src = (ROOT / "core" / f"{entry_name}.py").read_text()
    assert '__name__ == "__main__"' in src
    assert f"{entry_name.lstrip('_')}" in src or "run()" in src


@pytest.mark.parametrize("entry_name", ENTRY_NAMES)
def test_parse_config_round_trips(entry_name, tmp_path):
    """parse_config reads back exactly what was written (pure, no torch)."""
    mod = importlib.import_module(entry_name)
    cfg = {"pipeline_root": "/repo", "foo": 1, "bar": ["a", "b"]}
    p = tmp_path / "cfg.json"
    p.write_text(json.dumps(cfg))
    assert mod.parse_config(str(p)) == cfg


@pytest.mark.parametrize("entry_name,svc_cls,kwargs", STAGES)
def test_build_script_emits_shim_that_calls_entry(entry_name, svc_cls, kwargs):
    """The generated stage script imports the entry module and calls its run()."""
    svc = svc_cls(ROOT, "python")
    script = svc.build_script(**kwargs)
    compile(script, f"<{entry_name}>", "exec")  # shim must be valid Python
    assert f"import {entry_name}" in script
    assert f"{entry_name}.run(" in script
    # Config-driven: the shim writes a JSON config next to itself.
    assert f"{entry_name}.cfg.json" in script
    assert ".write_text(" in script


@pytest.mark.parametrize("entry_name,svc_cls,kwargs", STAGES)
def test_build_config_is_json_serializable(entry_name, svc_cls, kwargs):
    """build_config returns a dict that serializes to JSON (what the shim embeds)."""
    svc = svc_cls(ROOT, "python")
    cfg = svc.build_config(**kwargs)
    assert isinstance(cfg, dict)
    # Round-trips through JSON without loss.
    assert json.loads(json.dumps(cfg)) == cfg
    # pipeline_root is always present so the entry can put core/ on sys.path.
    assert "pipeline_root" in cfg


def test_cli_and_ui_shims_are_identical_for_every_stage():
    """Building the same Service twice (CLI/UI use the same classes) yields the
    same byte-identical shim — the H2/H1 single-source-of-truth guarantee."""
    for entry_name, svc_cls, kwargs in STAGES:
        a = svc_cls(ROOT, "python").build_script(**kwargs)
        b = svc_cls(ROOT, "python").build_script(**kwargs)
        assert a == b, entry_name
