"""
Service layer for Foundry pipeline stages.

Extracts business logic from ui/app.py route handlers into testable,
reusable service classes. Each service accepts a config and a progress
callback, handles subprocess execution, and returns success/failure.

Usage from the UI:
    svc = TrainingService(project_root, venv_python)
    ok = await svc.run(cfg, run_script_fn)
"""

import json
import re
from pathlib import Path
from typing import Callable, Awaitable, Optional, Union

# Type alias for the async subprocess runner used by the UI.
# Signature: (script_text, output_dir) -> exit_code
RunScriptFn = Callable[[str, str], Awaitable[int]]

# Type alias for the async log callback used by the UI.
AsyncLogFn = Callable[[str, str], Awaitable[None]]


def _entry_shim(entry_module: str, cfg: dict, pipeline_root: Path) -> str:
    """Return a small shim that writes ``cfg`` as JSON and calls an entry module.

    The shim is intentionally tiny and config-driven: the CLI and the UI produce
    a byte-identical shim for the same module, and all stage logic lives in the
    importable ``core/<entry_module>.py`` (audit H2). The JSON config is embedded
    as a repr'd string and written next to the running script, then passed to
    ``run()``.
    """
    core_path = repr(str(Path(pipeline_root) / "core"))
    cfg_json = repr(json.dumps(cfg, indent=2, sort_keys=True))
    return (
        "import sys, json, os\n"
        "from pathlib import Path\n"
        f"sys.path.insert(0, {core_path})\n"
        f"_cfg_path = Path(__file__).with_name({repr(entry_module + '.cfg.json')})\n"
        f"_cfg_path.write_text({cfg_json})\n"
        f"import {entry_module}\n"
        f"{entry_module}.run(str(_cfg_path))\n"
    )


# ── Model-name derivation (shared by ui/app.py and core/pipeline.py) ────────
#
# A run directory name and every GGUF filename prefix come from one of these:
# training's model, or -- when training didn't produce this run's model --
# the explicit source_model an operator points a later stage at directly.
# Getting this wrong is silent and expensive: a wrong run-directory name
# either collides with an unrelated model's output or gets created fresh and
# empty, and a wrong GGUF filename prefix ships a file whose name lies about
# its own precision (e.g. a Q4 file suffixed "-BF16").

# Upstream repo IDs often carry the source precision as a trailing tag
# (`...-A3B-BF16`). A quantized artifact must not inherit that tag in its own
# name. Matches the FINAL path segment only -- "Foo-BF16-Instruct" must not
# be touched, only a tag that is the actual end of the name.
# Trailing precision tokens to strip when deriving a model name, so a Q4 file
# stops advertising itself as BF16 (issue #6: ...-A3B-BF16-Q4_K_M.gguf).
#
# FP8/F8 are deliberately NOT in this class. For several real upstream repos
# FP8 is the model's IDENTITY, not a redundant suffix -- nvidia/
# Llama-3.3-70B-Instruct-FP8 and deepseek-ai/DeepSeek-V3-FP8 are distinct
# published models from their BF16 originals, and stripping it would merge
# them into one run directory and one GGUF prefix. There is also no upside:
# the writer rejects a pre-quantized source outright, so an FP8 repo never
# reaches the naming path as a quantization source anyway.
_PRECISION_SUFFIX_RE = re.compile(
    r"[-_](?:BF16|FP16|F16|FP32|F32)$", re.IGNORECASE
)

# Names that carry no model identity at all. Foundry writes `model-bf16.gguf`
# into EVERY run directory, and pointing MagicQuant at a GGUF source is a
# documented workflow (--magicquant-source-model <file.gguf>), so stripping
# the extension and then the precision token turns that into the bare word
# "model" -- which would give every such run the same directory and the same
# GGUF prefix. Keep the pre-strip name when the strip would leave one of
# these; `model-bf16` is ugly but unique, `model` is a collision.
_NON_IDENTIFYING_NAMES = frozenset({"model", "models", "output", "gguf", "merged"})

# Basenames Foundry itself writes into EVERY run directory. A quant-only
# workflow points magicquant.source_model at one of these, and deriving the
# run name from the basename then gives every model on the box the same
# directory and the same GGUF prefix ("model-bf16", "merged_model", ...).
# These are artifact names, not model identities -- when one is the final
# path segment, the model's identity is its PARENT directory.
_FOUNDRY_ARTIFACT_BASENAMES = frozenset({
    "model-bf16", "model-bf16-nomtp", "model-f16", "model",
    "merged_model", "reap_model", "heretic_model",
})

# Directories/files that mark an output directory as belonging to a real,
# already-executed run. Mirrors the exact artifact set core.reap_common.
# resolve_artifact_source() and ui/app.py's validate_pipeline() already treat
# as "this stage's upstream artifacts exist" -- kept as one definition so a
# fourth marker never gets added in only one of the three places.
_RUN_ARTIFACT_MARKERS = ("reap_model", "heretic_model", "merged_model", "model-bf16.gguf")
# Stage output directories identified by containing at least one *.gguf,
# rather than by a fixed name -- MagicQuant/ROCmFPX write multiple tier files.
_RUN_ARTIFACT_GLOB_DIRS = ("magicquant", "rocmfpx")


class ModelNameUnresolvedError(RuntimeError):
    """No stage config (and no existing run directory) yields a usable model
    name for this run. Raised instead of silently falling back to a stale or
    default value -- callers must abort before creating any directory, never
    mkdir a wrongly-named one."""


def _has_run_artifacts(path: Path) -> bool:
    """True if ``path`` looks like a real (at least partially executed) run
    directory -- i.e. it has any of the artifacts a pipeline stage produces."""
    if not path.is_dir():
        return False
    if any((path / marker).exists() for marker in _RUN_ARTIFACT_MARKERS):
        return True
    # NOTE: `.glob()` returns a (truthy) generator regardless of whether it
    # yields anything -- `any(p.glob(...) for ...)` would always be True.
    # Each glob must itself be consumed by `any()` to test for a real match.
    return any(any((path / sub).glob("*.gguf")) for sub in _RUN_ARTIFACT_GLOB_DIRS)


def _existing_run_basename(output_dir: Union[str, Path]) -> Optional[str]:
    """The one safe last-resort model-name fallback: an already-populated run
    directory, so a standalone component re-run (e.g. magicquant-only against
    a completed run) resolves to its own prior output instead of a stale
    config field. Never guesses among ambiguous candidates.

    Checks ``output_dir`` ITSELF ONLY. It deliberately does not look at
    sibling directories.

    An earlier version scanned the immediate children and accepted the answer
    when exactly one of them had run artifacts, on the theory that "exactly
    one" means "unambiguous". It does not. On this box `output/` currently
    holds exactly one child with artifacts, because the 08-12 cleanup deleted
    the rest -- so every workflow narrowed to ["magicquant","upload"] resolved
    to that one unrelated model's 346 GB run directory. `validate_pipeline`
    then PASSED, because the directory genuinely contains a merged model, and
    the pipeline would have quantized that model and uploaded it under the
    loaded workflow's repo_id. Issue #5 at least failed loudly on an empty
    directory; guessing a sibling fails silently and does work, publishing the
    wrong model. "Exactly one candidate" was a property of disk cleanup state,
    not a safety guarantee, and it can become true again at any time.

    If a caller genuinely needs the shared-base-directory behaviour, it must
    pass the model-specific directory instead of asking this to infer it.
    """
    base = Path(output_dir)
    if not base.is_dir():
        return None
    if _has_run_artifacts(base):
        return base.name
    return None


def _strip_known_extension(name: str) -> str:
    for ext in (".gguf", ".safetensors", ".bin", ".pt", ".pth"):
        if name.lower().endswith(ext):
            return name[: -len(ext)]
    return name


def derive_model_short_name(
    *,
    training_model_name: str = "",
    training_enabled: bool = False,
    export_source_model: str = "",
    export_enabled: bool = False,
    magicquant_source_model: str = "",
    magicquant_enabled: bool = False,
    rocmfpx_source_model: str = "",
    rocmfpx_enabled: bool = False,
    output_dir: Optional[Union[str, Path]] = None,
) -> str:
    """Derive a short, filesystem-safe model name for run-directory naming
    and GGUF filename prefixes. ONE implementation shared by ui/app.py and
    core/pipeline.py so the two orchestrators can't drift apart again.

    Layered fallback, in pipeline order -- the first stage that is both
    enabled for this run AND has a usable name wins:
    training.model_name -> export.source_model -> magicquant.source_model ->
    rocmfpx.source_model -> an already-populated run directory under
    ``output_dir`` (see ``_existing_run_basename``).

    Raises ModelNameUnresolvedError when nothing resolves -- callers must
    treat that as a clean abort and must NOT create a directory first.
    """
    raw = ""
    if training_enabled and training_model_name:
        raw = training_model_name
    elif export_enabled and export_source_model:
        raw = export_source_model
    elif magicquant_enabled and magicquant_source_model:
        raw = magicquant_source_model
    elif rocmfpx_enabled and rocmfpx_source_model:
        raw = rocmfpx_source_model

    if not raw:
        existing = _existing_run_basename(output_dir) if output_dir is not None else None
        if existing:
            # Fall through to the SAME normalization every other layer gets.
            # Returning it raw here meant issue #6 was unfixed on precisely the
            # path issue #5 travels: a re-run resolving through this fallback
            # got its "-BF16" back.
            raw = existing
        else:
            where = f" under {output_dir}" if output_dir is not None else ""
            raise ModelNameUnresolvedError(
                "Could not determine a model name for this run: no enabled "
                "stage provided a source model, and no existing run artifacts "
                f"were found{where}. Set a Source Model "
                "(Export/MagicQuant/ROCmFPX) or enable Training."
            )

    segments = [seg for seg in raw.rstrip("/").split("/") if seg]
    name = _strip_known_extension(segments[-1] if segments else "")
    # A quant-only workflow points magicquant.source_model at an artifact
    # INSIDE a run directory -- model-bf16.gguf, merged_model, ... -- and every
    # run directory on the box contains identically-named ones. Deriving from
    # that basename gives every model the same run directory and the same GGUF
    # prefix. When the final segment is a known Foundry artifact, the model's
    # identity is its PARENT directory, so use that instead.
    # ...but only when the parent is itself identifying. `/runs/model-bf16.gguf`
    # must NOT become "runs": walking up blindly just moves the collision one
    # level, from every-model-is-"model-bf16" to every-model-is-"runs". If the
    # parent is no better, keep the artifact basename -- ugly and unique beats
    # short and colliding, same rule as _NON_IDENTIFYING_NAMES.
    if name.lower() in _FOUNDRY_ARTIFACT_BASENAMES and len(segments) >= 2:
        parent = _strip_known_extension(segments[-2])
        if parent and parent.lower() not in _NON_IDENTIFYING_NAMES | _FOUNDRY_ARTIFACT_BASENAMES:
            name = parent
    stripped = _PRECISION_SUFFIX_RE.sub("", name)
    # Only take the strip if what's left still identifies a model -- see
    # _NON_IDENTIFYING_NAMES for why `model-bf16.gguf` must not become `model`.
    if stripped.lower() not in _NON_IDENTIFYING_NAMES:
        name = stripped
    sanitized = "".join(c if c.isalnum() or c in "-_." else "-" for c in name).strip("-")
    if not sanitized:
        raise ModelNameUnresolvedError(
            f"Resolved model name {raw!r} sanitizes to an empty string."
        )
    return sanitized


class TrainingService:
    """Orchestrates the QLoRA training subprocess."""

    def __init__(self, pipeline_root: Path, venv_python: str) -> None:
        self.pipeline_root = pipeline_root
        self.venv_python = venv_python

    def build_config(
        self,
        *,
        model_name: str,
        datasets: list[str] = None,
        dataset_path: str = "",
        output_dir: str,
        max_seq_length: int,
        lora_r: int,
        lora_alpha: int,
        lora_dropout: float,
        use_rslora: bool,
        num_train_epochs: int,
        per_device_train_batch_size: int,
        gradient_accumulation_steps: int,
        learning_rate: float,
        lr_scheduler_type: str,
        warmup_steps: Optional[int] = None,
        warmup_ratio: Optional[float] = None,
        optim: str,
        packing: bool = False,
    ) -> dict:
        """Build the JSON-serializable config consumed by core/_train_entry.py.

        Warmup: pass ``warmup_ratio`` (preferred, used by both CLI and UI) and/or
        ``warmup_steps``. When ``warmup_ratio`` is set it takes precedence and
        ``warmup_steps`` is dropped; at least one must be provided (audit
        M-warmup). The same config from CLI and UI yields the same entry-module
        invocation, so produced adapters match.
        """
        if warmup_ratio is None and warmup_steps is None:
            raise ValueError("TrainingService.build_config requires warmup_ratio or warmup_steps")
        sources = datasets if datasets else ([dataset_path] if dataset_path else None)
        return {
            "pipeline_root": str(self.pipeline_root),
            "model_name": model_name,
            "datasets": list(sources) if sources else None,
            "output_dir": output_dir,
            "max_seq_length": max_seq_length,
            "lora_r": lora_r,
            "lora_alpha": lora_alpha,
            "lora_dropout": lora_dropout,
            "use_rslora": use_rslora,
            "num_train_epochs": num_train_epochs,
            "per_device_train_batch_size": per_device_train_batch_size,
            "gradient_accumulation_steps": gradient_accumulation_steps,
            "learning_rate": learning_rate,
            "lr_scheduler_type": lr_scheduler_type,
            # Ratio wins: drop warmup_steps entirely when a ratio is given.
            "warmup_ratio": warmup_ratio,
            "warmup_steps": None if warmup_ratio is not None else warmup_steps,
            "optim": optim,
            "packing": packing,
        }

    def build_script(self, **kwargs) -> str:
        """Generate the training subprocess shim.

        The shim writes the JSON config (built by :meth:`build_config`) next to
        itself and invokes the importable entry module ``core/_train_entry.py``.
        All real logic — dataset format normalization, kbit prep, LoRA setup,
        SFTConfig construction — lives in that module so it is IDE/lint/type
        friendly and unit-testable without a GPU (audit H2).
        """
        cfg = self.build_config(**kwargs)
        return _entry_shim("_train_entry", cfg, self.pipeline_root)


class ExportService:
    """Orchestrates the streaming LoRA merge / export subprocess."""

    def __init__(self, pipeline_root: Path, venv_python: str) -> None:
        self.pipeline_root = pipeline_root
        self.venv_python = venv_python

    def build_config(
        self,
        *,
        base_model_id: str,
        lora_source: Optional[str],
        has_lora: bool,
        merged_dir: str,
    ) -> dict:
        """Build the JSON config consumed by core/_export_entry.py."""
        return {
            "pipeline_root": str(self.pipeline_root),
            "base_model_id": base_model_id,
            "lora_source": lora_source,
            "has_lora": has_lora,
            "merged_dir": merged_dir,
        }

    def build_script(self, **kwargs) -> str:
        """Generate the export subprocess shim (calls core/_export_entry.py)."""
        cfg = self.build_config(**kwargs)
        return _entry_shim("_export_entry", cfg, self.pipeline_root)


class HereticService:
    """Orchestrates the heretic abliteration subprocess."""

    def __init__(self, pipeline_root: Path, venv_python: str) -> None:
        self.pipeline_root = pipeline_root
        self.venv_python = venv_python

    def build_config(
        self,
        *,
        model_path: str,
        output_path: str,
        checkpoint_dir: str,
        n_trials: int,
        n_startup_trials: int,
        quantization: str,
        kl_divergence_scale: float,
        orthogonalize_direction: bool,
        row_normalization: str,
    ) -> dict:
        """Build the JSON config consumed by core/_heretic_entry.py."""
        return {
            "pipeline_root": str(self.pipeline_root),
            "model_path": model_path,
            "output_path": output_path,
            "checkpoint_dir": checkpoint_dir,
            "n_trials": n_trials,
            "n_startup_trials": n_startup_trials,
            "quantization": quantization,
            "kl_divergence_scale": kl_divergence_scale,
            "orthogonalize_direction": orthogonalize_direction,
            "row_normalization": row_normalization,
        }

    def build_script(self, **kwargs) -> str:
        """Generate the heretic subprocess shim (calls core/_heretic_entry.py)."""
        cfg = self.build_config(**kwargs)
        return _entry_shim("_heretic_entry", cfg, self.pipeline_root)



class ReapService:
    """Orchestrates the REAP expert pruning subprocess.

    REAP (Router-weighted Expert Activation Pruning) prunes experts from MoE
    models. Its own pyproject.toml pins torch==2.7.1 / transformers==4.55.0 /
    vllm==0.10.0, which would break Foundry's ROCm stack. So we import
    ``reap.prune`` by adding its ``src/`` to ``sys.path`` and stubbing every
    heavy optional dependency (vllm, lm_eval, evalplus, lcb_runner, crfm_helm,
    evalscope, uvloop, deepspeed, wandb) at subprocess startup.

    REAP writes its output to a path like
    ``artifacts/<model>/<dataset>/pruned_models/<method>-seed_<seed>-<ratio>/``
    relative to cwd. We chdir into the Foundry output directory so that
    relative path lands inside the run, then move the pruned model into
    ``<output_dir>/reap_model/``.
    """

    def __init__(self, pipeline_root: Path, venv_python: str) -> None:
        self.pipeline_root = pipeline_root
        self.venv_python = venv_python

    def build_config(
        self,
        *,
        input_dir: str,     # heretic_model or merged_model path (absolute)
        output_dir: str,    # absolute path where reap_model should end up
        cwd_dir: str,       # working directory for REAP's relative artifact paths
        compression_ratio: float,
        prune_method: str,
        samples_per_category: int,
        model_max_length: int,
        dataset_name: str,
        seed: int,
        observer_only: bool = False,
    ) -> dict:
        """Build the JSON config consumed by core/_reap_entry.py.

        ``observer_only`` (opt-in, default False): stops REAP after the
        calibration/observation pass -- skips pruning, saving, and eval
        entirely. Maps to ``reap.prune``'s own ``--run_observer_only`` flag.
        For huge MoE models (e.g. Laguna-S) whose full-precision weights
        don't fit in memory, this is meant to pair with a 4-bit calibration
        load (``REAP_LOAD_IN_4BIT=1``, see reap.model_util) and/or an
        unfused-expert architecture (see reap.laguna_unfused) upstream of
        this entry point: observe cheaply, then prune out-of-band via
        streaming safetensors surgery driven by the saved observations,
        rather than pruning the same (possibly quantized/patched) in-memory
        model REAP just calibrated.
        """
        return {
            "pipeline_root": str(self.pipeline_root),
            "input_dir": input_dir,
            "output_dir": output_dir,
            "cwd_dir": cwd_dir,
            "compression_ratio": compression_ratio,
            "prune_method": prune_method,
            "samples_per_category": samples_per_category,
            "model_max_length": model_max_length,
            "dataset_name": dataset_name,
            "seed": seed,
            "observer_only": observer_only,
        }

    def build_script(self, **kwargs) -> str:
        """Generate the REAP subprocess shim (calls core/_reap_entry.py)."""
        cfg = self.build_config(**kwargs)
        return _entry_shim("_reap_entry", cfg, self.pipeline_root)



class MagicQuantService:
    """Orchestrates the MagicQuant evolutionary search subprocess."""

    def __init__(self, pipeline_root: Path, venv_python: str) -> None:
        self.pipeline_root = pipeline_root
        self.venv_python = venv_python

    def build_config(
        self,
        *,
        llamacpp_hint: str,
        pipeline_root_str: str,
        mq_source_override: str,
        out_abs_str: str,
        generations: int,
        population_size: int,
        target_base_quant: str,
        tiers_json: str,
        model_name: str,
        verify: bool = False,
        measured: bool = False,
        measurement_rounds: int = 3,
        rocmfpx_schemes: bool = False,
        iq_schemes: bool = False,
        seed: Optional[int] = None,
        use_imatrix: bool = True,
        imatrix_corpus: Optional[str] = None,
        enable_kl: bool = True,
        kl_weight: float = 0.1,
        enable_speed_bench: bool = False,
        measurement_chunks: Optional[int] = None,
        stream_aware: bool = False,
        head_aggressive: bool = False,
        # speed_aware: None (default) means "no explicit Foundry choice" --
        # the emitted config carries speed_aware=null, and
        # core/_magicquant_entry.py's run() omits the kwarg entirely so
        # MagicQuantOrchestrator.run_measured_search's OWN default (True as
        # of the 2026-07 fix) actually takes effect. A caller (CLI flag or
        # UI toggle) that explicitly passes True/False here still wins --
        # this is only about what happens when nobody asked for anything.
        # Previously this defaulted to a hardcoded False that was ALWAYS
        # forwarded, silently pinning every real run to speed_aware=False
        # regardless of what the library's own default became -- see
        # test_entry_speed_aware_omitted_lets_library_default_apply in
        # tests/test_magicquant_knobs.py for the regression this guards.
        speed_aware: Optional[bool] = None,
        speed_metric: str = "bytes",
        speed_weight: Optional[float] = None,
        use_bytes_tps: bool = False,
        calibration_source: str = "",
        write_calibration: bool = False,
        allow_dequant_source: bool = False,
        budget_gib: Optional[float] = None,
    ) -> dict:
        """Build the JSON config consumed by core/_magicquant_entry.py."""
        if budget_gib is not None and measured:
            raise ValueError(
                "--magicquant-budget-gib and --magicquant-measured are mutually "
                "exclusive: the v2 budget search verifies with real perplexity "
                "itself, so 'measured' would misrepresent what ran."
            )
        return {
            "pipeline_root": str(self.pipeline_root),
            "llamacpp_hint": llamacpp_hint,
            "pipeline_root_str": pipeline_root_str,
            "mq_source_override": mq_source_override,
            "out_abs_str": out_abs_str,
            "generations": generations,
            "population_size": population_size,
            "target_base_quant": target_base_quant,
            "tiers_json": tiers_json,
            "model_name": model_name,
            "verify": verify,
            "measured": measured,
            "measurement_rounds": measurement_rounds,
            "rocmfpx_schemes": rocmfpx_schemes,
            "iq_schemes": iq_schemes,
            "seed": seed,
            "use_imatrix": use_imatrix,
            "imatrix_corpus": imatrix_corpus,
            "enable_kl": enable_kl,
            "kl_weight": kl_weight,
            "enable_speed_bench": enable_speed_bench,
            "measurement_chunks": measurement_chunks,
            "stream_aware": stream_aware,
            "head_aggressive": head_aggressive,
            "speed_aware": speed_aware,
            "speed_metric": speed_metric,
            "speed_weight": speed_weight,
            "use_bytes_tps": use_bytes_tps,
            "calibration_source": calibration_source,
            "write_calibration": write_calibration,
            "allow_dequant_source": allow_dequant_source,
            "budget_gib": budget_gib,
        }

    def build_script(self, **kwargs) -> str:
        """Generate the MagicQuant subprocess shim (calls _magicquant_entry.py)."""
        cfg = self.build_config(**kwargs)
        return _entry_shim("_magicquant_entry", cfg, self.pipeline_root)


class QATService:
    """Orchestrates the QAT-LoRA subprocess.

    Quantization-Aware Training fine-tunes LoRA adapters that are robust to
    MagicQuant's per-group hybrid quant config (read from a prior search's
    ``search_results.json``). The heavy work lives in MagicQuant's
    ``magicquant.qat.run_qat``; this service builds the JSON config that the
    importable ``core/_qat_entry.py`` consumes — one source of truth for CLI + UI.

    Audited against the 2026-07 tier-semantics fix (magicquant.quant.tiers'
    TIER_SCHEME_VERSION): ``tier`` below is passed straight through as an
    opaque dict key into WHATEVER ``search_results.json`` ``config_path``
    points at (``magicquant.qat.config.load_hybrid_config`` does the actual
    ``tiered[tier]["config"]`` lookup). That lookup is self-consistent and
    version-agnostic -- it always reads the tier under the SAME file it was
    requested from, never assumes "Q5" means the same thing across
    different runs/files -- so no change was needed here. The compatibility
    read path (warns, but still loads, when ``config_path`` predates
    ``tier_scheme_version``) lives in ``load_hybrid_config`` itself.
    """

    def __init__(self, pipeline_root: Path, venv_python: str) -> None:
        self.pipeline_root = pipeline_root
        self.venv_python = venv_python

    def build_config(
        self,
        *,
        model: str,
        config_path: str,   # path to the prior search's search_results.json
        tier: str,
        dataset: str,
        out: str,
        lora_r: int,
        lora_alpha: float,
        epochs: float,
        max_steps: int,
        lr: float,
        max_seq_len: int,
    ) -> dict:
        """Build the JSON config consumed by core/_qat_entry.py.

        The keys mirror ``magicquant.qat.run_qat``'s contract exactly (model,
        config + tier, dataset, out, and the LoRA/training hyperparams); the entry
        module strips ``pipeline_root`` before dispatch.
        """
        return {
            "pipeline_root": str(self.pipeline_root),
            "model": model,
            "config": config_path,
            "tier": tier,
            "dataset": dataset,
            "out": out,
            "lora_r": lora_r,
            "lora_alpha": lora_alpha,
            "epochs": epochs,
            "max_steps": max_steps,
            "lr": lr,
            "max_seq_len": max_seq_len,
        }

    def build_script(self, **kwargs) -> str:
        """Generate the QAT subprocess shim (calls core/_qat_entry.py)."""
        cfg = self.build_config(**kwargs)
        return _entry_shim("_qat_entry", cfg, self.pipeline_root)


class ROCmFPXService:
    """Orchestrates the ROCmFPX (AMD-tuned GGUF quant family) subprocess.

    ROCmFPX (https://github.com/ciru-ai/ROCmFPX) is a llama.cpp fork adding
    the ROCmFP3/4/6/8 quant types; it is a native build (git clone + compile),
    not a pip package. The heavy work (discovery/auto-install, BF16 GGUF
    conversion, per-format quantize) lives in the importable
    ``core/_rocmfpx_entry.py`` -- this service builds the JSON config that
    module consumes, the same shape as ``MagicQuantService``.
    """

    def __init__(self, pipeline_root: Path, venv_python: str) -> None:
        self.pipeline_root = pipeline_root
        self.venv_python = venv_python

    def build_config(
        self,
        *,
        rocmfpx_hint: str,
        pipeline_root_str: str,
        source_override: str,
        out_abs_str: str,
        formats_json: str,
        model_name: str,
        imatrix: str = "",
        allow_requantize: bool = False,
        allow_partial: bool = False,
    ) -> dict:
        """Build the JSON config consumed by core/_rocmfpx_entry.py."""
        return {
            "pipeline_root": str(self.pipeline_root),
            "rocmfpx_hint": rocmfpx_hint,
            "pipeline_root_str": pipeline_root_str,
            "source_override": source_override,
            "out_abs_str": out_abs_str,
            "formats_json": formats_json,
            "model_name": model_name,
            "imatrix": imatrix,
            "allow_requantize": allow_requantize,
            "allow_partial": allow_partial,
        }

    def build_script(self, **kwargs) -> str:
        """Generate the ROCmFPX subprocess shim (calls _rocmfpx_entry.py)."""
        cfg = self.build_config(**kwargs)
        return _entry_shim("_rocmfpx_entry", cfg, self.pipeline_root)


class UploadService:
    """Orchestrates the HuggingFace upload subprocess."""

    def __init__(self, pipeline_root: Path, venv_python: str) -> None:
        self.pipeline_root = pipeline_root
        self.venv_python = venv_python

    def build_config(
        self,
        *,
        repo_id: str,
        private: bool,
        license_id: str,
        upload_gguf: bool,
        upload_lora: bool,
        upload_merged: bool,
        upload_dataset: bool,
        base_model: str,
        dataset_name: str,
        did_training: bool = True,
        did_heretic: bool = False,
        did_reap: bool = False,
        did_magicquant: bool = True,
        lora_r: int,
        lora_alpha: int,
        lora_dropout: float,
        num_epochs: int,
        learning_rate: float,
        max_seq_length: int,
        batch_size: int,
        gradient_accumulation: int,
        optimizer: str,
        lr_scheduler: str,
        out_abs: str,
    ) -> dict:
        """Build the JSON config consumed by core/_upload_entry.py.

        ``license_id`` maps to the HFUploadConfig ``license`` field.
        """
        return {
            "pipeline_root": str(self.pipeline_root),
            "repo_id": repo_id,
            "private": private,
            "license": license_id,
            "upload_gguf": upload_gguf,
            "upload_lora": upload_lora,
            "upload_merged": upload_merged,
            "upload_dataset": upload_dataset,
            "base_model": base_model,
            "dataset_name": dataset_name,
            "did_training": did_training,
            "did_heretic": did_heretic,
            "did_reap": did_reap,
            "did_magicquant": did_magicquant,
            "lora_r": lora_r,
            "lora_alpha": lora_alpha,
            "lora_dropout": lora_dropout,
            "num_epochs": num_epochs,
            "learning_rate": learning_rate,
            "max_seq_length": max_seq_length,
            "batch_size": batch_size,
            "gradient_accumulation": gradient_accumulation,
            "optimizer": optimizer,
            "lr_scheduler": lr_scheduler,
            "out_abs": out_abs,
        }

    def build_script(self, **kwargs) -> str:
        """Generate the upload subprocess shim (calls core/_upload_entry.py)."""
        cfg = self.build_config(**kwargs)
        return _entry_shim("_upload_entry", cfg, self.pipeline_root)
