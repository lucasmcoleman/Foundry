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
LLAMACPP_PIN = "b4585"  # known-good release tag; bump deliberately


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

    orch = MagicQuantOrchestrator(
        source_model_path=source,
        output_dir=str(out_dir / "magicquant"),
        llamacpp_path=llamacpp,
    )

    generations = cfg["generations"]
    population_size = cfg["population_size"]
    target_base_quant = cfg["target_base_quant"]
    print(
        f"Search: generations={generations}, population={population_size}, "
        f"base={target_base_quant}",
        flush=True,
    )

    best_configs, tiered = orch.run_full_search(
        target_base_quant=target_base_quant,
        max_generations=generations,
        population_size=population_size,
        verbose=True,
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
