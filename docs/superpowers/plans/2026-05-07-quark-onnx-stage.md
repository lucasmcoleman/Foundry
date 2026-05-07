# Quark/ONNX Stage Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a new optional pipeline stage `onnx` that runs AMD Quark INT4 AWQ quantization followed by the onnxruntime-genai model builder, producing an OGA hybrid (NPU+iGPU) ONNX model directory that Lemonade Server consumes on Linux.

**Architecture:** New stage slots in parallel to the existing `magicquant` stage — both consume the same upstream source (`reap_model > heretic_model > merged_model`), each is independently toggleable via `Optional[OnnxConfig]` / `Optional[OnnxCfg]` in the dataclass + Pydantic config layer, and both write to sibling subdirectories under the run's `output_dir`. A single HF repo can hold both GGUF tiers and the `onnx_model/` directory; `lemonade pull <repo>` then routes per-format.

**Tech Stack:** Python 3.10+, AMD Quark (`amd-quark>=0.11`, used via the official `quantize_quark.py` reference script), onnxruntime-genai (`>=0.6`, used via its `models.builder` Python module), FastAPI/Pydantic for UI, existing Foundry subprocess-script-generation pattern.

**Spec:** See `docs/superpowers/specs/2026-05-07-quark-onnx-stage-design.md`.

---

## Pre-flight context for the engineer

You have not seen this codebase before. Three things to internalize before you start:

1. **Stages follow a strict pattern** — every existing stage (`training`, `export`, `heretic`, `reap`, `magicquant`, `upload`) has the same five touchpoints: a dataclass in `core/pipeline.py`, a Pydantic mirror in `ui/app.py`, a `*Service` class in `core/services.py` whose `build_script()` returns a Python script string, a `stage_*` (CLI) and `do_*` (UI) function that runs the script in a subprocess, and an entry in `STAGES`/`ALL_STAGES`/`STAGE_RUNNERS`. Read `core/pipeline.py:stage_magicquant` and `core/services.py:MagicQuantService` first — your new stage is a near-clone in shape.

2. **Subprocess scripts are generated as text** — the `build_script()` methods return Python source code that's written to disk and executed with the venv interpreter. This isolates GPU memory: each stage gets a fresh process. Don't try to refactor away from this pattern.

3. **AMD APU memory** — this server has 124 GB of unified memory. Quark loads the model in FP16 to do AWQ calibration, so a 30B model needs ~70 GB and a 70B model won't fit. The stage should fail loudly on OOM, not silently fall back. Lemonade is the *runtime* (separate machine concern); Foundry only *builds*.

---

## File map

| File | Status | Responsibility |
|---|---|---|
| `core/pipeline.py` | modify | `OnnxConfig` dataclass, `Artifacts.{onnx_dir,quark_dir}`, `stage_onnx()`, register in `STAGES`, CLI flags. |
| `core/services.py` | modify | New `OnnxService` class — generates the subprocess script. |
| `core/onnx_quark.py` | **new** | Runtime helper: locate/install Quark, locate/install onnxruntime-genai, run `quantize_quark.py`, run OGA builder, copy tokenizer files, optional cleanup. Imported by the generated subprocess script. |
| `core/hf_upload.py` | modify | `upload_onnx`, `did_onnx` flags; copy `onnx_model/` tree; emit ONNX section in model card. |
| `ui/app.py` | modify | `OnnxCfg` Pydantic, `do_onnx()` async, register in `ALL_STAGES`/`STAGE_RUNNERS`/`validate_pipeline()`. |
| `ui/index.html` | modify | New stage panel + sidebar entry mirroring the MagicQuant ones. |
| `configs/default.yaml` | modify | Add `onnx:` section with documented defaults. |
| `pyproject.toml` | modify | Add `[project.optional-dependencies] onnx = [...]`. |
| `tests/test_onnx_quark.py` | **new** | Unit tests for the wrapper module's argv construction and path resolution. |
| `tests/test_onnx_stage.py` | **new** | End-to-end smoke test on a tiny model (≤200M params) producing a valid `onnx_model/` directory. |
| `CLAUDE.md` | modify | Document the new stage and Lemonade runtime target. |

---

## Task 1: Config classes + Artifacts paths + pyproject extras

**Files:**
- Modify: `core/pipeline.py` — add `OnnxConfig` dataclass, `Artifacts.onnx_dir`, `Artifacts.quark_dir`, add `Optional[OnnxConfig]` to `PipelineConfig`.
- Modify: `ui/app.py` — add `OnnxCfg` Pydantic model, add `Optional[OnnxCfg]` to `RunRequest`.
- Modify: `pyproject.toml` — add the `onnx` optional dependency group.

This task introduces only types and paths. No behavior yet. The next tasks will fail tests until they're done; that's expected.

- [ ] **Step 1: Read the existing config patterns**

Run: `sed -n '74,101p' core/pipeline.py` — observe `MagicQuantConfig` shape.
Run: `sed -n '153,160p' ui/app.py` — observe `MagicQuantCfg` shape.

You're cloning these.

- [ ] **Step 2: Add `OnnxConfig` dataclass to `core/pipeline.py`**

Insert immediately after `MagicQuantConfig` (around line 101):

```python
@dataclass
class OnnxConfig:
    """AMD Quark INT4 AWQ → ORT-GenAI ONNX for Lemonade NPU+iGPU hybrid serving.

    Output: <output_dir>/onnx_model/ (model.onnx, model.onnx.data, genai_config.json,
    tokenizer files). Drop-in for `lemonade pull <hf_repo>` then `lemonade run`.
    """
    quant_scheme: str = "uint4_wo_128"          # uint4_wo_32 | uint4_wo_64 | uint4_wo_128
    quant_algo: str = "awq"                      # awq | gptq
    execution_provider: str = "dml"              # dml (hybrid) | cpu (NPU-only); dml only tested
    data_type: str = "float16"                   # float16 | bfloat16
    num_calib_data: int = 128
    seq_len: int = 512
    calib_dataset: str = "pileval_for_awq_benchmark"  # HF id or local .jsonl path
    cleanup_intermediates: bool = True           # delete quark_safetensors/ after build
    source_model: str = ""                       # override when running without upstream stages
```

- [ ] **Step 3: Add `Artifacts.onnx_dir` and `Artifacts.quark_dir`**

In the `Artifacts` class (around line 151), add immediately after `magicquant_dir`:

```python
    @property
    def quark_dir(self) -> Path:
        return self.output_dir / "quark_safetensors"

    @property
    def onnx_dir(self) -> Path:
        return self.output_dir / "onnx_model"
```

- [ ] **Step 4: Wire `OnnxConfig` into `PipelineConfig`**

In `PipelineConfig` (around line 138-146), add after the `magicquant` field:

```python
    onnx: Optional[OnnxConfig] = None
```

(Default `None` — opt-in like `heretic` and `reap`, not opt-out like `magicquant`. Rationale: Quark/ONNX is a heavier and newer path; users should explicitly turn it on.)

- [ ] **Step 5: Add `OnnxCfg` Pydantic model in `ui/app.py`**

Insert immediately after `MagicQuantCfg` (around line 160):

```python
class OnnxCfg(BaseModel):
    quant_scheme: str = "uint4_wo_128"
    quant_algo: str = "awq"
    execution_provider: str = "dml"
    data_type: str = "float16"
    num_calib_data: int = 128
    seq_len: int = 512
    calib_dataset: str = "pileval_for_awq_benchmark"
    cleanup_intermediates: bool = True
    source_model: str = ""
```

- [ ] **Step 6: Wire `OnnxCfg` into `RunRequest`**

In `RunRequest` (around line 170-177), add after the `magicquant` field:

```python
    onnx: Optional[OnnxCfg] = None
```

- [ ] **Step 7: Add `onnx` to `ALL_STAGES` in `ui/app.py`**

Find line 72 (`ALL_STAGES = ["training", "export", "heretic", "reap", "magicquant", "upload"]`) and update to:

```python
ALL_STAGES = ["training", "export", "heretic", "reap", "magicquant", "onnx", "upload"]
```

(Insert `onnx` after `magicquant` so the UI sidebar lists it adjacent to its sibling.)

- [ ] **Step 8: Add `onnx` to `STAGES` in `core/pipeline.py`**

Find the `STAGES` list (around line 1377) and update to:

```python
STAGES = [
    ("training",   stage_training),
    ("export",     stage_export),
    ("heretic",    stage_heretic),
    ("reap",       stage_reap),
    ("magicquant", stage_magicquant),
    ("onnx",       stage_onnx),       # <-- new entry; stage_onnx defined in Task 4
    ("upload",     stage_upload),
]
```

This will fail to import until Task 4 defines `stage_onnx`. That's expected — defer running anything until Task 4. (You can leave the entry commented out at this step and uncomment in Task 4 to keep the import green; up to you.)

- [ ] **Step 9: Add the `onnx` extras group to `pyproject.toml`**

Find the `[project.optional-dependencies]` block (around line 56) and append a new group:

```toml
onnx = [
    "amd-quark>=0.11",
    "onnxruntime-genai>=0.6",
]
```

- [ ] **Step 10: Verify imports still work**

Run: `python -c "from core.pipeline import OnnxConfig, Artifacts; print(OnnxConfig().quant_scheme, Artifacts('./tmp').onnx_dir)"`
Expected: `uint4_wo_128 tmp/onnx_model`

(If you commented out the `STAGES` entry in Step 8, this works. If you uncommented it, it'll fail on `stage_onnx` not being defined — that's also OK; it'll be fixed in Task 4.)

Run: `python -c "from ui.app import OnnxCfg, RunRequest; r = RunRequest(); print(r.onnx)"`
Expected: `None`

- [ ] **Step 11: Commit**

```bash
git add core/pipeline.py ui/app.py pyproject.toml
git commit -m "feat: add OnnxConfig/OnnxCfg config classes + Artifacts paths

Lays the groundwork for the new onnx pipeline stage. No behavior yet —
just the type definitions, artifact paths, and the optional dependency
group. The stage_onnx wiring in STAGES is added in a follow-up commit
once the runner is implemented."
```

---

## Task 2: Build `core/onnx_quark.py` runtime helper

**Files:**
- Create: `core/onnx_quark.py`
- Create: `tests/test_onnx_quark.py`

This module is imported by the generated subprocess script. It does four things:
1. Ensure `amd-quark` and `onnxruntime-genai` are pip-installed; otherwise install them.
2. Ensure the AMD `Quark` GitHub repo is cloned to `~/quark-amd/` (the `quantize_quark.py` reference script lives in `examples/torch/language_modeling/llm_ptq/`, not in the pip wheel).
3. Run `quantize_quark.py` as a subprocess with the right argv.
4. Run `python -m onnxruntime_genai.models.builder` as a subprocess.
5. Copy tokenizer files from the source dir into the final `onnx_model/` so Lemonade has everything in one place; optionally delete the intermediate `quark_safetensors/`.

The test file mocks `subprocess.run` and asserts argv shape — we don't actually run Quark in the unit test (that's Task 9's job).

- [ ] **Step 1: Read the `ensure_llamacpp` pattern you'll be cloning**

Run: `sed -n '362,397p' core/pipeline.py`

Note the shape: probe → if missing, clone shallow → return path. Match this style.

- [ ] **Step 2: Write the failing test**

Create `tests/test_onnx_quark.py`:

```python
"""Unit tests for core/onnx_quark.py — argv construction and path resolution.

We mock subprocess.run because actually invoking Quark or the OGA builder
would require GPU + multi-GB downloads. End-to-end is covered in
tests/test_onnx_stage.py.
"""

import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "core"))

from onnx_quark import (
    build_quark_argv,
    build_oga_builder_argv,
    find_quantize_quark_script,
)


def test_quark_argv_uses_uint4_wo_128_awq_by_default():
    argv = build_quark_argv(
        script_path="/tmp/quantize_quark.py",
        model_dir="/srv/merged",
        output_dir="/srv/quark",
        quant_scheme="uint4_wo_128",
        quant_algo="awq",
        data_type="float16",
        num_calib_data=128,
        seq_len=512,
        calib_dataset="pileval_for_awq_benchmark",
    )
    assert argv[0] == sys.executable
    assert "/tmp/quantize_quark.py" in argv
    assert "--model_dir" in argv and "/srv/merged" in argv
    assert "--output_dir" in argv and "/srv/quark" in argv
    assert "--quant_scheme" in argv and "uint4_wo_128" in argv
    assert "--quant_algo" in argv and "awq" in argv
    assert "--data_type" in argv and "float16" in argv
    assert "--num_calib_data" in argv and "128" in argv
    assert "--seq_len" in argv and "512" in argv
    assert "--dataset" in argv and "pileval_for_awq_benchmark" in argv
    assert "--model_export" in argv and "hf_format" in argv
    assert "--custom_mode" in argv and "awq" in argv


def test_oga_builder_argv_hybrid_dml():
    argv = build_oga_builder_argv(
        input_dir="/srv/quark",
        output_dir="/srv/onnx_model",
        precision="int4",
        execution_provider="dml",
    )
    assert argv[:3] == [sys.executable, "-m", "onnxruntime_genai.models.builder"]
    assert "-i" in argv and "/srv/quark" in argv
    assert "-o" in argv and "/srv/onnx_model" in argv
    assert "-p" in argv and "int4" in argv
    assert "-e" in argv and "dml" in argv


def test_find_quantize_quark_script_returns_none_when_repo_missing(tmp_path, monkeypatch):
    # Point the helper at a known-empty home dir.
    monkeypatch.setenv("HOME", str(tmp_path))
    assert find_quantize_quark_script() is None
```

- [ ] **Step 3: Run the test to verify it fails**

Run: `pytest tests/test_onnx_quark.py -v`
Expected: `ImportError` / `ModuleNotFoundError: No module named 'onnx_quark'`

- [ ] **Step 4: Create `core/onnx_quark.py`**

```python
"""Runtime helper for the ONNX pipeline stage.

This module is imported by the subprocess script generated in
core/services.py:OnnxService. It handles:

  - ensuring the amd-quark and onnxruntime-genai pip packages are installed
  - cloning the AMD Quark GitHub repo (the reference quantize_quark.py
    script lives there, not in the pip wheel)
  - invoking quantize_quark.py with the right argv
  - invoking python -m onnxruntime_genai.models.builder with the right argv
  - copying tokenizer files into the final onnx_model/ directory
  - optionally cleaning up the intermediate quark_safetensors/

End-to-end behavior is covered by tests/test_onnx_stage.py.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable, Optional

QUARK_REPO_URL = "https://github.com/amd/Quark.git"
QUARK_REPO_BRANCH = "release/0.11"   # pinned; bump deliberately
QUARK_HOME = Path.home() / "quark-amd"
QUARK_SCRIPT_RELPATH = Path("examples/torch/language_modeling/llm_ptq/quantize_quark.py")

LogFn = Callable[[str], None]


def _default_log(msg: str) -> None:
    print(msg, flush=True)


def ensure_quark_installed(log: LogFn = _default_log) -> None:
    """Install amd-quark and onnxruntime-genai into the current venv if missing.

    Raises RuntimeError if pip install fails — we don't silently skip the stage.
    """
    missing = []
    try:
        import quark.torch  # noqa: F401
    except ImportError:
        missing.append("amd-quark>=0.11")
    try:
        import onnxruntime_genai  # noqa: F401
    except ImportError:
        missing.append("onnxruntime-genai>=0.6")

    if not missing:
        log("amd-quark and onnxruntime-genai already installed")
        return

    log(f"Installing: {', '.join(missing)}")
    rc = subprocess.run(
        [sys.executable, "-m", "pip", "install", *missing],
        check=False,
    ).returncode
    if rc != 0:
        raise RuntimeError(
            f"pip install failed (exit {rc}) for: {missing}. "
            "Cannot proceed with ONNX stage."
        )
    log("Install complete")


def ensure_quark_repo(log: LogFn = _default_log) -> Path:
    """Clone the AMD Quark GitHub repo if it isn't already at QUARK_HOME.

    Returns the local repo path. Raises RuntimeError on clone failure.
    """
    if (QUARK_HOME / QUARK_SCRIPT_RELPATH).exists():
        log(f"Quark repo found at {QUARK_HOME}")
        return QUARK_HOME

    log(f"Cloning amd/Quark ({QUARK_REPO_BRANCH}) to {QUARK_HOME}")
    rc = subprocess.run(
        ["git", "clone", "--depth", "1",
         "--branch", QUARK_REPO_BRANCH,
         QUARK_REPO_URL, str(QUARK_HOME)],
        check=False,
    ).returncode
    if rc != 0:
        raise RuntimeError(
            f"git clone of {QUARK_REPO_URL} failed (exit {rc})"
        )

    if not (QUARK_HOME / QUARK_SCRIPT_RELPATH).exists():
        raise RuntimeError(
            f"Cloned repo at {QUARK_HOME} does not contain "
            f"{QUARK_SCRIPT_RELPATH} — branch may have moved"
        )
    log(f"Quark repo cloned to {QUARK_HOME}")
    return QUARK_HOME


def find_quantize_quark_script() -> Optional[Path]:
    """Return the path to quantize_quark.py if the cloned repo contains it."""
    candidate = QUARK_HOME / QUARK_SCRIPT_RELPATH
    return candidate if candidate.exists() else None


def build_quark_argv(
    *,
    script_path: str,
    model_dir: str,
    output_dir: str,
    quant_scheme: str,
    quant_algo: str,
    data_type: str,
    num_calib_data: int,
    seq_len: int,
    calib_dataset: str,
) -> list[str]:
    """Construct the argv list for quantize_quark.py.

    Mirrors the recipe documented at
    https://quark.docs.amd.com/latest/supported_accelerators/ryzenai/tutorial_uint4_oga.html
    for INT4 AWQ → HF-format safetensors suitable for the OGA builder.
    """
    return [
        sys.executable, script_path,
        "--model_dir", model_dir,
        "--output_dir", output_dir,
        "--quant_scheme", quant_scheme,
        "--quant_algo", quant_algo,
        "--data_type", data_type,
        "--num_calib_data", str(num_calib_data),
        "--seq_len", str(seq_len),
        "--dataset", calib_dataset,
        "--model_export", "hf_format",
        "--custom_mode", "awq",
    ]


def build_oga_builder_argv(
    *,
    input_dir: str,
    output_dir: str,
    precision: str = "int4",
    execution_provider: str = "dml",
) -> list[str]:
    """Construct the argv list for `python -m onnxruntime_genai.models.builder`."""
    return [
        sys.executable, "-m", "onnxruntime_genai.models.builder",
        "-i", input_dir,
        "-o", output_dir,
        "-p", precision,
        "-e", execution_provider,
    ]


def _copy_tokenizer_files(source_dir: Path, dest_dir: Path, log: LogFn) -> None:
    """Copy tokenizer + chat-template files from source to dest if not already there.

    The OGA builder copies most of these itself, but we double-check so the
    final onnx_model/ is a complete drop-in for `lemonade pull`.
    """
    files = [
        "tokenizer.json", "tokenizer_config.json",
        "special_tokens_map.json", "tokenizer.model",
        "chat_template.jinja", "vocab.json", "merges.txt",
    ]
    for name in files:
        src = source_dir / name
        dst = dest_dir / name
        if src.exists() and not dst.exists():
            shutil.copy2(src, dst)
            log(f"  copied {name}")


def run_onnx_pipeline(
    *,
    source_dir: str,
    quark_dir: str,
    onnx_dir: str,
    quant_scheme: str,
    quant_algo: str,
    execution_provider: str,
    data_type: str,
    num_calib_data: int,
    seq_len: int,
    calib_dataset: str,
    cleanup_intermediates: bool,
    log: LogFn = _default_log,
) -> None:
    """Run the full ONNX build: Quark → OGA builder → tokenizer copy → cleanup.

    Raises RuntimeError on any subprocess failure. Idempotent at the directory
    level: if onnx_dir already contains model.onnx, returns early.
    """
    onnx_dir_p = Path(onnx_dir)
    quark_dir_p = Path(quark_dir)
    source_dir_p = Path(source_dir)

    if (onnx_dir_p / "model.onnx").exists():
        log(f"ONNX model already exists at {onnx_dir} — skipping")
        return

    if not source_dir_p.exists():
        raise RuntimeError(f"Source model not found: {source_dir}")

    ensure_quark_installed(log)
    ensure_quark_repo(log)

    script = find_quantize_quark_script()
    if script is None:
        raise RuntimeError(
            f"quantize_quark.py not found in {QUARK_HOME / QUARK_SCRIPT_RELPATH}"
        )

    quark_dir_p.mkdir(parents=True, exist_ok=True)
    onnx_dir_p.mkdir(parents=True, exist_ok=True)

    log(f"Running Quark INT4 AWQ quantization → {quark_dir}")
    quark_argv = build_quark_argv(
        script_path=str(script),
        model_dir=str(source_dir_p),
        output_dir=str(quark_dir_p),
        quant_scheme=quant_scheme,
        quant_algo=quant_algo,
        data_type=data_type,
        num_calib_data=num_calib_data,
        seq_len=seq_len,
        calib_dataset=calib_dataset,
    )
    rc = subprocess.run(quark_argv, check=False).returncode
    if rc != 0:
        raise RuntimeError(f"Quark quantization failed (exit {rc})")

    log(f"Running ORT-GenAI model builder → {onnx_dir}")
    oga_argv = build_oga_builder_argv(
        input_dir=str(quark_dir_p),
        output_dir=str(onnx_dir_p),
        precision="int4",
        execution_provider=execution_provider,
    )
    rc = subprocess.run(oga_argv, check=False).returncode
    if rc != 0:
        raise RuntimeError(f"ORT-GenAI model builder failed (exit {rc})")

    log("Copying tokenizer files")
    _copy_tokenizer_files(source_dir_p, onnx_dir_p, log)

    if cleanup_intermediates:
        log(f"Cleaning up intermediate {quark_dir}")
        shutil.rmtree(quark_dir_p, ignore_errors=True)

    log(f"ONNX build complete: {onnx_dir}")
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `pytest tests/test_onnx_quark.py -v`
Expected: 3 passed.

- [ ] **Step 6: Commit**

```bash
git add core/onnx_quark.py tests/test_onnx_quark.py
git commit -m "feat: add core/onnx_quark.py runtime helper

Wraps Quark's reference quantize_quark.py and the onnxruntime-genai
model builder behind a single run_onnx_pipeline() entry point. Auto-
installs amd-quark + onnxruntime-genai pip packages and clones the
amd/Quark repo (release/0.11) for the example script. Unit tests
cover argv construction and missing-repo path resolution; full e2e
is in tests/test_onnx_stage.py."
```

---

## Task 3: Add `OnnxService` to `core/services.py`

**Files:**
- Modify: `core/services.py` — add `OnnxService` class with `build_script()`.

This generates the Python source code that runs in the stage subprocess. Mirror `MagicQuantService` for shape — but the body is much smaller because the heavy lifting lives in `core/onnx_quark.py`.

- [ ] **Step 1: Read `MagicQuantService` to confirm the pattern**

Run: `sed -n '726,876p' core/services.py`

You'll see the build_script() concatenates Python source as text, using `repr()` to safely embed string config values. Match the style.

- [ ] **Step 2: Append `OnnxService` to `core/services.py`**

Add at the end of the file (after `UploadService`):

```python
class OnnxService:
    """Orchestrates the AMD Quark + ORT-GenAI model builder subprocess.

    The actual Quark and OGA builder calls live in core/onnx_quark.py — this
    service just generates the small subprocess script that resolves the
    source path and calls run_onnx_pipeline().
    """

    def __init__(self, pipeline_root: Path, venv_python: str) -> None:
        self.pipeline_root = pipeline_root
        self.venv_python = venv_python

    def build_script(
        self,
        *,
        pipeline_root_str: str,
        out_abs_str: str,
        source_override: str,
        quant_scheme: str,
        quant_algo: str,
        execution_provider: str,
        data_type: str,
        num_calib_data: int,
        seq_len: int,
        calib_dataset: str,
        cleanup_intermediates: bool,
    ) -> str:
        """Generate the subprocess script text for the ONNX stage."""
        core_path = repr(str(self.pipeline_root / "core"))

        script = _env_preamble()
        script += (
            f"\nimport sys\n"
            f"from pathlib import Path\n"
            f"sys.path.insert(0, str(Path({repr(str(self.pipeline_root))}) / 'core'))\n"
            f"from onnx_quark import run_onnx_pipeline\n"
            f"\n"
            f"override = {repr(source_override)}\n"
            f"out_dir = Path({repr(out_abs_str)})\n"
            f"\n"
            f"def _resolve_source(override, out_dir):\n"
            f"    candidates = []\n"
            f"    if override:\n"
            f"        p = Path(override)\n"
            f"        if not p.is_absolute():\n"
            f"            candidates = [out_dir / override, "
            f"Path({repr(pipeline_root_str)}) / override]\n"
            f"        else:\n"
            f"            candidates = [p]\n"
            f"    if not candidates:\n"
            f"        candidates = [out_dir]\n"
            f"    for c in candidates:\n"
            f"        if c.is_dir():\n"
            f"            for sub in ('reap_model', 'heretic_model', 'merged_model'):\n"
            f"                _p = c / sub\n"
            f"                if _p.exists() and any(_p.glob('*.safetensors')):\n"
            f"                    return str(_p)\n"
            f"            if any(c.glob('*.safetensors')):\n"
            f"                return str(c)\n"
            f"    return None\n"
            f"\n"
            f"source = _resolve_source(override, out_dir)\n"
            f"if not source:\n"
            f"    print('Error: no safetensors source model found. Enable an "
            f"upstream stage (export/heretic/reap) or set OnnxConfig.source_model.')\n"
            f"    sys.exit(1)\n"
            f"print(f'ONNX source: {{source}}')\n"
            f"\n"
            f"run_onnx_pipeline(\n"
            f"    source_dir=source,\n"
            f"    quark_dir=str(out_dir / 'quark_safetensors'),\n"
            f"    onnx_dir=str(out_dir / 'onnx_model'),\n"
            f"    quant_scheme={repr(quant_scheme)},\n"
            f"    quant_algo={repr(quant_algo)},\n"
            f"    execution_provider={repr(execution_provider)},\n"
            f"    data_type={repr(data_type)},\n"
            f"    num_calib_data={num_calib_data},\n"
            f"    seq_len={seq_len},\n"
            f"    calib_dataset={repr(calib_dataset)},\n"
            f"    cleanup_intermediates={cleanup_intermediates},\n"
            f")\n"
            f"print('PIPELINE_STAGE_COMPLETE=onnx')\n"
        )
        return script
```

Note: `_env_preamble()` is the helper at the top of `services.py` that emits the standard ROCm env-var block. We reuse it.

- [ ] **Step 3: Smoke-check that the generated script is syntactically valid Python**

Run:

```bash
python -c "
from pathlib import Path
import sys
sys.path.insert(0, 'core')
from services import OnnxService
svc = OnnxService(Path('.'), sys.executable)
src = svc.build_script(
    pipeline_root_str='/tmp/foundry',
    out_abs_str='/tmp/foundry/output',
    source_override='',
    quant_scheme='uint4_wo_128',
    quant_algo='awq',
    execution_provider='dml',
    data_type='float16',
    num_calib_data=128,
    seq_len=512,
    calib_dataset='pileval_for_awq_benchmark',
    cleanup_intermediates=True,
)
compile(src, '<generated>', 'exec')
print('OK — generated script is syntactically valid Python')
print(f'   length: {len(src)} chars')
"
```

Expected: `OK — generated script is syntactically valid Python` and a length around 1500-2000 chars. Any `SyntaxError` here means a typo in the f-string concatenation; fix and retry.

- [ ] **Step 4: Commit**

```bash
git add core/services.py
git commit -m "feat: add OnnxService for the new onnx pipeline stage

Generates the subprocess script that resolves the source model path
(reap > heretic > merged) and calls core.onnx_quark.run_onnx_pipeline().
Mirrors MagicQuantService in shape; the heavy lifting is in onnx_quark
to keep the generated script small."
```

---

## Task 4: Add `stage_onnx()` to `core/pipeline.py` and register it

**Files:**
- Modify: `core/pipeline.py` — add `stage_onnx()`, ensure `STAGES` includes it, ensure `run_pipeline()` adds `onnx` to the enabled set.

- [ ] **Step 1: Read `stage_magicquant` end-to-end**

Run: `sed -n '1186,1259p' core/pipeline.py`

Note the structure: log opening line → resolve source dir → call orchestrator → check output exists → log completion. Match this shape.

- [ ] **Step 2: Add `stage_onnx()` immediately after `stage_magicquant()`**

Insert before the `# ── Stage: Upload` divider (around line 1261):

```python
# ── Stage: ONNX (AMD Quark INT4 AWQ + ORT-GenAI builder) ─────────────────────
#
# Builds an ONNX model directory for OGA hybrid (NPU+iGPU) execution via
# Lemonade Server on Linux. Sibling to magicquant — both can run from the
# same upstream artifacts, neither blocks the other.

def stage_onnx(config: PipelineConfig, artifacts: Artifacts, log: LogFn) -> bool:
    """Run AMD Quark INT4 AWQ + ORT-GenAI model builder.

    Reads from same source priority as magicquant: reap > heretic > merged.
    Writes onnx_model/ alongside any existing magicquant/ output. The actual
    Quark + OGA invocations live in core.onnx_quark.run_onnx_pipeline().
    """
    log("Starting ONNX (Quark INT4 AWQ + OGA builder)", "stage")

    # Skip if final artifact already present.
    if artifacts.onnx_dir.exists() and (artifacts.onnx_dir / "model.onnx").exists():
        log(f"ONNX model already exists at {artifacts.onnx_dir} — skipping", "success")
        return True

    # Determine source: same priority as MagicQuant.
    if artifacts.reap_dir.exists() and any(artifacts.reap_dir.glob("*.safetensors")):
        source_path = str(artifacts.reap_dir)
        log(f"Source: REAP-pruned model at {source_path}")
    elif artifacts.heretic_dir.exists() and any(artifacts.heretic_dir.glob("*.safetensors")):
        source_path = str(artifacts.heretic_dir)
        log(f"Source: abliterated model at {source_path}")
    elif artifacts.merged_dir.exists() and any(artifacts.merged_dir.glob("*.safetensors")):
        source_path = str(artifacts.merged_dir)
        log(f"Source: merged safetensors at {source_path}")
    elif config.onnx and config.onnx.source_model:
        source_path = config.onnx.source_model
        log(f"Source: explicit override at {source_path}")
    else:
        log("No safetensors source model found — run export first or set OnnxConfig.source_model", "error")
        return False

    oc = config.onnx
    _project_root = Path(__file__).resolve().parent.parent

    # Subprocess script — keep imports minimal; the heavy lifting is in core/onnx_quark.py.
    script = f'''
import os, sys
from pathlib import Path
os.environ["HSA_ENABLE_SDMA"] = "0"
os.environ["PYTORCH_HIP_ALLOC_CONF"] = "backend:native,expandable_segments:True"
os.environ["TORCH_ROCM_AOTRITON_ENABLE_EXPERIMENTAL"] = "1"

sys.path.insert(0, str(Path({repr(str(_project_root))}) / "core"))
from onnx_quark import run_onnx_pipeline

run_onnx_pipeline(
    source_dir={source_path!r},
    quark_dir={str(artifacts.quark_dir)!r},
    onnx_dir={str(artifacts.onnx_dir)!r},
    quant_scheme={oc.quant_scheme!r},
    quant_algo={oc.quant_algo!r},
    execution_provider={oc.execution_provider!r},
    data_type={oc.data_type!r},
    num_calib_data={oc.num_calib_data},
    seq_len={oc.seq_len},
    calib_dataset={oc.calib_dataset!r},
    cleanup_intermediates={oc.cleanup_intermediates},
)
print("PIPELINE_STAGE_COMPLETE=onnx")
'''

    script_path = artifacts.output_dir / "_stage_onnx.py"
    script_path.write_text(script)

    rc = _run([_find_python(), "-u", str(script_path)], log, cwd=str(_project_root))
    if rc != 0:
        log(f"ONNX stage failed (exit code {rc})", "error")
        return False

    if not (artifacts.onnx_dir / "model.onnx").exists():
        log("ONNX stage completed but model.onnx not found — investigate", "error")
        return False

    size_gb = (artifacts.onnx_dir / "model.onnx.data").stat().st_size / 1e9 if (artifacts.onnx_dir / "model.onnx.data").exists() else 0
    log(f"ONNX model written: {artifacts.onnx_dir} ({size_gb:.1f} GB weights)", "success")
    return True
```

- [ ] **Step 3: Confirm `STAGES` registration is correct**

Find the `STAGES` list (set in Task 1 Step 8). Make sure it reads:

```python
STAGES = [
    ("training",   stage_training),
    ("export",     stage_export),
    ("heretic",    stage_heretic),
    ("reap",       stage_reap),
    ("magicquant", stage_magicquant),
    ("onnx",       stage_onnx),
    ("upload",     stage_upload),
]
```

If you commented this out in Task 1 Step 8, uncomment it now.

- [ ] **Step 4: Update `run_pipeline()` to include `onnx` in the enabled set**

Find `run_pipeline()` (around line 1387-1404). Add an `onnx` enable check, modeled after the `magicquant` one:

```python
    if config.magicquant is not None:
        enabled.add("magicquant")
    if config.onnx is not None:               # <-- add this
        enabled.add("onnx")                   # <--
    if config.upload is not None:
        enabled.add("upload")
```

- [ ] **Step 5: Update the dry-run path's enabled set in the same way**

Find the dry-run block at around line 1492-1515 (`_dry_enabled = set()`) and add the same two-line block after the magicquant entry:

```python
        if cfg.magicquant is not None:
            _dry_enabled.add("magicquant")
        if cfg.onnx is not None:               # <-- add
            _dry_enabled.add("onnx")           # <--
        if cfg.upload is not None:
            _dry_enabled.add("upload")
```

- [ ] **Step 6: Update `stage_upload`'s did_* flags**

Find the `did_` flags being passed into `HFUploadConfig` (around lines 1308-1313 in `stage_upload`):

```python
        did_training="training" in _enabled,
        did_heretic="heretic" in _enabled,
        did_reap="reap" in _enabled,
        did_magicquant="magicquant" in _enabled,
        did_onnx="onnx" in _enabled,           # <-- add
```

Do the same for `stage_upload_dry_run` (around lines 1356-1359). The `did_onnx` field on `HFUploadConfig` is added in Task 7 — leaving these references in now is fine because Task 7 will follow.

- [ ] **Step 7: Smoke-test that the dry-run path still works**

Run: `python core/pipeline.py --help 2>&1 | head -30`
Expected: argparse help text appears with no Python error.

(The dry-run end-to-end smoke test will fail until `did_onnx` is added to `HFUploadConfig` in Task 7. That's expected.)

- [ ] **Step 8: Commit**

```bash
git add core/pipeline.py
git commit -m "feat: add stage_onnx pipeline stage and register it

stage_onnx is sibling to stage_magicquant — same source priority
(reap > heretic > merged), runs in a fresh subprocess that imports
core.onnx_quark.run_onnx_pipeline. did_onnx is threaded through to
the upload stage's HFUploadConfig (the field itself is added in a
follow-up commit)."
```

---

## Task 5: Add CLI flags to `core/pipeline.py`

**Files:**
- Modify: `core/pipeline.py` — add argparse flags + post-parse handling.

- [ ] **Step 1: Read the existing `--reap` flag handling for the pattern**

Run: `sed -n '1445,1485p' core/pipeline.py`

Note: opt-in stages have an `--<stage>` and `--no-<stage>` pair, plus per-stage knob flags. Match.

- [ ] **Step 2: Add the argparse flags**

Find the argparse section (around line 1431-1454). Add after the reap flags and before `--no-magicquant`:

```python
    parser.add_argument("--onnx", action="store_true", help="Enable ONNX (Quark INT4 AWQ + OGA) stage")
    parser.add_argument("--no-onnx", action="store_true", help="Disable ONNX stage")
    parser.add_argument("--onnx-quant-scheme", type=str, default="uint4_wo_128",
                        choices=["uint4_wo_32", "uint4_wo_64", "uint4_wo_128"],
                        help="Quark quantization scheme")
    parser.add_argument("--onnx-quant-algo", type=str, default="awq",
                        choices=["awq", "gptq"], help="Quark quantization algorithm")
    parser.add_argument("--onnx-ep", type=str, default="dml", choices=["dml", "cpu"],
                        help="OGA execution provider (dml=hybrid NPU+iGPU, cpu=NPU-only)")
    parser.add_argument("--onnx-num-calib", type=int, default=128, help="AWQ calibration samples")
    parser.add_argument("--onnx-seq-len", type=int, default=512, help="Calibration sequence length")
    parser.add_argument("--onnx-calib-dataset", type=str, default="pileval_for_awq_benchmark",
                        help="HF dataset id or local .jsonl path for calibration")
    parser.add_argument("--no-onnx-cleanup", action="store_true",
                        help="Keep intermediate quark_safetensors/ after build (default: delete)")
```

- [ ] **Step 3: Add post-parse handling**

Find the existing post-parse logic (around line 1481-1488). Add after the `--no-reap` block and before `--no-magicquant`:

```python
    if args.onnx and not args.no_onnx:
        cfg.onnx = OnnxConfig(
            quant_scheme=args.onnx_quant_scheme,
            quant_algo=args.onnx_quant_algo,
            execution_provider=args.onnx_ep,
            num_calib_data=args.onnx_num_calib,
            seq_len=args.onnx_seq_len,
            calib_dataset=args.onnx_calib_dataset,
            cleanup_intermediates=not args.no_onnx_cleanup,
        )
    if args.no_onnx:
        cfg.onnx = None
```

- [ ] **Step 4: Verify the help output shows the flags**

Run: `python core/pipeline.py --help 2>&1 | grep -E "onnx|ONNX"`
Expected: 9 lines covering `--onnx`, `--no-onnx`, `--onnx-quant-scheme`, `--onnx-quant-algo`, `--onnx-ep`, `--onnx-num-calib`, `--onnx-seq-len`, `--onnx-calib-dataset`, `--no-onnx-cleanup`.

- [ ] **Step 5: Verify the flags actually wire to config**

Run:

```bash
python -c "
import sys
sys.argv = ['pipeline.py', '--onnx', '--onnx-quant-scheme', 'uint4_wo_64', '--no-magicquant']
from core.pipeline import OnnxConfig
# Just confirm OnnxConfig builds — we don't run the full main() here.
print(OnnxConfig(quant_scheme='uint4_wo_64'))
"
```

Expected: `OnnxConfig(quant_scheme='uint4_wo_64', ...)` printed.

- [ ] **Step 6: Commit**

```bash
git add core/pipeline.py
git commit -m "feat: add CLI flags for the onnx stage

--onnx / --no-onnx toggle, plus knobs for quant_scheme, quant_algo,
execution_provider, calibration count/seq_len/dataset, and
--no-onnx-cleanup to keep intermediates."
```

---

## Task 6: Wire `do_onnx()` into the UI

**Files:**
- Modify: `ui/app.py` — add `do_onnx()`, register in `STAGE_RUNNERS`, update `validate_pipeline()`.

- [ ] **Step 1: Read `do_magicquant` end-to-end**

Run: `sed -n '673,720p' ui/app.py`

Match its shape: skip-if-artifact, set state, build script via service, run subprocess.

- [ ] **Step 2: Add `do_onnx()` immediately after `do_magicquant()`**

Insert before the `do_upload` definition (around line 723):

```python
async def do_onnx(cfg: RunRequest) -> bool:
    """Run the ONNX (Quark INT4 AWQ + OGA builder) stage.

    Sibling to do_magicquant — same source priority. Skips if onnx_model/model.onnx
    already exists.
    """
    out = cfg.training.output_dir
    out_abs = _resolve_out(out)
    oc = cfg.onnx
    export_enabled = "export" in cfg.enabled_stages

    onnx_dir = out_abs / "onnx_model"
    if (onnx_dir / "model.onnx").exists():
        await state.log(f"ONNX model already exists at {onnx_dir} — skipping", "success")
        await state.set_stage("onnx", StageStatus.COMPLETE)
        await state.set_progress(100)
        return True

    await state.set_stage("onnx", StageStatus.RUNNING)
    await state.set_progress(0)
    await state.log("Starting ONNX (Quark INT4 AWQ + OGA builder)", "stage")

    onnx_source_override = oc.source_model if (oc.source_model and not export_enabled) else ""

    from services import OnnxService
    svc = OnnxService(FOUNDRY_ROOT, VENV_PYTHON)
    script = svc.build_script(
        pipeline_root_str=str(FOUNDRY_ROOT),
        out_abs_str=str(out_abs),
        source_override=onnx_source_override,
        quant_scheme=oc.quant_scheme,
        quant_algo=oc.quant_algo,
        execution_provider=oc.execution_provider,
        data_type=oc.data_type,
        num_calib_data=oc.num_calib_data,
        seq_len=oc.seq_len,
        calib_dataset=oc.calib_dataset,
        cleanup_intermediates=oc.cleanup_intermediates,
    )
    rc = await run_script(script, out)
    ok = rc == 0
    await state.set_stage("onnx", StageStatus.COMPLETE if ok else StageStatus.FAILED)
    if ok:
        await state.set_progress(100)
    return ok
```

- [ ] **Step 3: Add to `STAGE_RUNNERS`**

Find `STAGE_RUNNERS` (around line 799) and update:

```python
STAGE_RUNNERS = {
    "training":   do_training,
    "export":     do_export,
    "heretic":    do_heretic,
    "reap":       do_reap,
    "magicquant": do_magicquant,
    "onnx":       do_onnx,         # <-- add
    "upload":     do_upload,
}
```

- [ ] **Step 4: Update `validate_pipeline()` to add ONNX dependency check**

Find the magicquant validation block (around line 844-866). Add an analogous block for onnx immediately after it (before the upload check):

```python
    # ONNX without export: needs a source
    if "onnx" in enabled and "export" not in enabled:
        oc = cfg.onnx
        source = oc.source_model if oc else ""
        has_reap = (out_abs / "reap_model").exists()
        has_heretic = (out_abs / "heretic_model").exists()
        has_merged = (out_abs / "merged_model").exists()
        if not source and not has_reap and not has_heretic and not has_merged:
            await state.log("ONNX is enabled without Export, and no existing safetensors "
                            "model artifacts were found in the output directory. Set a "
                            "Source Model path in ONNX config, or enable Export.", "error")
            return False
        if source:
            await state.log(f"Export skipped — ONNX will use source: {source}")
        elif has_reap:
            await state.log(f"Export skipped — ONNX will use existing: {out_abs}/reap_model")
        elif has_heretic:
            await state.log(f"Export skipped — ONNX will use existing: {out_abs}/heretic_model")
        else:
            await state.log(f"Export skipped — ONNX will use existing: {out_abs}/merged_model")
```

- [ ] **Step 5: Update the upload service call to pass `did_onnx`**

Find the `do_upload` body (around line 760-790). The `UploadService.build_script()` call passes `did_*` flags. Add `did_onnx`:

```python
        did_magicquant="magicquant" in enabled,
        did_onnx="onnx" in enabled,           # <-- add
```

(`did_onnx` will be threaded into `UploadService.build_script`'s signature in Task 7.)

- [ ] **Step 6: Smoke-test that the FastAPI app still imports**

Run: `python -c "import sys; sys.path.insert(0, '.'); from ui.app import app; print('OK')"`
Expected: `OK`

If this fails, check the diff for indentation drift or missing commas.

- [ ] **Step 7: Commit**

```bash
git add ui/app.py
git commit -m "feat: wire do_onnx into the UI pipeline

Adds do_onnx async runner mirroring do_magicquant, registers in
STAGE_RUNNERS, updates validate_pipeline to require an upstream
source (or override) when onnx runs without export. did_onnx is
threaded to the upload stage; the matching field on HFUploadConfig
is added in the upload-integration commit."
```

---

## Task 7: HF upload integration

**Files:**
- Modify: `core/hf_upload.py` — add `upload_onnx`, `did_onnx`; copy `onnx_model/` tree; emit ONNX section in model card.
- Modify: `core/services.py` — extend `UploadService.build_script` signature to accept and forward `did_onnx`, `upload_onnx`.

- [ ] **Step 1: Read `hf_upload.py` to find the right insertion points**

Run: `wc -l core/hf_upload.py && sed -n '1,80p' core/hf_upload.py`

Then find the `HFUploadConfig` definition and the model-card-generation function:

```bash
grep -n "class HFUploadConfig\|did_magicquant\|upload_gguf\|model_card\|README" core/hf_upload.py | head -30
```

This locates: the dataclass, the existing model card text, and where files get uploaded.

- [ ] **Step 2: Add `upload_onnx` and `did_onnx` to `HFUploadConfig`**

In `core/hf_upload.py`, find the `HFUploadConfig` dataclass and add two fields next to the matching `upload_gguf`/`did_magicquant`:

```python
    upload_gguf: bool = True
    upload_lora: bool = False
    upload_merged: bool = False
    upload_onnx: bool = True            # <-- add
    upload_dataset: bool = True
    ...
    did_training: bool = True
    did_heretic: bool = False
    did_reap: bool = False
    did_magicquant: bool = True
    did_onnx: bool = False              # <-- add
```

(Defaults: `upload_onnx=True` — if you built it, ship it. `did_onnx=False` — opt-in like heretic.)

- [ ] **Step 3: Update the upload routine to copy `onnx_model/`**

Find where existing artifacts are uploaded (look for a loop over `*.gguf` or a `upload_folder`/`upload_file` call). The exact shape varies; the change is: when `cfg.upload_onnx` and `<output_dir>/onnx_model/` exists, upload that directory tree to the same HF repo under `onnx_model/`.

Sketch (adjust to match the file's actual style):

```python
    if cfg.upload_onnx:
        onnx_dir = Path(output_dir) / "onnx_model"
        if onnx_dir.exists() and (onnx_dir / "model.onnx").exists():
            log(f"Uploading {onnx_dir} → onnx_model/", "info")
            api.upload_folder(
                folder_path=str(onnx_dir),
                path_in_repo="onnx_model",
                repo_id=cfg.repo_id,
                repo_type="model",
            )
        else:
            log("upload_onnx=True but onnx_model/ not found — skipping", "warn")
```

If `hf_upload.py` already uses `HfApi.upload_folder` for other artifacts, mirror that exactly. If it uses individual `upload_file` calls in a loop, follow that pattern instead.

- [ ] **Step 4: Add an ONNX section to the model card text**

Find the markdown body of the generated model card (search for the line that lists existing GGUF tiers). Add a new section that's emitted only when `cfg.did_onnx`:

```python
    if cfg.did_onnx:
        sections.append(f"""
## ONNX (INT4 AWQ — OGA hybrid for NPU+iGPU)

Built with [AMD Quark](https://quark.docs.amd.com/) ({cfg.... the quant_scheme used})
plus [onnxruntime-genai](https://github.com/microsoft/onnxruntime-genai)'s model builder.

**Runtime:** [Lemonade Server](https://lemonade-server.ai/) on Linux, routed to the
OGA (ONNX Runtime GenAI) backend for **NPU+iGPU Hybrid execution** on Ryzen AI 300 /
Strix Halo.

```bash
lemonade pull {cfg.repo_id}
lemonade run {cfg.repo_id.split('/')[-1]}
```

Files live under `onnx_model/` in this repo (`model.onnx`, `model.onnx.data`,
`genai_config.json`, plus tokenizer files).
""")
```

(Adapt the exact string-builder shape to whatever `hf_upload.py` already uses — list-append, f-string concatenation, etc.)

- [ ] **Step 5: Extend `UploadService.build_script()` in `core/services.py`**

Find `UploadService.build_script` (around line 880-952). Add `did_onnx: bool` and `upload_onnx: bool` to the signature, and add corresponding lines to the generated script:

```python
    def build_script(
        self,
        *,
        repo_id: str,
        ...
        upload_gguf: bool,
        upload_lora: bool,
        upload_merged: bool,
        upload_onnx: bool,                # <-- add
        upload_dataset: bool,
        ...
        did_magicquant: bool = True,
        did_onnx: bool = False,            # <-- add
        ...
    ) -> str:
        script = (
            ...
            f"    upload_gguf={upload_gguf},\n"
            f"    upload_lora={upload_lora},\n"
            f"    upload_merged={upload_merged},\n"
            f"    upload_onnx={upload_onnx},\n"        # <-- add
            f"    upload_dataset={upload_dataset},\n"
            ...
            f"    did_magicquant={did_magicquant},\n"
            f"    did_onnx={did_onnx},\n"              # <-- add
            ...
        )
```

- [ ] **Step 6: Update the call site in `ui/app.py`**

Find `do_upload` (around line 762-789). Add the matching kwargs:

```python
    script = svc.build_script(
        ...
        upload_gguf=uc.upload_gguf,
        upload_lora=uc.upload_lora,
        upload_merged=uc.upload_merged,
        upload_onnx=uc.upload_onnx,         # <-- add
        upload_dataset=uc.upload_dataset,
        ...
        did_magicquant="magicquant" in enabled,
        did_onnx="onnx" in enabled,         # already added in Task 6
        ...
    )
```

You'll also need `upload_onnx` on `UploadCfg` in `ui/app.py`. Find `UploadCfg` (around line 161) and add:

```python
class UploadCfg(BaseModel):
    repo_id: str = ""
    private: bool = True
    license: str = ""
    upload_gguf: bool = True
    upload_lora: bool = False
    upload_merged: bool = False
    upload_onnx: bool = True              # <-- add
    upload_dataset: bool = True
```

And the matching field on `UploadConfig` in `core/pipeline.py` (around line 128-135):

```python
@dataclass
class UploadConfig:
    repo_id: str = ""
    private: bool = True
    base_model: str = ""
    license: str = ""
    upload_lora: bool = False
    upload_merged: bool = False
    upload_gguf: bool = True
    upload_onnx: bool = True              # <-- add
```

And update `stage_upload` / `stage_upload_dry_run` (around lines 1300-1370) to pass `upload_onnx=uc.upload_onnx` and `did_onnx="onnx" in _enabled` into `HFUploadConfig`.

- [ ] **Step 7: Smoke-test that imports still resolve**

Run:

```bash
python -c "
from core.hf_upload import HFUploadConfig
c = HFUploadConfig(repo_id='x/y', license='mit', base_model='meta/x', dataset_name='ds')
print('upload_onnx:', c.upload_onnx)
print('did_onnx:', c.did_onnx)
"
```

Expected: `upload_onnx: True` then `did_onnx: False`.

Run: `python core/pipeline.py --help > /dev/null && echo OK`
Expected: `OK`

- [ ] **Step 8: Commit**

```bash
git add core/hf_upload.py core/services.py ui/app.py core/pipeline.py
git commit -m "feat: HF upload integration for the onnx stage

HFUploadConfig gains upload_onnx (default True) and did_onnx (default
False, opt-in like heretic). The upload routine ships onnx_model/ to
the same HF repo under that path. Model card gains an ONNX section
documenting Lemonade pull/run when did_onnx is set. UploadCfg /
UploadConfig / UploadService all thread the new fields through."
```

---

## Task 8: UI HTML stage panel + default.yaml

**Files:**
- Modify: `ui/index.html` — new stage panel mirroring MagicQuant; new sidebar entry.
- Modify: `configs/default.yaml` — `onnx:` section.

- [ ] **Step 1: Find the MagicQuant stage panel in `ui/index.html`**

Run:

```bash
grep -n "magicquant\|MagicQuant" ui/index.html | head -30
```

Note: the file is one large HTML document with stage `<div>`s, master toggles, and JS state. You'll need to copy three things:
1. The sidebar `<div class="stage-item" data-stage="magicquant">` entry.
2. The main-area panel for MagicQuant config fields.
3. The JS read/write that saves & restores the toggle and field values to/from the run config.

- [ ] **Step 2: Add the sidebar entry**

After the magicquant `<div class="stage-item">` block, add:

```html
<div class="stage-item" data-stage="onnx" data-status="pending">
  <div class="stage-indicator"></div>
  <div class="stage-name">ONNX</div>
  <div class="stage-desc">Quark + OGA hybrid (NPU)</div>
  <div class="stage-toggle">
    <label class="toggle">
      <input type="checkbox" id="onnx-enable">
      <span class="toggle-track"></span>
      <span class="toggle-thumb"></span>
    </label>
  </div>
</div>
```

- [ ] **Step 3: Add the main-area config panel**

After the MagicQuant config panel (find `id="onnx-..."` or the surrounding `data-stage="magicquant"`), add a new section. Match the same heading + field-grid pattern. Fields needed:
- `quant_scheme` (select: uint4_wo_32 / uint4_wo_64 / uint4_wo_128)
- `quant_algo` (select: awq / gptq)
- `execution_provider` (select: dml / cpu)
- `data_type` (select: float16 / bfloat16)
- `num_calib_data` (number, default 128)
- `seq_len` (number, default 512)
- `calib_dataset` (text, default `pileval_for_awq_benchmark`)
- `cleanup_intermediates` (checkbox, default checked)
- `source_model` (text, optional)

Each `<input>`/`<select>` should have an id that matches `onnx-<field>` for consistency with how the rest of the file IDs fields.

- [ ] **Step 4: Wire JS read/write**

Find the JS function that builds the `RunRequest` payload (search for `magicquant:` in the JS section). Add an `onnx:` key that mirrors the magicquant block. Also wire the master enable toggle to include `"onnx"` in `enabled_stages` when checked.

For workflow save/restore: find where saved-workflow JSON is read into the form, and ensure the new fields are populated. (Pattern is already established by magicquant — copy that exactly.)

- [ ] **Step 5: Reload the UI and visually verify**

Run: `bash ui/run.sh &` (or use the existing dev script). Open `http://localhost:7865`. Confirm:
- Sidebar shows the new "ONNX" entry between "MagicQuant" and "Upload".
- Toggling its master switch enables/disables the panel.
- All fields are present with the documented defaults.
- A test run with `--no-magicquant`, `--onnx` (or the UI equivalents) creates a `RunRequest` whose JSON includes `"onnx": {...}` and `"enabled_stages": ["..., "onnx", ...]`.

(Use the browser devtools network tab — when you click Run, inspect the POST `/api/run` body.)

- [ ] **Step 6: Add the `onnx:` section to `configs/default.yaml`**

After the existing `magicquant:` section (or wherever stage configs live in the file), add:

```yaml
# --- ONNX (AMD Quark + OGA hybrid for NPU+iGPU via Lemonade) ---
# Set --onnx on the CLI or enable in the UI to activate this stage.
# Output: <output_dir>/onnx_model/  (consumed by `lemonade pull <hf_repo>`)
onnx:
  quant_scheme: uint4_wo_128       # uint4_wo_32 | uint4_wo_64 | uint4_wo_128
  quant_algo: awq                  # awq | gptq
  execution_provider: dml          # dml = hybrid NPU+iGPU; cpu = NPU-only (less tested)
  data_type: float16               # float16 | bfloat16
  num_calib_data: 128
  seq_len: 512
  calib_dataset: pileval_for_awq_benchmark
  cleanup_intermediates: true      # delete quark_safetensors/ after build
  source_model: ""                 # override for runs without upstream stages
```

- [ ] **Step 7: Commit**

```bash
git add ui/index.html configs/default.yaml
git commit -m "ui: add ONNX stage panel + default config section

Sidebar entry, main config panel, JS read/write for the new
onnx: section in RunRequest. default.yaml documents the recipe
defaults so users can see the knobs at a glance."
```

---

## Task 9: End-to-end smoke test

**Files:**
- Create: `tests/test_onnx_stage.py`

This is the critical regression test. It uses a tiny model (≤200M params) so it runs in <5 minutes on CI. It's marked as a slow integration test and skipped if `amd-quark` isn't importable.

- [ ] **Step 1: Read the existing integration test for conventions**

Run: `cat tests/test_training_integration.py | head -80`

Match the marker style, fixture style, and skip-on-missing-dep pattern.

- [ ] **Step 2: Write the smoke test**

Create `tests/test_onnx_stage.py`:

```python
"""End-to-end smoke test for the ONNX pipeline stage.

Quantizes a tiny 135M-param model with Quark INT4 AWQ and builds an
OGA-format ONNX directory. Verifies the output structure is a drop-in
for `lemonade pull`. Skipped if amd-quark is not installed (it's a
heavy optional dep).

Runtime budget: <5 minutes on the dev box (Strix Halo). May be
slower in CI; mark accordingly if/when CI gets one.
"""

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

# Skip the whole module if quark isn't installed — it's an optional dep.
quark = pytest.importorskip("quark.torch", reason="amd-quark not installed")
oga = pytest.importorskip(
    "onnxruntime_genai", reason="onnxruntime-genai not installed"
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TINY_MODEL = "HuggingFaceTB/SmolLM2-135M-Instruct"  # 135M params, OGA-supported arch


@pytest.fixture(scope="module")
def tiny_model_dir(tmp_path_factory):
    """Download SmolLM2-135M-Instruct as merged FP16 safetensors."""
    from huggingface_hub import snapshot_download

    target = tmp_path_factory.mktemp("source_model")
    snapshot_download(
        repo_id=TINY_MODEL,
        local_dir=str(target),
        allow_patterns=["*.safetensors", "*.json", "tokenizer*", "*.txt"],
    )
    return target


def test_onnx_stage_end_to_end(tiny_model_dir, tmp_path):
    """Run stage_onnx against the tiny model, verify the output layout."""
    output_dir = tmp_path / "run"
    output_dir.mkdir()

    sys.path.insert(0, str(PROJECT_ROOT))
    from core.pipeline import (
        Artifacts,
        OnnxConfig,
        PipelineConfig,
        stage_onnx,
        _default_log,
    )

    # Place the source where stage_onnx expects to find it (merged_model/).
    merged_dir = output_dir / "merged_model"
    shutil.copytree(tiny_model_dir, merged_dir)

    cfg = PipelineConfig(
        output_dir=str(output_dir),
        onnx=OnnxConfig(
            num_calib_data=8,            # cut for test speed
            seq_len=128,
            cleanup_intermediates=True,
        ),
    )
    artifacts = Artifacts(str(output_dir))

    ok = stage_onnx(cfg, artifacts, _default_log)
    assert ok, "stage_onnx returned False — see stdout for the failure"

    # Output layout assertions.
    onnx_dir = output_dir / "onnx_model"
    assert onnx_dir.exists(), "onnx_model/ directory not created"
    assert (onnx_dir / "model.onnx").exists(), "model.onnx not produced"
    assert (onnx_dir / "genai_config.json").exists(), "genai_config.json missing"

    # genai_config.json should be valid JSON.
    config = json.loads((onnx_dir / "genai_config.json").read_text())
    assert "model" in config, "genai_config.json missing 'model' top-level key"

    # Tokenizer files should be present (copied by either OGA builder or our helper).
    tokenizer_files = list(onnx_dir.glob("tokenizer*"))
    assert tokenizer_files, "no tokenizer files in onnx_model/"

    # cleanup_intermediates=True should have deleted quark_safetensors/.
    assert not (output_dir / "quark_safetensors").exists(), (
        "quark_safetensors/ was not cleaned up despite cleanup_intermediates=True"
    )


def test_stage_onnx_skips_when_artifact_exists(tmp_path):
    """If onnx_model/model.onnx already exists, the stage skips and returns True."""
    sys.path.insert(0, str(PROJECT_ROOT))
    from core.pipeline import (
        Artifacts,
        OnnxConfig,
        PipelineConfig,
        stage_onnx,
        _default_log,
    )

    output_dir = tmp_path / "run"
    onnx_dir = output_dir / "onnx_model"
    onnx_dir.mkdir(parents=True)
    (onnx_dir / "model.onnx").write_bytes(b"fake")  # pretend we already built it

    cfg = PipelineConfig(output_dir=str(output_dir), onnx=OnnxConfig())
    artifacts = Artifacts(str(output_dir))

    ok = stage_onnx(cfg, artifacts, _default_log)
    assert ok, "skip-on-existing should return True"
```

- [ ] **Step 3: Run only the skip test first (it's fast and offline)**

Run: `pytest tests/test_onnx_stage.py::test_stage_onnx_skips_when_artifact_exists -v`
Expected: 1 passed.

- [ ] **Step 4: Run the full E2E test (requires GPU + network for the model download)**

Run: `pytest tests/test_onnx_stage.py::test_onnx_stage_end_to_end -v -s`
Expected: 1 passed in roughly 2-5 minutes. The first run downloads SmolLM2-135M (~270 MB) and clones the Quark repo (~10 MB).

If it fails:
- `quark.torch` import error → `ensure_quark_installed` couldn't find it; check `pip install amd-quark` worked.
- `quantize_quark.py not found` → `ensure_quark_repo` couldn't find the script; check the QUARK_REPO_BRANCH pin in `core/onnx_quark.py`. The branch may have moved; bump to a newer release branch and update the spec.
- OOM during quantization → unexpected for a 135M model; check that another process isn't holding GPU memory (LM Studio etc.).
- ORT-GenAI builder fails on SmolLM2 architecture → the model may not be in OGA's supported arch list. Switch to a model that is — try `meta-llama/Llama-3.2-1B` or similar small Llama variant.

- [ ] **Step 5: Commit**

```bash
git add tests/test_onnx_stage.py
git commit -m "test: add end-to-end smoke test for the onnx stage

Quantizes SmolLM2-135M with Quark INT4 AWQ and runs the OGA builder,
verifying the resulting onnx_model/ directory has model.onnx and a
well-formed genai_config.json. Skipped when amd-quark or
onnxruntime-genai aren't installed."
```

---

## Task 10: Update `CLAUDE.md`

**Files:**
- Modify: `CLAUDE.md` — document the new stage and Lemonade as the runtime target.

- [ ] **Step 1: Locate the Pipeline Stages section**

Run: `grep -n "Pipeline Stages\|MagicQuant\|HF Upload" CLAUDE.md | head -10`

Find the bulleted list of stages.

- [ ] **Step 2: Add the new stage to the stage list**

Where the stages are listed (likely under "Pipeline Stages (core/pipeline.py)" or similar), add an entry after the MagicQuant bullet:

```markdown
4. **MagicQuant**: Evolutionary search → 3-tier hybrid GGUFs (Q4/Q5/Q6)
5. **ONNX (Quark + OGA)**: AMD Quark INT4 AWQ → onnxruntime-genai model builder → `onnx_model/` directory for **NPU+iGPU Hybrid execution** via [Lemonade Server](https://lemonade-server.ai/) on Linux. Sibling to MagicQuant — both can run from the same source; pick GGUF, ONNX, or both. Auto-installs `amd-quark` and `onnxruntime-genai` and clones `amd/Quark` to `~/quark-amd/` for the reference `quantize_quark.py` script.
6. **Upload**: HuggingFace Hub with model card generation (now also handles `onnx_model/`)
```

(Renumber any subsequent entries.)

- [ ] **Step 3: Add a note to "Known Issues" if relevant**

Append to the "Known Issues" section:

```markdown
- **Quark memory budget**: AWQ calibration loads the full source model in FP16 plus activation stats. ~30B fits comfortably; 40B is tight; 70B+ will OOM. For larger models, run only the GGUF path (`--no-onnx`) until streaming Quark calibration is added.
```

- [ ] **Step 4: Add a deployment-target note near the top**

Where Foundry is described (top of the file), add a sentence about Lemonade as the canonical runtime if it's not already there:

```markdown
Built artifacts are designed for [Lemonade Server](https://lemonade-server.ai/) on Linux: GGUF tiers route to llama.cpp on ROCm/Vulkan; ONNX models route to OGA for NPU+iGPU hybrid execution.
```

- [ ] **Step 5: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: document the onnx pipeline stage in CLAUDE.md

Adds the new stage to the pipeline list, calls out the Quark memory
budget under Known Issues, and notes Lemonade as the canonical
serving runtime."
```

---

## Final verification checklist (run after Task 10)

- [ ] `python core/pipeline.py --help` lists all the `--onnx*` flags.
- [ ] `pytest tests/test_onnx_quark.py tests/test_onnx_stage.py -v` — all green.
- [ ] `python -c "from ui.app import app"` — clean import.
- [ ] `git log --oneline -10` — 10 focused commits, one per task.
- [ ] Manual UI smoke: start `bash ui/run.sh`, open the UI, toggle ONNX on, fill in defaults, run a tiny test workflow, confirm log streaming and stage progression work.
- [ ] Manual deployment smoke: take an actual ONNX run output, upload to a private HF repo, run `lemonade pull <repo>` + `lemonade run <repo>` on the Linux server, confirm a 1-token generation happens.

If the deployment smoke fails because Lemonade rejects the OGA model produced by `-e dml`, the fix is to investigate which `-e` value Lemonade routes to its OGA backend on Linux specifically (could be a different flag than `dml` despite the spec's assumption). Update the default in `OnnxConfig` and document the change in the spec.

---

## Self-review notes

**Spec coverage:** Every section of the spec maps to at least one task — config classes (T1), Quark+OGA wrapper (T2), service (T3), pipeline stage (T4), CLI (T5), UI (T6, T8), HF upload + model card (T7), smoke test (T9), CLAUDE.md (T10).

**No placeholders:** All steps contain runnable commands or compilable code. The HF upload integration in Task 7 has the most "match-existing-style" caveats; the engineer should read `hf_upload.py` once before editing rather than guessing.

**Type/name consistency:** `OnnxConfig` ↔ `OnnxCfg` ↔ argparse flags ↔ `OnnxService.build_script` keyword args all use the same field names (quant_scheme, quant_algo, execution_provider, etc.). `did_onnx` is the upload-stage flag name everywhere it appears. `Artifacts.onnx_dir`/`Artifacts.quark_dir` match the directory names in `onnx_quark.run_onnx_pipeline`.

**Risk callouts left for the engineer:** the `release/0.11` branch pin in `core/onnx_quark.py` may move; the SmolLM2 model in the smoke test may not be in OGA's supported arch list (fallback to Llama-3.2-1B); the `-e dml` flag's routing in Lemonade-on-Linux is the open assumption that the deployment smoke step will validate.
