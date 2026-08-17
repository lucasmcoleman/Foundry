"""Regression coverage for Foundry #3: the training stage must be genuinely
optional, gated on explicit intent -- not on PipelineConfig.training being a
non-Optional field with a default_factory that was always truthy.

Before this change, EVERY invocation of core/pipeline.py -- CLI or library --
got a real TrainingConfig() (a real model name, a real EXISTING dataset file),
whether the caller wanted training or not, so _compute_enabled_stages()
always reported "training" enabled and validate_dataset() always passed. A
quantize-only campaign passing --model <repo> to name the model it wanted to
QUANTIZE silently started a QLoRA run on it instead, inside an exclusive
maintenance window (see docs/decisions/rocmfpx-stage-failure-handling.md's
sibling incident and Foundry #3's own "How it surfaced").

Covers all four acceptance criteria plus the backward-compat sites the fix
touched (_resolve_model_name, stage_export, stage_qat,
_build_hf_upload_config -- all previously assumed config.training was always
non-None).
"""

from pathlib import Path

import pytest

import pipeline as pl


def _capture_cfg(monkeypatch):
    """Intercept run_pipeline() so main() can be driven end-to-end without
    actually running any stage (matches test_qat_service.py /
    test_rocmfpx_entry.py's existing convention for CLI-level tests)."""
    captured = {}

    def _fake_run_pipeline(cfg, **kwargs):
        captured["cfg"] = cfg
        return {s: None for s, _ in pl.STAGES}

    monkeypatch.setattr(pl, "run_pipeline", _fake_run_pipeline)
    return captured


# ── AC1: a quantize-only CLI invocation does not enable training ──────────

def test_quantize_only_invocation_does_not_enable_training(monkeypatch):
    """The exact incident shape: --model <repo> naming the model to quantize,
    no --dataset(s), no --train -- must NOT enable training, and the other
    stages must still be enabled as normal."""
    captured = _capture_cfg(monkeypatch)

    pl.main([
        "--model", "org/Qwen3.8-27B", "--no-heretic", "--no-reap", "--no-qat",
        "--upload-to", "org/Qwen3.8-27B-GGUF",
    ])

    cfg = captured["cfg"]
    assert cfg.training is None
    enabled = pl._compute_enabled_stages(cfg)
    assert "training" not in enabled
    assert "export" in enabled
    assert "magicquant" in enabled
    assert "upload" in enabled


def test_bare_invocation_with_zero_flags_does_not_enable_training(monkeypatch):
    """The worst-case landmine this issue closes: literally no flags at all
    used to silently train Tesslate/OmniCoder-9B on the hardcoded zeroclaw
    dataset (both real defaults on TrainingConfig, so nothing failed loudly)."""
    captured = _capture_cfg(monkeypatch)
    pl.main([])
    assert captured["cfg"].training is None
    assert "training" not in pl._compute_enabled_stages(captured["cfg"])


def test_model_flag_alone_is_inert_on_a_disabled_training_section(monkeypatch):
    """--model does not itself flip training on, and -- since training stays
    None -- is simply not applied anywhere (no AttributeError)."""
    captured = _capture_cfg(monkeypatch)
    pl.main(["--model", "org/some-repo"])
    assert captured["cfg"].training is None


# ── AC3: genuinely-requested training runs are unaffected ─────────────────

def test_explicit_dataset_flag_enables_training(monkeypatch):
    """--dataset is training-specific -- no other stage consumes it -- so
    giving one is unambiguous training intent, and --model then customizes
    the enabled section exactly as before this fix."""
    captured = _capture_cfg(monkeypatch)
    pl.main(["--model", "org/m", "--dataset", "data/x.jsonl"])
    cfg = captured["cfg"]
    assert cfg.training is not None
    assert cfg.training.model_name == "org/m"
    assert cfg.training.datasets == ["data/x.jsonl"]
    assert "training" in pl._compute_enabled_stages(cfg)


def test_explicit_datasets_flag_enables_training(monkeypatch):
    captured = _capture_cfg(monkeypatch)
    pl.main(["--datasets", "a.jsonl", "b.jsonl"])
    cfg = captured["cfg"]
    assert cfg.training is not None
    assert cfg.training.datasets == ["a.jsonl", "b.jsonl"]


def test_explicit_train_flag_enables_training_with_defaults(monkeypatch):
    """--train with no --model/--dataset still enables the stage (with
    TrainingConfig's own defaults) -- the "just run the canonical config"
    invocation is now opt-in rather than automatic."""
    captured = _capture_cfg(monkeypatch)
    pl.main(["--train"])
    cfg = captured["cfg"]
    assert cfg.training is not None
    assert cfg.training.model_name == pl.TrainingConfig().model_name


def test_no_training_flag_overrides_dataset_and_train(monkeypatch):
    captured = _capture_cfg(monkeypatch)
    pl.main(["--train", "--dataset", "data/x.jsonl", "--no-training"])
    assert captured["cfg"].training is None


def test_no_training_flag_overrides_yaml_training_section(tmp_path, monkeypatch):
    """A YAML config that DOES configure training can still be run
    quantize-only for one invocation via --no-training, without editing the
    file (the same "wins over everything" shape as --no-heretic/--no-qat)."""
    cfg_path = tmp_path / "cfg.yaml"
    cfg_path.write_text("training:\n  model_name: org/from-yaml\n")
    captured = _capture_cfg(monkeypatch)
    pl.main(["--config", str(cfg_path), "--no-training"])
    assert captured["cfg"].training is None


def test_nested_yaml_training_section_enables_training_via_cli(tmp_path, monkeypatch):
    cfg_path = tmp_path / "cfg.yaml"
    cfg_path.write_text("training:\n  model_name: org/y\n  datasets:\n    - data/y.jsonl\n")
    captured = _capture_cfg(monkeypatch)
    pl.main(["--config", str(cfg_path)])
    cfg = captured["cfg"]
    assert cfg.training is not None
    assert cfg.training.model_name == "org/y"
    assert "training" in pl._compute_enabled_stages(cfg)


def test_flat_yaml_config_enables_training_via_cli(tmp_path, monkeypatch):
    """default.yaml-style flat file loaded end-to-end through main() --
    pins that existing flat training configs still work exactly as before."""
    cfg_path = tmp_path / "flat.yaml"
    cfg_path.write_text("model_name: org/flat\nlora_r: 16\n")
    captured = _capture_cfg(monkeypatch)
    pl.main(["--config", str(cfg_path)])
    cfg = captured["cfg"]
    assert cfg.training is not None
    assert cfg.training.model_name == "org/flat"
    assert cfg.training.lora_r == 16
    assert "training" in pl._compute_enabled_stages(cfg)


def test_flat_yaml_with_no_real_training_keys_does_not_enable_training(tmp_path):
    """A section-less YAML file that happens to have zero keys TrainingConfig
    recognizes (e.g. meant for some other tool entirely) must not spuriously
    switch training on as a side effect of the flat-layout heuristic."""
    cfg_path = tmp_path / "irrelevant.yaml"
    cfg_path.write_text("some_random_key: 1\nanother: two\n")
    cfg = pl.PipelineConfig()
    pl.load_yaml_into_config(str(cfg_path), cfg)
    assert cfg.training is None


# ── AC4: `training: null` in a YAML config disables the stage ─────────────

def test_yaml_training_null_disables_the_stage(tmp_path):
    cfg_path = tmp_path / "cfg.yaml"
    cfg_path.write_text("training: null\nmagicquant:\n  generations: 3\n")
    cfg = pl.PipelineConfig()
    pl.load_yaml_into_config(str(cfg_path), cfg)
    assert cfg.training is None
    assert cfg.magicquant.generations == 3


def test_yaml_training_null_clears_a_previously_populated_section(tmp_path):
    """Explicit null wins even when the section was already set (e.g. a base
    config layered under a quantize-only override) -- not just "absent"."""
    cfg_path = tmp_path / "cfg.yaml"
    cfg_path.write_text("training: null\n")
    cfg = pl.PipelineConfig(training=pl.TrainingConfig(model_name="org/already-set"))
    pl.load_yaml_into_config(str(cfg_path), cfg)
    assert cfg.training is None


def test_yaml_training_section_absent_leaves_training_untouched(tmp_path):
    """A nested-layout YAML that simply doesn't mention `training:` at all
    must leave whatever cfg.training already was alone (neither force it on
    nor force it off) -- distinct from an explicit `training: null`."""
    cfg_path = tmp_path / "cfg.yaml"
    cfg_path.write_text("magicquant:\n  generations: 5\n")
    cfg = pl.PipelineConfig()
    pl.load_yaml_into_config(str(cfg_path), cfg)
    assert cfg.training is None  # untouched, still the default None

    cfg2 = pl.PipelineConfig(training=pl.TrainingConfig(model_name="org/x"))
    pl.load_yaml_into_config(str(cfg_path), cfg2)
    assert cfg2.training is not None
    assert cfg2.training.model_name == "org/x"  # untouched, not cleared


# ── AC2: --dry-run prints the exact resolved stage list a real run would
#         execute ──────────────────────────────────────────────────────────

class _FakeDryRunReport:
    ok = True


def test_dry_run_prints_resolved_stage_plan(monkeypatch, capsys):
    monkeypatch.setattr(pl, "stage_upload_dry_run", lambda *a, **kw: _FakeDryRunReport())

    rc = pl.main([
        "--model", "org/m", "--no-heretic", "--no-reap", "--no-qat",
        "--upload-to", "org/m-GGUF", "--dry-run",
    ])

    assert rc == 0
    out = capsys.readouterr().out
    assert "Pipeline: export → magicquant → upload" in out
    # training must NOT appear in the printed plan (same invocation shape as
    # the quantize-only AC1 test above).
    assert "training" not in out.split("Pipeline:", 1)[1].split("\n", 1)[0]


def test_dry_run_stage_plan_matches_a_real_runs_enabled_stages(monkeypatch, capsys):
    """The dry-run plan must be exactly what run_pipeline() itself would
    compute for the identical config -- not a separately-hand-rolled list
    that could drift out of sync."""
    monkeypatch.setattr(pl, "stage_upload_dry_run", lambda *a, **kw: _FakeDryRunReport())
    argv = [
        "--train", "--model", "org/m", "--dataset", "data/x.jsonl",
        "--no-heretic", "--no-reap", "--no-qat",
        "--upload-to", "org/m-GGUF", "--dry-run",
    ]
    pl.main(argv)
    out = capsys.readouterr().out
    printed = out.split("Pipeline: ", 1)[1].strip().split(" → ")

    captured = _capture_cfg(monkeypatch)
    pl.main([a for a in argv if a != "--dry-run"])
    cfg = captured["cfg"]
    expected = [s for s, _ in pl.STAGES if s in pl._compute_enabled_stages(cfg)]

    assert printed == expected
    assert "training" in expected  # sanity: this arm DOES enable training


# ── Backward-compat: sites that used to assume config.training was always
#    non-None must not crash now that it can genuinely be None ────────────

def test_resolve_model_name_training_none_falls_through_safely(tmp_path):
    artifacts = pl.Artifacts(str(tmp_path / "out"))
    config = pl.PipelineConfig()
    config.rocmfpx = pl.ROCmFPXConfig()
    assert config.training is None

    result = pl._resolve_model_name(
        config, artifacts, lambda *a, **k: None,
        source_model_field="rocmfpx", source=Path("/models/Some-Model-FP16"),
    )
    assert result == "Some-Model"


def test_build_hf_upload_config_training_none_does_not_crash(tmp_path):
    config = pl.PipelineConfig(output_dir=str(tmp_path))
    config.upload = pl.UploadConfig(
        repo_id="org/model-GGUF", base_model="org/base-model", license="mit",
    )
    assert config.training is None

    logs = []
    hf_cfg = pl._build_hf_upload_config(
        config, lambda msg, level="info": logs.append((level, msg)),
    )

    assert hf_cfg is not None
    assert hf_cfg.did_training is False
    assert hf_cfg.base_model == "org/base-model"  # explicit uc.base_model wins
    assert hf_cfg.dataset_name == ""


def test_build_hf_upload_config_training_none_falls_back_to_magicquant_source(tmp_path):
    """base_model must NOT fall back to TrainingConfig()'s dummy default
    (Tesslate/OmniCoder-9B) when training is disabled and uc.base_model
    wasn't set -- that would misreport a quantize-only run's card."""
    config = pl.PipelineConfig(output_dir=str(tmp_path))
    config.magicquant.source_model = "org/real-source-model"
    config.upload = pl.UploadConfig(repo_id="org/model-GGUF", license="mit")
    assert config.training is None

    hf_cfg = pl._build_hf_upload_config(config, lambda *a, **k: None)

    assert hf_cfg.base_model == "org/real-source-model"
    assert hf_cfg.base_model != pl.TrainingConfig().model_name


def test_stage_export_training_none_falls_back_safely(tmp_path, monkeypatch):
    """A resumed export-only run against adapters produced by an EARLIER
    invocation, where THIS run's config never configured training, must not
    AttributeError computing base_model_id from None.training."""
    out_dir = tmp_path / "out"
    artifacts = pl.Artifacts(str(out_dir))
    artifacts.lora_dir.mkdir(parents=True)
    (artifacts.lora_dir / "adapter_model.safetensors").write_bytes(b"x")
    # Deliberately no adapter_config.json -- exercises the `else` fallback
    # branch (core/pipeline.py's stage_export), not the adapter_cfg.get()
    # default-argument branch.

    monkeypatch.setattr(pl, "_system_memory_gate", lambda *a, **kw: True)
    monkeypatch.setattr(pl, "_preflight_stage", lambda *a, **kw: None)
    monkeypatch.setattr(pl, "_run_stage_script", lambda *a, **kw: 1)

    config = pl.PipelineConfig(output_dir=str(out_dir))
    assert config.training is None

    ok = pl.stage_export(config, artifacts, lambda *a, **k: None)  # must not raise
    assert ok is False  # _run_stage_script's forced failure, reached cleanly


def test_stage_qat_training_none_falls_back_safely(tmp_path, monkeypatch):
    out_dir = tmp_path / "out"
    artifacts = pl.Artifacts(str(out_dir))
    artifacts.magicquant_dir.mkdir(parents=True)
    (artifacts.magicquant_dir / "search_results.json").write_text("{}")

    monkeypatch.setattr(pl, "_system_memory_gate", lambda *a, **kw: True)
    monkeypatch.setattr(pl, "_preflight_stage", lambda *a, **kw: None)
    monkeypatch.setattr(pl, "_run_stage_script", lambda *a, **kw: 1)

    config = pl.PipelineConfig(output_dir=str(out_dir))
    config.qat = pl.QATConfig(dataset="data/qat.jsonl")
    assert config.training is None

    ok = pl.stage_qat(config, artifacts, lambda *a, **k: None)  # must not raise
    assert ok is False
