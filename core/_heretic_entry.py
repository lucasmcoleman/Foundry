"""Importable entry module for the heretic abliteration stage (audit H2).

``HereticService.build_script`` emits a thin shim that writes a JSON config and
invokes ``core/_heretic_entry.py:run()``. The Optuna-driven abliteration search
(previously a ~240-line escaped f-string) lives here as ordinary Python so it is
IDE/lint/type friendly and the traceback points at a real file.

Module import is stdlib-only; heretic / optuna / torch are imported lazily inside
``run()`` so config parsing and trial selection are unit-testable without a GPU.

The GPT-OSS ``<|channel|>analysis`` response-prefix branch and the simplified
Pareto-min trial selection (audit L-heretic-deadloop) live in this module.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_ROCM_ENV = {
    "HSA_ENABLE_SDMA": "0",
    "PYTORCH_HIP_ALLOC_CONF": "backend:native,expandable_segments:True",
    "UNSLOTH_SKIP_TORCHVISION_CHECK": "1",
    "TORCH_ROCM_AOTRITON_ENABLE_EXPERIMENTAL": "1",
}


def parse_config(cfg_path: str) -> dict:
    return json.loads(Path(cfg_path).read_text())


def normalize_response_prefix(prefix: str) -> str:
    """Collapse a common response prefix to its canonical thinking-tag form.

    Carries the GPT-OSS ``<|channel|>analysis<|message|>`` branch (audit
    H1: was CLI-only) alongside the other reasoning-tag families. Pure string
    logic; unit-testable.
    """
    if prefix.startswith("<think>"):
        return "<think></think>"
    if prefix.startswith("<|channel|>analysis<|message|>"):
        return "<|channel|>analysis<|message|><|end|><|start|>assistant<|channel|>final<|message|>"
    if prefix.startswith("<thought>"):
        return "<thought></thought>"
    if prefix.startswith("[THINK]"):
        return "[THINK][/THINK]"
    return prefix


def select_best_trial(completed):
    """Return the best completed trial: fewest refusals, tie-broken by lowest KL.

    Simplified Pareto-min (audit L-heretic-deadloop): the old min_divergence /
    best_trials loop was dead — sorting ascending already puts the answer first.
    ``completed`` is any sequence of objects exposing ``.user_attrs`` with
    ``refusals`` and ``kl_divergence`` keys.
    """
    sorted_trials = sorted(
        completed, key=lambda t: (t.user_attrs["refusals"], t.user_attrs["kl_divergence"])
    )
    best = sorted_trials[0]
    return best


def run(cfg_path: str | None = None) -> None:
    import math
    import os
    import time
    import warnings

    for k, v in _ROCM_ENV.items():
        os.environ.setdefault(k, v)

    if cfg_path is None:
        cfg_path = sys.argv[1]
    cfg = parse_config(cfg_path)

    import torch
    import torch.nn.functional as F
    import optuna
    import transformers
    from optuna.samplers import TPESampler
    from optuna.storages import JournalStorage
    from optuna.storages.journal import JournalFileBackend, JournalFileOpenLock
    from optuna.study import StudyDirection
    from optuna.trial import TrialState
    from optuna.exceptions import ExperimentalWarning
    from optuna import TrialPruned
    from dataclasses import asdict
    from os.path import commonprefix

    from heretic.config import Settings
    from heretic.model import Model, AbliterationParameters
    from heretic.evaluator import Evaluator
    from heretic.utils import (
        empty_cache, format_duration, load_prompts, print_memory_usage,
    )

    # Use plain print for pipeline logging.
    import builtins
    _print = builtins.print
    from heretic import utils as _hu
    _hu.print = _print

    torch.set_grad_enabled(False)
    torch._dynamo.config.cache_size_limit = 64
    transformers.logging.set_verbosity_error()
    optuna.logging.set_verbosity(optuna.logging.WARNING)
    warnings.filterwarnings("ignore", category=ExperimentalWarning)

    model_path = cfg["model_path"]
    output_path = cfg["output_path"]
    checkpoint_dir = cfg["checkpoint_dir"]
    os.makedirs(checkpoint_dir, exist_ok=True)

    _print(f"Model: {model_path}")
    _print(f"Output: {output_path}")

    settings = Settings(
        model=model_path,
        quantization=cfg["quantization"],
        n_trials=cfg["n_trials"],
        n_startup_trials=cfg["n_startup_trials"],
        kl_divergence_scale=cfg["kl_divergence_scale"],
        orthogonalize_direction=cfg["orthogonalize_direction"],
        row_normalization=cfg["row_normalization"],
        study_checkpoint_dir=checkpoint_dir,
        batch_size=0,
    )

    model = Model(settings)
    _print()
    print_memory_usage()

    _print()
    _print(f"Loading good prompts from {settings.good_prompts.dataset}...")
    good_prompts = load_prompts(settings, settings.good_prompts)
    _print(f"  {len(good_prompts)} prompts loaded")

    _print()
    _print(f"Loading bad prompts from {settings.bad_prompts.dataset}...")
    bad_prompts = load_prompts(settings, settings.bad_prompts)
    _print(f"  {len(bad_prompts)} prompts loaded")

    # Auto batch size.
    if settings.batch_size == 0:
        _print()
        _print("Determining optimal batch size...")
        batch_size = 1
        best_batch_size = -1
        best_performance = -1
        while batch_size <= settings.max_batch_size:
            _print(f"  Trying batch size {batch_size}... ", end="")
            prompts = good_prompts * math.ceil(batch_size / len(good_prompts))
            prompts = prompts[:batch_size]
            try:
                model.get_responses(prompts)
                st = time.perf_counter()
                responses = model.get_responses(prompts)
                et = time.perf_counter()
            except Exception as error:
                if batch_size == 1:
                    raise
                _print(f"Failed ({error})")
                break
            rl = [len(model.tokenizer.encode(r)) for r in responses]
            perf = sum(rl) / (et - st)
            _print(f"Ok ({perf:.0f} tokens/s)")
            if perf > best_performance:
                best_batch_size = batch_size
                best_performance = perf
            batch_size *= 2
        settings.batch_size = best_batch_size
        _print(f"  Chosen batch size: {settings.batch_size}")

    # Check for common response prefix.
    _print()
    _print("Checking for common response prefix...")
    responses = model.get_responses_batched(good_prompts[:100] + bad_prompts[:100])
    model.response_prefix = normalize_response_prefix(commonprefix(responses).rstrip(" "))
    if model.response_prefix:
        _print(f"  Prefix found: {model.response_prefix!r}")
    else:
        _print("  None found")

    evaluator = Evaluator(settings, model)

    # Compute refusal directions.
    _print()
    _print("Calculating per-layer refusal directions...")
    _print("  Obtaining residuals for good prompts...")
    good_residuals = model.get_residuals_batched(good_prompts)
    _print("  Obtaining residuals for bad prompts...")
    bad_residuals = model.get_residuals_batched(bad_prompts)
    good_means = good_residuals.mean(dim=0)
    bad_means = bad_residuals.mean(dim=0)
    refusal_directions = F.normalize(bad_means - good_means, p=2, dim=1)
    if settings.orthogonalize_direction:
        good_directions = F.normalize(good_means, p=2, dim=1)
        proj = torch.sum(refusal_directions * good_directions, dim=1)
        refusal_directions = refusal_directions - proj.unsqueeze(1) * good_directions
        refusal_directions = F.normalize(refusal_directions, p=2, dim=1)
    del good_residuals, bad_residuals
    empty_cache()

    # Set up Optuna study.
    study_file = os.path.join(
        checkpoint_dir,
        "".join([(c if (c.isalnum() or c in ["_", "-"]) else "--") for c in settings.model]) + ".jsonl",
    )
    lock_obj = JournalFileOpenLock(study_file)
    backend = JournalFileBackend(study_file, lock_obj=lock_obj)
    storage = JournalStorage(backend)

    state = {"trial_index": 0, "start_index": 0}
    start_time = time.perf_counter()

    def objective(trial):
        state["trial_index"] += 1
        trial_index = state["trial_index"]
        trial.set_user_attr("index", trial_index)
        direction_scope = trial.suggest_categorical("direction_scope", ["global", "per layer"])
        last_layer = len(model.get_layers()) - 1
        direction_index = trial.suggest_float("direction_index", 0.4 * last_layer, 0.9 * last_layer)
        if direction_scope == "per layer":
            direction_index = None
        parameters = {}
        for comp in model.get_abliterable_components():
            mw = trial.suggest_float(f"{comp}.max_weight", 0.8, 1.5)
            mwp = trial.suggest_float(f"{comp}.max_weight_position", 0.6 * last_layer, 1.0 * last_layer)
            mnw = trial.suggest_float(f"{comp}.min_weight", 0.0, 1.0)
            mwd = trial.suggest_float(f"{comp}.min_weight_distance", 1.0, 0.6 * last_layer)
            parameters[comp] = AbliterationParameters(
                max_weight=mw, max_weight_position=mwp,
                min_weight=(mnw * mw), min_weight_distance=mwd,
            )
        trial.set_user_attr("direction_index", direction_index)
        trial.set_user_attr("parameters", {k: asdict(v) for k, v in parameters.items()})
        _print(f"\nRunning trial {trial_index} of {settings.n_trials}...")
        model.reset_model()
        model.abliterate(refusal_directions, direction_index, parameters)
        score, kl_div, refusals = evaluator.get_score()
        elapsed = time.perf_counter() - start_time
        remaining = (elapsed / (trial_index - state["start_index"])) * (settings.n_trials - trial_index)
        _print(f"  Elapsed: {format_duration(elapsed)}")
        if trial_index < settings.n_trials:
            _print(f"  Estimated remaining: {format_duration(remaining)}")
        trial.set_user_attr("kl_divergence", kl_div)
        trial.set_user_attr("refusals", refusals)
        return score

    def objective_wrapper(trial):
        try:
            return objective(trial)
        except KeyboardInterrupt:
            trial.study.stop()
            raise TrialPruned()

    study = optuna.create_study(
        sampler=TPESampler(n_startup_trials=settings.n_startup_trials, n_ei_candidates=128, multivariate=True),
        directions=[StudyDirection.MINIMIZE, StudyDirection.MINIMIZE],
        storage=storage, study_name="heretic", load_if_exists=True,
    )
    study.set_user_attr("settings", settings.model_dump_json())
    study.set_user_attr("finished", False)

    def count_done():
        return sum(1 for t in study.trials if t.state == TrialState.COMPLETE)

    state["start_index"] = state["trial_index"] = count_done()
    if state["start_index"] > 0:
        _print(f"\nResuming existing study ({state['start_index']} trials completed).")
    remaining = settings.n_trials - count_done()
    if remaining > 0:
        study.optimize(objective_wrapper, n_trials=remaining)
    if count_done() == settings.n_trials:
        study.set_user_attr("finished", True)

    # Select best trial from Pareto front (simplified — audit L-heretic-deadloop).
    completed = [t for t in study.trials if t.state == TrialState.COMPLETE]
    if not completed:
        _print("No completed trials")
        sys.exit(1)
    best = select_best_trial(completed)
    _print(
        f"\nSelected trial {best.user_attrs['index']}: "
        f"refusals={best.user_attrs['refusals']}, "
        f"KL={best.user_attrs['kl_divergence']:.4f}"
    )

    # Apply best trial and save.
    _print("Resetting model...")
    model.reset_model()
    _print("Abliterating with best parameters...")
    model.abliterate(
        refusal_directions,
        best.user_attrs["direction_index"],
        {k: AbliterationParameters(**v) for k, v in best.user_attrs["parameters"].items()},
    )
    _print("Saving merged abliterated model...")
    merged_model = model.get_merged_model()
    merged_model.save_pretrained(output_path)
    del merged_model
    empty_cache()
    model.tokenizer.save_pretrained(output_path)
    _print(f"Abliterated model saved to {output_path}")
    _print("PIPELINE_STAGE_COMPLETE=heretic")


if __name__ == "__main__":
    run()
