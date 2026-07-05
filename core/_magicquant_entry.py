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
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

LLAMACPP_REPO = "https://github.com/ggml-org/llama.cpp.git"
LLAMACPP_PIN = "gguf-v0.19.0"  # known-good release tag; bump deliberately


def parse_config(cfg_path: str) -> dict:
    return json.loads(Path(cfg_path).read_text())


def find_llamacpp(hint: str = "") -> str | None:
    """Return a llama.cpp dir that contains the converter or quantize binary."""
    import os

    for p in [hint, os.environ.get("LLAMACPP_PATH", ""),
              str(Path.home() / "llama.cpp"), "./llama.cpp", "/usr/local"]:
        if not p:
            continue
        pp = Path(p)
        for sub in [pp / "convert_hf_to_gguf.py", pp / "build" / "bin" / "llama-quantize"]:
            if sub.exists():
                return str(pp)
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
                return str(c)
            gguf = c / "model-bf16.gguf"
            if gguf.exists():
                return str(gguf)
        elif c.is_file():
            return str(c)
    return None


def _ensure_bf16_gguf(llamacpp_dir: str, source: str, out_dir: Path) -> str:
    import subprocess
    if source.endswith(".gguf"):
        return source
    cached = out_dir / "model-bf16.gguf"
    if cached.exists():
        print(f"Reusing cached BF16 GGUF: {cached}", flush=True)
        return str(cached)
    convert_script = Path(llamacpp_dir) / "convert_hf_to_gguf.py"
    if not convert_script.exists():
        convert_script = Path(llamacpp_dir) / "bin" / "convert_hf_to_gguf.py"
    if not convert_script.exists():
        raise RuntimeError(
            f"convert_hf_to_gguf.py not found in {llamacpp_dir} "
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

    # Measured search needs a GGUF — convert safetensors to BF16 GGUF first
    measured = cfg.get("measured", False)
    if measured and llamacpp and not source.endswith(".gguf"):
        source = _ensure_bf16_gguf(llamacpp, source, out_dir)
        print(f"MagicQuant GGUF source: {source}", flush=True)

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

    if measured:
        best_configs, tiered = orch.run_measured_search(
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
        )
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
    print("PIPELINE_STAGE_COMPLETE=magicquant", flush=True)


if __name__ == "__main__":
    run()
