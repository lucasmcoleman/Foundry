"""Importable entry module for the MagicQuant evolutionary-quantization stage (H2).

``MagicQuantService.build_script`` emits a thin shim that writes a JSON config
and invokes ``core/_magicquant_entry.py:run()``. The heavy work (llama.cpp
discovery/auto-install, evolutionary search via MagicQuantOrchestrator) lives
here as ordinary Python.

Module import is stdlib-only; the MagicQuant package is imported lazily inside
``run()`` so config/source-resolution helpers are unit-testable without it.

llama.cpp auto-install is pinned to a known-good release tag (audit
L-supply-chain): ``LLAMACPP_PIN`` + ``--branch`` rather than a bare
default-branch clone.

``LLAMACPP_REPO``/``LLAMACPP_PIN`` are defined here (this module is
stdlib-only importable) and re-exported by ``core/pipeline.py``, which
imports them rather than keeping a second hand-typed copy -- both call
sites' llama.cpp auto-install must agree on exactly the same pin, and a
second literal is a drift hazard the moment one gets bumped without the
other. Bump the pin only here; pipeline.py picks it up automatically.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import ppl_smoke

LLAMACPP_REPO = "https://github.com/ggml-org/llama.cpp.git"
LLAMACPP_PIN = "gguf-v0.19.0"  # known-good release tag; bump deliberately


def parse_config(cfg_path: str) -> dict:
    return json.loads(Path(cfg_path).read_text())


def apply_dequant_env(cfg: dict, environ) -> bool:
    """Propagate ``allow_dequant_source`` into MAGICQUANT_ALLOW_DEQUANT_SOURCE.

    MagicQuant normally hard-requires BF16/F16/F32 source weights; the env var
    opts every ``open_model_source`` call site (and any subprocess that
    inherits ``environ``) into dequantizing an already-quantized GGUF source
    instead. A no-op (returns False, ``environ`` untouched) unless
    ``cfg["allow_dequant_source"]`` is truthy. stdlib-only and pure w.r.t.
    ``environ`` (a plain dict works for tests) so it's unit-testable without
    the magicquant package.
    """
    if not cfg.get("allow_dequant_source"):
        return False
    environ["MAGICQUANT_ALLOW_DEQUANT_SOURCE"] = "1"
    return True


def find_llamacpp(hint: str = "") -> str | None:
    """Return a llama.cpp dir that contains the converter or quantize binary.

    Accepts the same layouts LlamaCppTools itself searches (repo root with
    build/bin, a standalone build dir with bin/, or a bare bin dir) so a
    user-supplied hint like a ROCmFPX build directory isn't silently
    rejected in favor of an auto-detected -- possibly incompatible -- build.
    """
    import os

    # Prefer ROCmFPX fork builds over stock llama.cpp: they measure everything
    # stock can, auto-offload to GPU, and are the only builds that handle the
    # rocmfp* types this pipeline exists to produce. Stock stays as fallback
    # (it tracks upstream master, so a brand-new arch may load there first --
    # override with the explicit hint / LLAMACPP_PATH when that happens).
    fork_builds = sorted(
        str(d) for d in (Path.home() / "ROCmFPX").glob("build-*")
        if (d / "bin" / "llama-quantize").exists()
    )
    candidates = [hint, os.environ.get("LLAMACPP_PATH", ""), *fork_builds,
                  str(Path.home() / "llama.cpp"), "./llama.cpp", "/usr/local"]
    for p in candidates:
        if not p:
            continue
        pp = Path(p)
        for sub in [pp / "convert_hf_to_gguf.py",
                    pp / "build" / "bin" / "llama-quantize",
                    pp / "bin" / "llama-quantize",
                    pp / "llama-quantize"]:
            if sub.exists():
                return str(pp)
        if p == hint:
            print(f"WARNING: llamacpp path hint {hint!r} has no "
                  "convert_hf_to_gguf.py or llama-quantize (searched ., bin/, "
                  "build/bin/) -- falling back to auto-detection", flush=True)
    return None


def ensure_llamacpp(hint: str = "") -> str | None:
    """Find llama.cpp, auto-installing a pinned build if absent.

    Returns the install path, or None if discovery + install both failed (the
    orchestrator then falls back to heuristic probing).
    """
    import multiprocessing
    import subprocess

    llamacpp = find_llamacpp(hint)
    if llamacpp:
        return llamacpp

    install_dir = Path.home() / "llama.cpp"
    print("llama.cpp not found — auto-installing...", flush=True)
    rc = subprocess.run(
        ["git", "clone", "--depth", "1", "--branch", LLAMACPP_PIN,
         LLAMACPP_REPO, str(install_dir)]
    ).returncode
    if rc == 0:
        build_dir = install_dir / "build"
        rc = subprocess.run(
            ["cmake", "-B", str(build_dir), "-DCMAKE_BUILD_TYPE=Release", str(install_dir)]
        ).returncode
        if rc == 0:
            jobs = str(multiprocessing.cpu_count())
            rc = subprocess.run(["cmake", "--build", str(build_dir), "-j", jobs]).returncode
    if rc == 0:
        print(f"llama.cpp installed: {install_dir}", flush=True)
        return str(install_dir)
    print("Warning: llama.cpp install failed, using heuristic probing", flush=True)
    return None


def _disclose_if_lora_adapter_dir(c: Path) -> None:
    """Loudly flag when a resolve_source() candidate is a LoRA ADAPTER
    directory (adapter_model.safetensors + adapter_config.json), not a
    merged model.

    INCIDENT (rsLoRA audit, 2026-07-28): resolve_source()'s plain-
    safetensors-dir fallback (``any(c.glob("*.safetensors"))``) accepts an
    adapter directory too -- ``adapter_model.safetensors`` IS a
    ``*.safetensors`` file. Without this check, a user who forgot to run the
    export stage (or pointed ``--magicquant-source``/the UI source override
    straight at a training checkpoint dir) got silently routed into
    MagicQuant's LoRAMergedSource with no indication the "source model" was
    actually an unmerged adapter.

    Behavior is UNCHANGED -- the adapter dir is still returned. Refusing it
    outright would break the legitimate use: MagicQuant's own
    ``open_model_source()`` supports LoRA-merge-on-read given an adapter dir
    whose ``adapter_config.json`` resolves ``base_model_name_or_path`` to a
    local directory. This only makes that choice loud instead of silent.
    """
    adapter_cfg = c / "adapter_config.json"
    if not adapter_cfg.exists():
        return
    base_model = "<unknown -- adapter_config.json has no base_model_name_or_path>"
    try:
        cfg = json.loads(adapter_cfg.read_text())
        candidate = cfg.get("base_model_name_or_path")
        if candidate:
            base_model = candidate
    except Exception:
        pass
    print(
        f"NOTE: MagicQuant source {c} is a LoRA ADAPTER directory "
        f"(adapter_config.json + adapter_model.safetensors), not a merged "
        f"model. It will be used as-is: MagicQuant will LoRA-merge it onto "
        f"base model '{base_model}' on read (LoRAMergedSource), not "
        f"quantize a pre-merged checkpoint. If you meant to quantize the "
        f"fully-merged model instead, run the export stage first (produces "
        f"merged_model/) or point the source at that directory explicitly.",
        flush=True,
    )


def resolve_source(override: str, out_dir: Path, pipeline_root: str) -> str | None:
    """Resolve the MagicQuant source model (reap > heretic > merged > bf16 gguf).

    Mirrors the priority chain used by the rest of the pipeline. Pure path logic;
    unit-testable.
    """
    candidates: list[Path] = []
    if override:
        p = Path(override)
        if not p.is_absolute():
            candidates = [out_dir / override, Path(pipeline_root) / override]
        else:
            candidates = [p]
    if not candidates:
        candidates = [out_dir]
    for c in candidates:
        if c.is_dir():
            for sub in ("reap_model", "heretic_model", "merged_model"):
                d = c / sub
                if d.exists() and any(d.glob("*.safetensors")):
                    return str(d)
            if any(c.glob("*.safetensors")):
                _disclose_if_lora_adapter_dir(c)
                return str(c)
            gguf = c / "model-bf16.gguf"
            if gguf.exists():
                return str(gguf)
        elif c.is_file():
            return str(c)
    return None


def _find_convert_script(llamacpp_dir: str):
    """Locate llama.cpp's convert_hf_to_gguf.py given a build/bin dir.

    The script lives in the llama.cpp *source root*, but ``llamacpp_dir`` is
    typically a build subdir (e.g. ``<src>/build-strix-rocmfp4`` or its
    ``bin/``). Check the dir itself, its ``bin/``, and walk up parents to the
    source root. Returns the Path or None.
    """
    d = Path(llamacpp_dir)
    candidates = [d / "convert_hf_to_gguf.py", d / "bin" / "convert_hf_to_gguf.py"]
    # Walk up: build dirs nest under the source root that holds the converter.
    for parent in list(d.parents)[:4]:
        candidates.append(parent / "convert_hf_to_gguf.py")
    for c in candidates:
        if c.exists():
            return c
    return None


def _ensure_bf16_gguf(llamacpp_dir: str, source: str, out_dir: Path) -> str:
    import subprocess
    if source.endswith(".gguf"):
        return source
    cached = out_dir / "model-bf16.gguf"
    if cached.exists():
        print(f"Reusing cached BF16 GGUF: {cached}", flush=True)
        return str(cached)
    convert_script = _find_convert_script(llamacpp_dir)
    if convert_script is None:
        raise RuntimeError(
            f"convert_hf_to_gguf.py not found near {llamacpp_dir} "
            "(needed to convert safetensors -> BF16 GGUF for baseline perplexity)"
        )
    print(f"Converting {source} -> {cached} (BF16)...", flush=True)
    rc = subprocess.run([
        sys.executable, str(convert_script), source,
        "--outfile", str(cached), "--outtype", "bf16",
    ]).returncode
    if rc != 0 or not cached.exists():
        raise RuntimeError(f"convert_hf_to_gguf.py failed (exit code {rc})")
    return str(cached)


def should_convert_source_to_gguf(is_dir_source: bool, llamacpp: str | None) -> bool:
    """Decide whether the resolved MagicQuant source should be converted to a
    BF16 GGUF via llama.cpp's converter before search.

    ALWAYS true for a directory (safetensors) source when llama.cpp is
    available -- regardless of ``measured`` mode. This used to be gated on
    ``measured`` (prediction-only search read safetensors directly via
    MagicQuant's own SafetensorsSource), but that reader's HF->GGUF value
    transforms silently drifted out of sync for the qwen3_5 arch and produced
    garbage quants (fixed + gated in MagicQuant c169090). llama.cpp's
    ``convert_hf_to_gguf.py`` is the single source of truth for HF->GGUF
    semantics -- converting unconditionally means MagicQuant only ever reads
    GGUF, closing off that whole class of drift for every arch, not just the
    one that's been caught so far.

    False for an already-GGUF source (nothing to convert) or when llama.cpp
    discovery/install failed (``llamacpp is None``) -- the caller then falls
    back to the pre-this-change behavior of passing the directory straight
    through to SafetensorsSource, with a loud warning naming the risk;
    MagicQuant's own arch gate (c169090) is the backstop in that fallback
    path, not a substitute for conversion.

    Pure/unit-testable: takes the already-computed ``is_dir_source`` bool
    rather than touching the filesystem itself.
    """
    return is_dir_source and llamacpp is not None


def _is_vision_model(source: str) -> bool:
    """True if the safetensors source is a multimodal (vision) model — detected
    by a preprocessor_config.json, or a vision_config in config.json."""
    import os
    if not os.path.isdir(source):
        return False
    if os.path.exists(os.path.join(source, "preprocessor_config.json")):
        return True
    cfg_path = os.path.join(source, "config.json")
    if os.path.exists(cfg_path):
        try:
            import json as _json
            return "vision_config" in _json.loads(Path(cfg_path).read_text())
        except Exception:
            return False
    return False


def _maybe_generate_mmproj(llamacpp_dir: str, source: str, out_dir: Path,
                           model_name: str) -> None:
    """For a multimodal (vision) source, export the mmproj projector GGUF next to
    the text quants so image input works (text quant + mmproj = full VL serving:
    ``llama-server -m <quant>.gguf --mmproj mmproj-*.gguf``).

    Best-effort and additive: silently returns for text-only models, and never
    fails the quant stage on error — the text quants are valid without it. Needs
    the original safetensors ``source`` (holds the vision weights + processor
    config); can't be extracted from an already-converted text GGUF.
    """
    import subprocess
    if not _is_vision_model(source):
        return
    convert_script = _find_convert_script(llamacpp_dir)
    if convert_script is None:
        print("mmproj: convert_hf_to_gguf.py not found — skipping vision projector",
              flush=True)
        return
    name = model_name or out_dir.name
    mmproj_dir = out_dir / "mmproj"
    mmproj_dir.mkdir(parents=True, exist_ok=True)
    out_file = mmproj_dir / f"mmproj-{name}-f16.gguf"
    if out_file.exists() and out_file.stat().st_size > 0:
        print(f"mmproj: reusing {out_file}", flush=True)
        return
    print(f"Vision model detected -> generating mmproj: {out_file}", flush=True)
    try:
        rc = subprocess.run([
            sys.executable, str(convert_script), source,
            "--mmproj", "--outfile", str(out_file), "--outtype", "f16",
        ]).returncode
        if rc == 0 and out_file.exists():
            print(f"mmproj generated ({out_file.stat().st_size / 2**30:.2f} GiB) — "
                  "pair with any text quant for image input", flush=True)
        else:
            print(f"mmproj generation failed (exit {rc}); text quants still valid",
                  flush=True)
    except Exception as exc:  # best-effort: must never fail the quant stage
        print(f"mmproj generation error: {exc}; text quants still valid", flush=True)


# v1 config keys that have no meaning under the v2 budget search. Listed
# explicitly so a user who set one sees it acknowledged, not swallowed.
_BUDGET_IGNORED_KEYS = (
    "generations", "population_size", "target_base_quant", "measured",
    "measurement_rounds", "iq_schemes", "seed", "enable_kl", "kl_weight",
    "enable_speed_bench", "stream_aware", "head_aggressive", "speed_aware",
    "speed_metric", "speed_weight", "use_bytes_tps", "calibration_source",
    "write_calibration", "tiers_json", "verify",
)

_BUDGET_KEY_DEFAULTS = {
    "generations": 50, "population_size": 100, "measurement_rounds": 3,
    "target_base_quant": "MXFP4_MOE", "kl_weight": 0.1,
    "speed_metric": "bytes", "tiers_json": '["Q4","Q5","Q6"]',
}


def _run_budget(cfg: dict, source: str, llamacpp) -> str:
    """Size-target mode: MagicQuant v2 budget search instead of the tier
    ladder. `source` and `llamacpp` are the RESOLVED values from run()'s
    shared preamble (source priority + safetensors->BF16 conversion + mmproj
    export all apply to budget runs identically). Returns the renamed output
    GGUF path (publish naming)."""
    from magicquant.v2 import (BudgetInfeasibleError, V2Config,
                               budget_tier_key, run_budget_search)

    budget_gib = float(cfg["budget_gib"])
    for key in _BUDGET_IGNORED_KEYS:
        val = cfg.get(key)
        default = _BUDGET_KEY_DEFAULTS.get(key)
        if val not in (None, False, "", default):
            print(f"  note: {key}={val!r} is ignored under "
                  f"--magicquant-budget-gib (v2 search)", flush=True)

    out_dir = Path(cfg["out_abs_str"]) / "magicquant"
    v2cfg = V2Config(
        source_model_path=source,
        output_dir=str(out_dir),
        budget_gb=budget_gib,
        llamacpp_path=llamacpp or None,
        use_imatrix=cfg.get("use_imatrix", True),
        imatrix_corpus=cfg.get("imatrix_corpus"),
        measurement_chunks=cfg.get("measurement_chunks"),
        enable_rocmfpx=cfg.get("rocmfpx_schemes", False),
        model_name=cfg["model_name"],
    )
    try:
        results = run_budget_search(v2cfg)
    except BudgetInfeasibleError as e:
        print(
            f"Error: budget {e.budget_bytes / 1024**3:.2f} GiB is below the "
            f"floor; minimum achievable with the enabled schemes is "
            f"{e.min_bytes / 1024**3:.2f} GiB. Raise --magicquant-budget-gib.",
            flush=True,
        )
        sys.exit(1)

    if not results.get("final_model"):
        print(
            "Error: v2 budget search produced no final model (the budget "
            "anchor's build or measurement failed; a neighbor anchor may have "
            f"succeeded). See {out_dir / 'v2_results.json'} -> 'failures'.",
            flush=True,
        )
        sys.exit(1)

    final = Path(results["final_model"])
    target = final.parent / f"{cfg['model_name']}-{budget_tier_key(budget_gib)}.gguf"
    final.rename(target)
    print(f"  budget build: {target.name} "
          f"({target.stat().st_size / 1024**3:.2f} GiB)", flush=True)
    return str(target)


def run(cfg_path: str | None = None) -> None:
    if cfg_path is None:
        cfg_path = sys.argv[1]
    cfg = parse_config(cfg_path)

    core_path = str(Path(cfg["pipeline_root"]) / "core")
    if core_path not in sys.path:
        sys.path.insert(0, core_path)

    import os

    llamacpp = ensure_llamacpp(cfg.get("llamacpp_hint", ""))
    print(f"llama.cpp: {llamacpp or 'not found (heuristic mode)'}", flush=True)

    # ROCmFPX-native search: the AMD fork types can only be encoded by the
    # fork's libggml, so point MagicQuant's ctypes binding at a ROCmFPX build
    # before importing the orchestrator (the binding resolves the lib lazily,
    # but set it early and fail loudly if the fork isn't available).
    if cfg.get("rocmfpx_schemes"):
        try:
            import _rocmfpx_entry
        except ImportError:
            from core import _rocmfpx_entry  # pragma: no cover
        rocmfpx_dir = _rocmfpx_entry.find_rocmfpx(cfg.get("rocmfpx_hint", ""))
        if not rocmfpx_dir:
            print(
                "Error: --magicquant-rocmfpx needs a ROCmFPX build (for its "
                "libggml). None found — build it via the ROCmFPX stage first, "
                "or set a build hint.",
                flush=True,
            )
            sys.exit(1)
        bindir = str(Path(rocmfpx_dir) / "build-strix-rocmfp4" / "bin")
        os.environ["MAGICQUANT_LIBGGML_DIR"] = bindir
        print(f"MagicQuant libggml -> {bindir} (ROCmFPX fork types enabled)", flush=True)

    if apply_dequant_env(cfg, os.environ):
        print(
            "WARNING: allow_dequant_source is on -- an already-quantized GGUF "
            "source will be DOUBLE-quantized. Output quality is bounded by the "
            "source quant's error floor; tiers at/above the source's precision "
            "are pointless. Disclose this on the model card.",
            flush=True,
        )

    from magicquant.orchestrator import MagicQuantOrchestrator

    out_dir = Path(cfg["out_abs_str"])
    source = resolve_source(cfg["mq_source_override"], out_dir, cfg["pipeline_root_str"])
    if not source:
        print(
            "Error: no source model found. Enable Export or set a Source Model "
            "path in MagicQuant config.",
            flush=True,
        )
        sys.exit(1)
    print(f"MagicQuant source: {source}", flush=True)

    # Vision models: export the mmproj projector alongside the text quants so
    # image input works (text quant + mmproj = full VL). Best-effort, from the
    # original safetensors source (must run before it's reassigned to the BF16
    # GGUF below, which no longer carries the vision weights).
    if llamacpp and os.path.isdir(source):
        _maybe_generate_mmproj(llamacpp, source, out_dir, cfg.get("model_name", ""))

    # Convert a safetensors source to BF16 GGUF via llama.cpp's converter --
    # ALWAYS, regardless of measured vs. prediction-only search (see
    # should_convert_source_to_gguf's docstring: this is the single source of
    # truth for HF->GGUF semantics, closing off the SafetensorsSource-drift
    # failure mode that produced silent garbage quants for qwen3_5). The
    # converted file is a full model-size BF16 GGUF written to
    # <out_dir>/model-bf16.gguf and reused across runs (_ensure_bf16_gguf).
    is_dir_source = os.path.isdir(source)
    if should_convert_source_to_gguf(is_dir_source, llamacpp):
        print(
            "Conversion policy: safetensors sources are always converted to "
            "BF16 GGUF before search (not just for measured runs) -- "
            f"writing a model-size BF16 GGUF to {out_dir / 'model-bf16.gguf'} "
            "(cached across runs).",
            flush=True,
        )
        source = _ensure_bf16_gguf(llamacpp, source, out_dir)
        print(f"MagicQuant GGUF source: {source}", flush=True)
    elif is_dir_source:
        # llamacpp discovery/install failed -- fall back to the old behavior
        # (pass the safetensors dir straight through) rather than hard-fail;
        # MagicQuant's own arch gate (c169090) is the backstop, but it only
        # covers archs that gate has been taught about.
        print(
            "WARNING: llama.cpp unavailable -- passing safetensors source "
            "through WITHOUT BF16 GGUF conversion. MagicQuant's "
            "SafetensorsSource reader will be used directly; its HF->GGUF "
            "value transforms have previously drifted out of sync for a given "
            "arch and produced silent garbage quants (SafetensorsSource "
            "drift -- see qwen3_5 / MagicQuant c169090). Only MagicQuant's "
            "arch gate protects against this here, not a substitute for the "
            "conversion this stage normally performs.",
            flush=True,
        )

    if cfg.get("budget_gib"):
        # Size-target mode: v2 budget search instead of the v1 tier ladder.
        # `source`/`llamacpp` are the same resolved values the v1 branch
        # below uses -- resolve_source(), the safetensors->BF16 conversion,
        # and mmproj export above all apply identically to budget runs.
        valid = [_run_budget(cfg, source, llamacpp)]
    else:
        orch = MagicQuantOrchestrator(
            source_model_path=source,
            output_dir=str(out_dir / "magicquant"),
            llamacpp_path=llamacpp,
        )

        generations = cfg["generations"]
        population_size = cfg["population_size"]
        target_base_quant = cfg["target_base_quant"]
        measured = cfg.get("measured", False)
        enable_rocmfpx = cfg.get("rocmfpx_schemes", False)
        enable_iq = cfg.get("iq_schemes", False)
        mode = "measured (Predict->Measure->Learn)" if measured else "prediction-only"
        print(
            f"Search [{mode}]: generations={generations}, "
            f"population={population_size}, base={target_base_quant}, "
            f"rocmfpx_schemes={enable_rocmfpx}, iq_schemes={enable_iq}",
            flush=True,
        )

        use_imatrix = cfg.get("use_imatrix", False)
        imatrix_corpus = cfg.get("imatrix_corpus") or None
        measurement_chunks = cfg.get("measurement_chunks")
        stream_aware = cfg.get("stream_aware", False)
        head_aggressive = cfg.get("head_aggressive", False)
        # speed_weight/use_bytes_tps/calibration_source: tunable SEARCH objective,
        # accepted by both run_measured_search and run_full_search. speed_aware/
        # speed_metric/write_calibration are measured-search-only (see their
        # docstrings) -- forwarded only in the `measured` branch below, never to
        # run_full_search (which has no such params).
        speed_weight = cfg.get("speed_weight")
        use_bytes_tps = cfg.get("use_bytes_tps", False)
        calibration_source = cfg.get("calibration_source", "")

        if measured:
            # speed_aware: absent/null in cfg means "no explicit Foundry choice"
            # (see MagicQuantService.build_config's speed_aware docstring) -- the
            # kwarg is omitted entirely in that case so
            # run_measured_search's OWN default (True, the 2026-07 fix) actually
            # takes effect, instead of a stray explicit False (the old bug: cfg.
            # get("speed_aware", False) always produced a concrete False, since
            # the key was always present, permanently overriding the library
            # default no matter what it was set to). An explicit True/False in
            # cfg (a real user/CLI choice) is still forwarded and still wins.
            measured_kwargs = dict(
                target_base_quant=target_base_quant,
                search_generations=generations,
                population_size=population_size,
                measurement_rounds=cfg.get("measurement_rounds", 3),
                verbose=True,
                enable_rocmfpx=enable_rocmfpx,
                enable_iq=enable_iq,
                seed=cfg.get("seed"),
                use_imatrix=use_imatrix,
                imatrix_corpus=imatrix_corpus,
                enable_kl=cfg.get("enable_kl", False),
                kl_weight=cfg.get("kl_weight", 0.1),
                enable_speed_bench=cfg.get("enable_speed_bench", False),
                measurement_chunks=measurement_chunks,
                stream_aware=stream_aware,
                head_aggressive=head_aggressive,
                speed_metric=cfg.get("speed_metric", "bytes"),
                speed_weight=speed_weight,
                use_bytes_tps=use_bytes_tps,
                write_calibration=cfg.get("write_calibration", False),
                calibration_source=calibration_source,
            )
            cfg_speed_aware = cfg.get("speed_aware")
            if cfg_speed_aware is not None:
                measured_kwargs["speed_aware"] = cfg_speed_aware
            best_configs, tiered = orch.run_measured_search(**measured_kwargs)
        else:
            best_configs, tiered = orch.run_full_search(
                target_base_quant=target_base_quant,
                max_generations=generations,
                population_size=population_size,
                verbose=True,
                enable_rocmfpx=enable_rocmfpx,
                enable_iq=enable_iq,
                seed=cfg.get("seed"),
                use_imatrix=use_imatrix,
                imatrix_corpus=imatrix_corpus,
                measurement_chunks=measurement_chunks,
                stream_aware=stream_aware,
                head_aggressive=head_aggressive,
                speed_weight=speed_weight,
                use_bytes_tps=use_bytes_tps,
                calibration_source=calibration_source,
            )
        if not tiered:
            print("Error: no viable configurations found", flush=True)
            sys.exit(1)
        print(f"Tiers found: {list(tiered.keys())}", flush=True)

        tiers = json.loads(cfg["tiers_json"])
        paths = orch.generate_tiered_models(
            tiered=tiered,
            model_name_prefix=cfg["model_name"],
            tiers=tiers,
            verify=cfg["verify"],
        )
        valid = [p for p in paths if p]
        for p in valid:
            size = os.path.getsize(p) / 1e9
            print(f"  {Path(p).name} ({size:.1f} GB)", flush=True)
        print(f"Generated {len(valid)} hybrid GGUF files", flush=True)

    # Post-generation PPL smoke gate: a cheap, automatic sanity check the
    # pipeline itself lacked before this change -- the qwen3_5 garbage-quant
    # incident was only caught by an operator running perplexity by hand,
    # after the fact. Advisory-on-unknown (missing binary/corpus just skips
    # with a warning) but a completed run that comes back pathological is a
    # hard stage failure, not a warning.
    perplexity_bin = ppl_smoke.find_perplexity_bin(llamacpp) if llamacpp else None
    failed = [p for p in valid if not ppl_smoke.smoke_test_gguf(perplexity_bin, Path(p))]
    if failed:
        print(
            f"Error: PPL smoke test FAILED for {len(failed)}/{len(valid)} "
            f"file(s): {[Path(p).name for p in failed]} -- aborting before "
            f"upload. Override with {ppl_smoke.SKIP_ENV}=1 if this is a known "
            "false positive (not recommended).",
            flush=True,
        )
        sys.exit(1)

    print("PIPELINE_STAGE_COMPLETE=magicquant", flush=True)


if __name__ == "__main__":
    run()
