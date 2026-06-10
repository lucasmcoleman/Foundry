"""Importable entry module for the REAP expert-pruning stage (audit H2).

``ReapService.build_script`` emits a thin shim that writes a JSON config and
invokes ``core/_reap_entry.py:run()``. The pruning orchestration — stubbing
REAP's heavy optional deps, chdir'ing so REAP's relative ``artifacts/`` lands
inside the run, invoking ``reap.prune.main()``, then relocating the pruned model
— lives here as ordinary Python.

Module import is stdlib-only; ``reap.prune`` is imported lazily inside ``run()``
(after the dep stubs are installed) so ``build_argv`` is unit-testable without
REAP installed.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_ROCM_ENV = {
    "HSA_ENABLE_SDMA": "0",
    "PYTORCH_HIP_ALLOC_CONF": "backend:native,expandable_segments:True",
    "TORCH_ROCM_AOTRITON_ENABLE_EXPERIMENTAL": "1",
}


def parse_config(cfg_path: str) -> dict:
    return json.loads(Path(cfg_path).read_text())


def build_argv(cfg: dict) -> list[str]:
    """Construct the ``reap.prune`` argv list from a config dict. Pure; testable."""
    return [
        "reap-prune",
        "--model-name", cfg["input_dir"],
        "--compression-ratio", str(cfg["compression_ratio"]),
        "--prune-method", cfg["prune_method"],
        "--samples_per_category", str(cfg["samples_per_category"]),
        "--model_max_length", str(cfg["model_max_length"]),
        "--seed", str(cfg["seed"]),
        "--dataset-name", cfg["dataset_name"],
        "--do-eval", "false",
        "--profile", "false",
        "--smoke_test", "false",
        "--record_pruning_metrics_only", "true",
        "--overwrite_observations", "false",
        "--plot_clusters", "false",
    ]


def run(cfg_path: str | None = None) -> None:
    import os
    import shutil

    for k, v in _ROCM_ENV.items():
        os.environ.setdefault(k, v)

    if cfg_path is None:
        cfg_path = sys.argv[1]
    cfg = parse_config(cfg_path)

    core_path = str(Path(cfg["pipeline_root"]) / "core")
    if core_path not in sys.path:
        sys.path.insert(0, core_path)

    # Stub heavy optional REAP deps + set the REAP src path on sys.path
    # (single source of truth — audit L-source-dup / L-reap-path-hardcoded).
    try:
        from reap_common import install_reap_stubs
    except ImportError:  # pragma: no cover
        from core.reap_common import install_reap_stubs
    install_reap_stubs()

    source = cfg["input_dir"]
    cwd = cfg["cwd_dir"]
    reap_dir = cfg["output_dir"]

    print(f"[reap] source: {source}", flush=True)
    print(f"[reap] cwd: {cwd}", flush=True)
    print(f"[reap] final dest: {reap_dir}", flush=True)

    # REAP writes to a relative ./artifacts/<model>/<dataset>/pruned_models/<name>/
    # path. Chdir into cwd so that relative path lands inside the Foundry run.
    os.makedirs(cwd, exist_ok=True)
    os.chdir(cwd)

    # Clear a stale reap artifacts dir from a previous run so we can reliably
    # locate the new pruned_models/ output after main() finishes.
    artifacts_root = Path(cwd) / "artifacts"
    if artifacts_root.exists():
        print(f"[reap] removing stale artifacts dir: {artifacts_root}", flush=True)
        shutil.rmtree(artifacts_root, ignore_errors=True)

    sys.argv = build_argv(cfg)
    print(f"[reap] invoking reap.prune.main() with argv: {sys.argv[1:]}", flush=True)
    from reap.prune import main as _reap_main
    _reap_main()

    # Locate the pruned_models/<something>/ directory that REAP just wrote.
    print(f"[reap] searching for pruned model under {artifacts_root}", flush=True)
    candidates = list(artifacts_root.rglob("pruned_models/*"))
    pruned = [c for c in candidates if c.is_dir() and any(c.glob("*.safetensors"))]
    if not pruned:
        print(
            f"[reap] ERROR: no pruned model directory with safetensors found under "
            f"{artifacts_root}",
            flush=True,
        )
        for p in sorted(artifacts_root.rglob("*"))[:50]:
            print(f"  {p}", flush=True)
        sys.exit(1)

    pruned.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    src = pruned[0]
    print(f"[reap] pruned model at: {src}", flush=True)

    # Move the pruned output into reap_dir.
    dest = Path(reap_dir)
    if dest.exists():
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dest))
    print(f"[reap] moved pruned model to: {dest}", flush=True)

    # Cleanup: remove the now-empty artifacts/ tree.
    try:
        shutil.rmtree(artifacts_root)
        print(f"[reap] cleaned up {artifacts_root}", flush=True)
    except Exception as e:
        print(f"[reap] warning: cleanup of {artifacts_root} failed: {e}", flush=True)

    print("PIPELINE_STAGE_COMPLETE=reap", flush=True)


if __name__ == "__main__":
    run()
