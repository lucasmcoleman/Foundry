"""Model-name derivation: ONE shared implementation (core/services.py) used by
both ui/app.py and core/pipeline.py, replacing two independent copies (the
UI's had a stale-name bug, the CLI's had no fallback/sanitization at all).

Background (Foundry issues #5 and #6, both traced to
ui/app.py::_derive_model_short_name):

  #5 -- A standalone re-run of ["magicquant", "upload"] against a COMPLETED
       run resolved the run directory via the function's final, unconditional
       `else: raw = cfg.training.model_name` -- used even when "training" was
       not in the enabled-stage set. The workflow this was reproduced from
       (ThinkingCap-Qwen36-27B-Fable5Traces-Heretic-Uncensored-GGUF.json) still
       carries `training.model_name = "bottlecapai/ThinkingCap-Qwen3.6-27B"`
       from the template it was cloned from -- so the mis-resolved directory
       was `output/ThinkingCap-Qwen3.6-27B`, not the completed run's own
       directory. The pipeline then created that (wrong, empty) directory and
       failed validation with a message that didn't even say which directory
       it had checked.

  #6 -- model_name derives from export.source_model's basename with no
       precision-suffix handling. nvidia/NVIDIA-Nemotron-3.5-Lightning-30B-A3B-BF16
       produced GGUF filenames like "...-A3B-BF16-Q4_K_M.gguf" -- a Q4 file
       whose name claims BF16.

This module tests the shared derivation directly (core/services.py) and each
orchestrator's integration of it.
"""

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

from services import (  # noqa: E402  (conftest puts core/ on sys.path)
    ModelNameUnresolvedError,
    _existing_run_basename,
    _has_run_artifacts,
    _PRECISION_SUFFIX_RE,
    derive_model_short_name,
)


# ── Fallback layering, in pipeline order ─────────────────────────────────────

def test_layer_1_training_wins_when_enabled():
    name = derive_model_short_name(
        training_model_name="org/Some-Model", training_enabled=True,
        export_source_model="org/Ignored-1", export_enabled=True,
        magicquant_source_model="org/Ignored-2", magicquant_enabled=True,
        rocmfpx_source_model="org/Ignored-3", rocmfpx_enabled=True,
    )
    assert name == "Some-Model"


def test_layer_1_skipped_when_training_not_enabled():
    """The exact shape of bug #5: training has a real (non-empty) model_name,
    but training is not part of THIS run -- it must not win."""
    name = derive_model_short_name(
        training_model_name="org/Should-Not-Win", training_enabled=False,
        export_source_model="org/Export-Model", export_enabled=True,
    )
    assert name == "Export-Model"


def test_layer_2_export_source_model():
    name = derive_model_short_name(
        training_model_name="org/T", training_enabled=False,
        export_source_model="org/Export-Model", export_enabled=True,
        magicquant_source_model="org/Ignored", magicquant_enabled=True,
    )
    assert name == "Export-Model"


def test_layer_3_magicquant_source_model():
    name = derive_model_short_name(
        training_model_name="org/T", training_enabled=False,
        export_source_model="", export_enabled=True,
        magicquant_source_model="org/MQ-Model", magicquant_enabled=True,
        rocmfpx_source_model="org/Ignored", rocmfpx_enabled=True,
    )
    assert name == "MQ-Model"


def test_layer_4_rocmfpx_source_model_is_a_real_fallback_layer():
    """Pre-existing gap called out in the spec: do_rocmfpx already honors its
    own source_model override when running, but the OLD name deriver never
    consulted rocmfpx.source_model at all -- it fell straight through to
    training.model_name. This is the fourth, previously-missing layer."""
    name = derive_model_short_name(
        training_model_name="org/T", training_enabled=False,
        export_source_model="", export_enabled=True,
        magicquant_source_model="", magicquant_enabled=True,
        rocmfpx_source_model="org/ROCmFPX-Model", rocmfpx_enabled=True,
    )
    assert name == "ROCmFPX-Model"


def test_layer_disabled_stage_does_not_win_even_with_a_source_model_set():
    """A leftover/unused source_model field on a DISABLED stage must not
    out-rank an active stage -- same class of bug as #5, just one layer over.
    Order still matters: export (disabled, has a value) must lose to
    magicquant (enabled, has a value)."""
    name = derive_model_short_name(
        training_model_name="org/T", training_enabled=False,
        export_source_model="org/Stale-Disabled-Export", export_enabled=False,
        magicquant_source_model="org/Active-MQ", magicquant_enabled=True,
    )
    assert name == "Active-MQ"


# ── Precision-suffix stripping (issue #6) ────────────────────────────────────

# NOTE the base name here is a realistic one, not the bare word "Model".
# "Model" is precisely the degenerate case _NON_IDENTIFYING_NAMES protects, so
# using it as a fixture would assert the behaviour that guard exists to
# prevent (see test_precision_strip_never_produces_a_non_identifying_name).
@pytest.mark.parametrize("raw,expected", [
    ("nvidia/NVIDIA-Nemotron-3.5-Lightning-30B-A3B-BF16", "NVIDIA-Nemotron-3.5-Lightning-30B-A3B"),
    ("org/Llama-3-8B-FP16", "Llama-3-8B"),
    ("org/Llama-3-8B-F16", "Llama-3-8B"),
    ("org/Llama-3-8B-FP32", "Llama-3-8B"),
    ("org/Llama-3-8B-F32", "Llama-3-8B"),
    ("org/Llama-3-8B-bf16", "Llama-3-8B"),   # case-insensitive
    ("org/Llama-3-8B_BF16", "Llama-3-8B"),   # underscore variant
])
def test_precision_suffix_stripped(raw, expected):
    assert derive_model_short_name(
        training_model_name=raw, training_enabled=True,
    ) == expected


@pytest.mark.parametrize("raw,expected", [
    ("nvidia/Llama-3.3-70B-Instruct-FP8", "Llama-3.3-70B-Instruct-FP8"),
    ("deepseek-ai/DeepSeek-V3-FP8", "DeepSeek-V3-FP8"),
    ("org/Llama-3-8B-F8", "Llama-3-8B-F8"),
])
def test_fp8_is_model_identity_not_a_suffix(raw, expected):
    """FP8 must NOT be stripped. For these repos FP8 IS the published model,
    distinct from the BF16 original -- stripping it merges two different models
    into one run directory and one GGUF prefix. There is also no upside: the
    writer rejects a pre-quantized source, so an FP8 repo never reaches this
    path as a quantization source anyway."""
    assert derive_model_short_name(
        training_model_name=raw, training_enabled=True,
    ) == expected


@pytest.mark.parametrize("raw,expected", [
    # Foundry's real layout: the artifact sits in a run directory named after
    # the model, so the PARENT carries the identity.
    ("/server/out/NVIDIA-Nemotron-30B-A3B/model-bf16.gguf", "NVIDIA-Nemotron-30B-A3B"),
    ("/server/out/NVIDIA-Nemotron-30B-A3B/merged_model", "NVIDIA-Nemotron-30B-A3B"),
    ("/server/out/Llama-3-8B/model-bf16-nomtp.gguf", "Llama-3-8B"),
    # Parent is itself generic -> walking up would just move the collision one
    # level, so keep the artifact basename instead.
    ("/output/model-bf16.gguf", "model-bf16"),
    ("/merged/model-bf16", "model-bf16"),
    # No parent at all.
    ("model-bf16.gguf", "model-bf16"),
    # A real model that merely LOOKS like an artifact name must be untouched.
    ("org/model-zephyr-7b", "model-zephyr-7b"),
])
def test_artifact_basename_resolves_to_the_run_directory(raw, expected):
    """A quant-only workflow points magicquant.source_model at an artifact
    INSIDE a run directory -- model-bf16.gguf, merged_model -- and every run
    directory on the box holds identically-named ones. Deriving from that
    basename would give every model the same directory and the same GGUF
    prefix. When the final segment is a known Foundry artifact, identity comes
    from the parent -- but only if the parent is itself identifying, or we have
    just relocated the collision rather than fixed it."""
    assert derive_model_short_name(
        magicquant_source_model=raw, magicquant_enabled=True,
    ) == expected


def test_precision_suffix_final_segment_only():
    """A precision-looking token that is NOT the final segment must survive
    untouched -- 'Foo-BF16-Instruct' is a real model name, not 'Foo-BF16'
    with a trailing tag."""
    assert derive_model_short_name(
        training_model_name="org/Foo-BF16-Instruct", training_enabled=True,
    ) == "Foo-BF16-Instruct"


def test_precision_suffix_stripped_after_extension():
    """Extension-stripping must run first so a GGUF filename's precision tag
    is still recognized as the final segment."""
    assert derive_model_short_name(
        training_model_name="org/Llama-3-8B-BF16.gguf", training_enabled=True,
    ) == "Llama-3-8B"


# ── Sanitization / extension stripping (pre-existing behavior, preserved) ───

@pytest.mark.parametrize("ext", [".gguf", ".safetensors", ".bin", ".pt", ".pth"])
def test_known_extensions_stripped(ext):
    assert derive_model_short_name(
        training_model_name=f"/local/path/Some-Model{ext}", training_enabled=True,
    ) == "Some-Model"


def test_sanitizes_unsafe_characters():
    name = derive_model_short_name(
        training_model_name="org/Weird Model!! Name??", training_enabled=True,
    )
    assert re.fullmatch(r"[A-Za-z0-9\-_.]+", name)
    assert "!" not in name and " " not in name


# ── ModelNameUnresolvedError: refuse, never guess ────────────────────────────

def test_raises_when_nothing_resolves():
    with pytest.raises(ModelNameUnresolvedError):
        derive_model_short_name()


def test_raises_when_only_disabled_layers_have_values():
    with pytest.raises(ModelNameUnresolvedError):
        derive_model_short_name(
            training_model_name="org/T", training_enabled=False,
            export_source_model="org/E", export_enabled=False,
            magicquant_source_model="org/M", magicquant_enabled=False,
            rocmfpx_source_model="org/R", rocmfpx_enabled=False,
        )


def test_raises_when_sanitized_result_would_be_empty():
    """A pathological raw name that sanitizes to nothing must refuse, not
    silently fall back to a generic literal like 'model' (that IS a
    wrongly-named directory, just a boring one)."""
    with pytest.raises(ModelNameUnresolvedError):
        derive_model_short_name(training_model_name="???", training_enabled=True)


# ── _existing_run_basename: the one safe last resort ─────────────────────────

def test_existing_run_basename_none_for_missing_dir(tmp_path):
    assert _existing_run_basename(tmp_path / "does-not-exist") is None


def test_existing_run_basename_none_for_empty_base(tmp_path):
    assert _existing_run_basename(tmp_path) is None


def test_existing_run_basename_self_match(tmp_path):
    """The CLI hands this an already model-specific directory; a marker
    directly inside it should resolve to ITS OWN name."""
    run_dir = tmp_path / "SomeModel"
    (run_dir / "merged_model").mkdir(parents=True)
    assert _existing_run_basename(run_dir) == "SomeModel"


def test_existing_run_basename_gguf_glob_marker(tmp_path):
    run_dir = tmp_path / "SomeModel"
    mq = run_dir / "magicquant"
    mq.mkdir(parents=True)
    (mq / "SomeModel-Q4_K_M.gguf").write_bytes(b"x")
    assert _existing_run_basename(run_dir) == "SomeModel"
    assert _has_run_artifacts(run_dir) is True


@pytest.mark.parametrize("n_populated", [0, 1, 2])
def test_existing_run_basename_never_scans_siblings(tmp_path, n_populated):
    """A shared BASE directory must resolve to nothing, no matter how many of
    its children look like runs.

    The first version of this accepted a child when EXACTLY ONE had artifacts.
    That is not a safety property. On the real box exactly one child had
    artifacts -- because a cleanup had deleted the others -- so every workflow
    narrowed to ["magicquant","upload"] resolved to one unrelated model's
    346 GB run directory, validation PASSED because that directory genuinely
    holds a merged model, and the pipeline would have quantized and published
    the wrong model under the loaded workflow's repo_id. Issue #5 at least
    failed loudly on an empty directory.

    n_populated is parametrized over 0, 1 and 2 deliberately: 1 was the state
    the box happened to be in, and it is the one that looked safe.
    """
    (tmp_path / "OtherEmptyDir").mkdir()
    for i in range(n_populated):
        (tmp_path / f"SomeRun{i}" / "merged_model").mkdir(parents=True)
    assert _existing_run_basename(tmp_path) is None


def test_existing_run_basename_self_check_still_works(tmp_path):
    """The legitimate case it DOES serve: handed a model-specific directory
    that is itself a populated run, it returns that directory's own name."""
    run = tmp_path / "ThinkingCap-Real-Run"
    (run / "merged_model").mkdir(parents=True)
    assert _existing_run_basename(run) == "ThinkingCap-Real-Run"


def test_existing_run_basename_ambiguous_multiple_children_refuses(tmp_path):
    """Never guess: with genuinely many run directories under the base (the
    normal state of this operator's output/ -- see the spot-check below),
    resolving to any one of them would as often be wrong as right."""
    (tmp_path / "ModelA" / "merged_model").mkdir(parents=True)
    (tmp_path / "ModelB" / "merged_model").mkdir(parents=True)
    assert _existing_run_basename(tmp_path) is None


def test_derive_falls_through_to_existing_run_basename(tmp_path):
    """The fallback resolves the run directory it was HANDED, not one it
    inferred from siblings -- and the result goes through the same precision
    strip every other layer gets (issue #6 was previously unfixed on exactly
    the path issue #5 travels)."""
    run = tmp_path / "TheOnlyRun-BF16"
    (run / "merged_model").mkdir(parents=True)
    name = derive_model_short_name(
        training_model_name="org/Stale", training_enabled=False,
        output_dir=run,
    )
    assert name == "TheOnlyRun"


def test_derive_raises_when_existing_run_basename_ambiguous(tmp_path):
    (tmp_path / "ModelA" / "merged_model").mkdir(parents=True)
    (tmp_path / "ModelB" / "merged_model").mkdir(parents=True)
    with pytest.raises(ModelNameUnresolvedError):
        derive_model_short_name(
            training_model_name="org/Stale", training_enabled=False,
            output_dir=tmp_path,
        )


# ── ui/app.py integration ────────────────────────────────────────────────────

def test_ui_shares_the_one_implementation():
    """Not just equivalent behavior -- the literal same function object, so
    the two orchestrators cannot drift back into separate copies."""
    import services
    import app as app_module

    assert app_module._shared_derive_model_short_name is services.derive_model_short_name
    assert app_module.ModelNameUnresolvedError is services.ModelNameUnresolvedError


def test_thinkingcap_rerun_no_longer_returns_the_stale_name(tmp_path):
    """Reproduces issue #5 exactly: re-running just ["magicquant", "upload"]
    against a workflow whose training/export sections still carry
    'bottlecapai/ThinkingCap-Qwen3.6-27B' from the template it was cloned
    from. With no output_dir artifacts to fall back to, this must now refuse
    instead of silently resolving to the stale name."""
    import app as app_module

    cfg = app_module.RunRequest(
        training=app_module.TrainingCfg(
            model_name="bottlecapai/ThinkingCap-Qwen3.6-27B",
            output_dir=str(tmp_path / "output"),
        ),
        export=app_module.ExportCfg(source_model="bottlecapai/ThinkingCap-Qwen3.6-27B"),
        magicquant=app_module.MagicQuantCfg(source_model=""),
        rocmfpx=app_module.ROCmFPXCfg(source_model=""),
        enabled_stages=["magicquant", "upload"],  # training/export unchecked at run time
    )
    with pytest.raises(app_module.ModelNameUnresolvedError):
        app_module._derive_model_short_name(cfg)


def test_thinkingcap_rerun_from_a_shared_base_refuses_rather_than_guessing(tmp_path):
    """The issue-#5 scenario, and the honest limit of what can be fixed here.

    The UI hands the deriver the SHARED base directory and expects a run
    directory named after the model -- but the model name is what we are
    trying to derive, so there is nothing to resolve against. The original fix
    closed that circle by scanning the base's children and accepting the answer
    when exactly one looked like a run. That is what would have quantized
    Nemotron and published it to ThinkingCap's repo_id.

    So this now REFUSES, which is the other option the bug report itself
    offered ("or refuse with an explicit 'cannot determine run directory'
    rather than inventing one"). The operator sets a Source Model on an
    enabled stage -- one field -- and the run proceeds. That is a strictly
    better trade than a silent wrong-model publish, and the message says
    exactly which directory was searched.
    """
    import app as app_module

    out_base = tmp_path / "output"
    (out_base / "ThinkingCap-Real-Output" / "magicquant").mkdir(parents=True)
    (out_base / "ThinkingCap-Real-Output" / "magicquant" / "model-Q4.gguf").write_bytes(b"x")

    cfg = app_module.RunRequest(
        training=app_module.TrainingCfg(
            model_name="bottlecapai/ThinkingCap-Qwen3.6-27B",
            output_dir=str(out_base),
        ),
        export=app_module.ExportCfg(source_model="bottlecapai/ThinkingCap-Qwen3.6-27B"),
        magicquant=app_module.MagicQuantCfg(source_model=""),
        enabled_stages=["magicquant", "upload"],
    )
    with pytest.raises(ModelNameUnresolvedError):
        app_module._derive_model_short_name(cfg)

    # The stale name must NOT appear -- that is the original bug.
    try:
        app_module._derive_model_short_name(cfg)
    except ModelNameUnresolvedError as exc:
        assert "ThinkingCap-Qwen3.6-27B" not in str(exc)


def test_thinkingcap_rerun_resolves_when_given_the_run_directory(tmp_path):
    """And the case that DOES work: point it at the actual run directory and
    it resolves to that directory's own name."""
    import app as app_module

    run = tmp_path / "output" / "ThinkingCap-Real-Output"
    (run / "magicquant").mkdir(parents=True)
    (run / "magicquant" / "model-Q4.gguf").write_bytes(b"x")

    cfg = app_module.RunRequest(
        training=app_module.TrainingCfg(
            model_name="bottlecapai/ThinkingCap-Qwen3.6-27B",
            output_dir=str(run),
        ),
        export=app_module.ExportCfg(source_model="bottlecapai/ThinkingCap-Qwen3.6-27B"),
        magicquant=app_module.MagicQuantCfg(source_model=""),
        enabled_stages=["magicquant", "upload"],
    )
    assert app_module._derive_model_short_name(cfg) == "ThinkingCap-Real-Output"

def test_nemotron_gguf_name_no_longer_carries_bf16(tmp_path):
    """Reproduces issue #6: export.source_model is the upstream repo id
    (which legitimately keeps its -BF16 tag, per derive_base_model), but the
    derived short name used for OUTPUT filenames must not."""
    import app as app_module

    cfg = app_module.RunRequest(
        training=app_module.TrainingCfg(
            model_name="nvidia/NVIDIA-Nemotron-3.5-Lightning-30B-A3B-BF16",
            output_dir=str(tmp_path / "output"),
        ),
        export=app_module.ExportCfg(
            source_model="nvidia/NVIDIA-Nemotron-3.5-Lightning-30B-A3B-BF16"
        ),
        enabled_stages=["export", "magicquant", "rocmfpx", "upload"],  # training NOT enabled
    )
    assert app_module._derive_model_short_name(cfg) == "NVIDIA-Nemotron-3.5-Lightning-30B-A3B"


async def test_run_pipeline_does_not_mkdir_when_name_unresolved(tmp_path, monkeypatch):
    """Point 5 of the fix: resolve the name BEFORE creating the output
    directory, and abort cleanly (no dangling 'running' state) instead of
    creating a wrongly-named directory then failing validation against it."""
    import app as app_module

    monkeypatch.setattr(app_module, "state", app_module.PipelineState())
    out_base = tmp_path / "output"

    cfg = app_module.RunRequest(
        training=app_module.TrainingCfg(
            model_name="org/Irrelevant-Default", output_dir=str(out_base),
        ),
        export=None,
        magicquant=app_module.MagicQuantCfg(source_model=""),
        rocmfpx=None,
        enabled_stages=["magicquant", "upload"],  # training not enabled -> unresolvable
    )

    await app_module.run_pipeline(cfg)

    assert not out_base.exists(), "run_pipeline must not mkdir before the name resolves"
    assert app_module.state.running is False


def test_validate_pipeline_messages_name_the_checked_directory():
    """Point 6 of the fix: the 'no existing model artifacts' messages must
    say WHICH directory was checked -- that's half of what made issue #5 hard
    to diagnose (the message gave no hint the directory was wrong)."""
    src = (ROOT / "ui" / "app.py").read_text()
    mq_block = src[src.index('if "magicquant" in enabled and "export" not in enabled'):]
    mq_block = mq_block[:mq_block.index('# ROCmFPX without export')]
    assert 'f"MagicQuant is enabled without Export' in mq_block
    assert "{out_abs}" in mq_block

    rc_block = src[src.index('if "rocmfpx" in enabled and "export" not in enabled'):]
    rc_block = rc_block[:rc_block.index('# Upload: check')]
    assert 'f"ROCmFPX is enabled without Export' in rc_block
    assert "{out_abs}" in rc_block


# ── core/pipeline.py (CLI) integration ───────────────────────────────────────

def test_cli_shares_the_one_implementation():
    import services
    import pipeline as pl

    assert pl._services().derive_model_short_name is services.derive_model_short_name
    assert pl._services().ModelNameUnresolvedError is services.ModelNameUnresolvedError


def test_cli_training_is_genuinely_optional():
    """Foundry #3 regression pin (supersedes the old
    test_cli_training_is_structurally_always_enabled, which documented this
    exact behavior as an accepted limitation): a default/bare PipelineConfig
    no longer force-enables training. training is Optional[TrainingConfig]
    with no default_factory, so a config nobody explicitly configured for
    training has none, and _compute_enabled_stages() correctly reports it
    disabled -- for every other stage's config state, not just the "nothing
    else configured either" case this used to document."""
    import pipeline as pl

    config = pl.PipelineConfig()
    assert config.training is None
    assert "training" not in pl._compute_enabled_stages(config)
    config.export = config.heretic = config.reap = config.qat = None
    config.magicquant = config.rocmfpx = config.upload = None
    assert "training" not in pl._compute_enabled_stages(config)

    # And the positive case: a genuinely-configured training section IS
    # reported enabled -- pins the other half of the same gate.
    config.training = pl.TrainingConfig()
    assert "training" in pl._compute_enabled_stages(config)


def test_resolve_model_name_prefers_training_when_set(tmp_path):
    import pipeline as pl

    artifacts = pl.Artifacts(str(tmp_path / "out"))
    config = pl.PipelineConfig(training=pl.TrainingConfig(model_name="org/Llama-3-8B-BF16"))
    logs = []
    result = pl._resolve_model_name(
        config, artifacts, lambda msg, level="info": logs.append((msg, level)),
        source_model_field="magicquant", source=Path("/some/other/source"),
    )
    assert result == "Llama-3-8B"  # precision suffix stripped too -- new for the CLI
    assert logs == []


def test_resolve_model_name_falls_back_to_stage_source_when_training_name_empty(tmp_path):
    """The 'worse' half of the pre-existing CLI defect: config.training.
    model_name.split('/')[-1] had no fallback at all. Now, when training's
    name is empty, the calling stage's own already-resolved source wins."""
    import pipeline as pl

    artifacts = pl.Artifacts(str(tmp_path / "out"))
    config = pl.PipelineConfig(training=pl.TrainingConfig(model_name=""))
    config.rocmfpx = pl.ROCmFPXConfig()  # must be present for _compute_enabled_stages
    result = pl._resolve_model_name(
        config, artifacts, lambda *a, **k: None,
        source_model_field="rocmfpx", source=Path("/models/Some-Model-FP16"),
    )
    assert result == "Some-Model"


def test_resolve_model_name_falls_back_to_existing_output_dir(tmp_path):
    """CLI equivalent of the UI's re-run scenario: --output-dir already
    points at a populated run directory, training.model_name is blank, and
    no explicit source override was given for this stage."""
    import pipeline as pl

    out_dir = tmp_path / "SomeExistingRun"
    artifacts = pl.Artifacts(str(out_dir))
    (out_dir / "merged_model").mkdir()
    config = pl.PipelineConfig(training=pl.TrainingConfig(model_name=""))
    result = pl._resolve_model_name(
        config, artifacts, lambda *a, **k: None,
        source_model_field="magicquant", source=None,
    )
    assert result == "SomeExistingRun"


def test_resolve_model_name_returns_none_and_logs_instead_of_raising(tmp_path):
    """Matches this module's existing stage-failure convention (log + return
    False) rather than propagating an exception out of stage_magicquant/
    stage_rocmfpx."""
    import pipeline as pl

    artifacts = pl.Artifacts(str(tmp_path / "out"))
    config = pl.PipelineConfig(training=pl.TrainingConfig(model_name=""))
    logs = []
    result = pl._resolve_model_name(
        config, artifacts, lambda msg, level="info": logs.append((msg, level)),
        source_model_field="magicquant", source="???",  # sanitizes to empty
    )
    assert result is None
    assert logs and logs[0][1] == "error"


def test_stage_magicquant_strips_precision_suffix_from_gguf_model_name(tmp_path, monkeypatch):
    """End-to-end through the real stage function: the CLI's GGUF filename
    prefix must no longer carry a source precision tag (issue #6, now also
    fixed for the CLI path, which previously had zero suffix handling)."""
    import pipeline as pl
    import services as services_mod

    out_dir = tmp_path / "out"
    artifacts = pl.Artifacts(str(out_dir))
    # training must be explicitly constructed (Foundry #3: no longer a
    # non-optional default) so the training layer of the naming fallback
    # actually wins here, matching this test's original intent.
    config = pl.PipelineConfig(
        output_dir=str(out_dir),
        training=pl.TrainingConfig(model_name="org/Some-Model-BF16"),
    )
    config.magicquant.source_model = "unused-but-non-empty"

    captured = {}
    real_build_script = services_mod.MagicQuantService.build_script

    def _capture(self, **kwargs):
        captured.update(kwargs)
        return real_build_script(self, **kwargs)

    monkeypatch.setattr(services_mod.MagicQuantService, "build_script", _capture)

    def fake_run_stage_script(script, script_path, log, *, cfg_hash="", timeout=None, **kw):
        gguf = artifacts.magicquant_dir / "model-Q4.gguf"
        gguf.parent.mkdir(parents=True, exist_ok=True)
        gguf.write_bytes(b"x")
        return 0

    monkeypatch.setattr(pl, "_run_stage_script", fake_run_stage_script)

    ok = pl.stage_magicquant(config, artifacts, lambda *a, **k: None)
    assert ok is True
    assert captured["model_name"] == "Some-Model"


# ── JS <-> Python sync guard (ui/index.html suggestRepoId) ──────────────────

def test_js_precision_regex_matches_python_pattern():
    """ui/index.html::suggestRepoId() carries its own copy of the precision-
    suffix regex (JS can't import core/services.py). Guard against the two
    silently drifting apart."""
    html = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
    line = next(
        l for l in html.splitlines()
        if "short.replace" in l and "BF16" in l
    )
    m = re.search(r"/(.+)/i", line)
    assert m, f"could not find a /pattern/i regex literal in: {line!r}"
    assert m.group(1) == _PRECISION_SUFFIX_RE.pattern


def test_js_suggest_repo_id_does_not_gain_the_refuse_half():
    """Spec point 7: JS gets the precision strip only. A wrong suggestion is
    low-stakes (the operator can edit the field), so suggestRepoId() must
    keep its existing unconditional final else -- no raised/thrown error."""
    html = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
    start = html.index("function suggestRepoId()")
    window = html[start:start + 2000]
    assert "modelName = S.config.training.model_name;" in window  # unconditional final else, preserved
    assert "throw" not in window


# ── Spot-check every saved workflow (this box only) ──────────────────────────

WORKFLOWS_DIR = Path.home() / ".foundry" / "workflows"


def _build_kwargs_from_workflow(cfg: dict, enabled: set) -> dict:
    tc = cfg.get("training") or {}
    ec = cfg.get("export") or {}
    mc = cfg.get("magicquant") or {}
    rc = cfg.get("rocmfpx") or {}
    return dict(
        training_model_name=tc.get("model_name") or "",
        training_enabled="training" in enabled,
        export_source_model=ec.get("source_model") or "",
        export_enabled="export" in enabled,
        magicquant_source_model=mc.get("source_model") or "",
        magicquant_enabled="magicquant" in enabled,
        rocmfpx_source_model=rc.get("source_model") or "",
        rocmfpx_enabled="rocmfpx" in enabled,
    )


@pytest.mark.skipif(not WORKFLOWS_DIR.is_dir(), reason="no saved ~/.foundry/workflows on this box")
def test_spot_check_all_saved_workflows():
    """Spec ask: spot-check every saved workflow against the new derivation
    (the research pass that wrote the spec only checked 2 of however many
    exist) and report each result. Every file must EITHER resolve to a
    non-empty, precision-suffix-free name, OR cleanly raise
    ModelNameUnresolvedError -- never raise anything else, and never resolve
    to an empty/garbage string."""
    files = sorted(WORKFLOWS_DIR.glob("*.json"))
    assert files, "workflows dir exists but is empty"
    results = {}
    for f in files:
        d = json.loads(f.read_text())
        enabled = set(d.get("enabled_stages") or [])
        kwargs = _build_kwargs_from_workflow(d.get("config") or {}, enabled)
        try:
            results[f.name] = derive_model_short_name(**kwargs, output_dir=None)
        except ModelNameUnresolvedError as e:
            results[f.name] = f"UNRESOLVED: {e}"

    for fname, result in results.items():
        if result.startswith("UNRESOLVED"):
            continue
        assert result, f"{fname}: resolved to an empty name"
        assert not _PRECISION_SUFFIX_RE.search(result), (
            f"{fname}: resolved name {result!r} still carries a precision suffix"
        )
