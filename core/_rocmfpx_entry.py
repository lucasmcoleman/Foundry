"""Importable entry module for the ROCmFPX quantization stage.

``ROCmFPXService.build_script`` emits a thin shim that writes a JSON config
and invokes ``core/_rocmfpx_entry.py:run()``. The heavy work (ROCmFPX
discovery/auto-install/build, BF16 GGUF conversion, per-format quantize)
lives here as ordinary Python, mirroring ``_magicquant_entry.py``'s shape.

Module import is stdlib-only so config/format-mapping/path-resolution helpers
are unit-testable without a real ROCmFPX checkout.

ROCmFPX auto-install is pinned to a known commit (audit L-supply-chain
convention, matching ``_magicquant_entry.LLAMACPP_PIN``): a specific SHA, not
a floating branch.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROCMFPX_REPO = "https://github.com/ciru-ai/ROCmFPX.git"
ROCMFPX_PIN = "221402af8574faf652b101b6afe225a3f329561f"  # known-good commit; bump deliberately

# A build lacking these doesn't carry the full ROCmFPX family (e.g. a plain
# ROCmFP4-only rocmfp4-llama checkout) and is rejected as a discovery hit.
REQUIRED_TYPES = ("Q3_0_ROCMFPX", "Q6_0_ROCMFPX", "Q8_0_ROCMFPX")

# FORMAT/PROFILE -> GGML type name, from ROCmFPX's README quantize table.
FORMAT_TABLE = {
    ("rocmfp3", "straight"): "Q3_0_ROCMFPX",
    ("rocmfp3", "agent"): "Q3_0_ROCMFPX_AGENT",
    ("rocmfp4", "straight"): "Q4_0_ROCMFP4",
    ("rocmfp4", "agent"): "Q4_0_ROCMFP4_COHERENT",
    ("rocmfp6", "straight"): "Q6_0_ROCMFPX",
    ("rocmfp6", "agent"): "Q6_0_ROCMFPX_AGENT",
    ("rocmfp8", "straight"): "Q8_0_ROCMFPX",
    ("rocmfp8", "agent"): "Q8_0_ROCMFPX_AGENT",
}

# MagicQuant scheme name -> ROCmFPX-family ggml type, rounding UP in quality so
# MagicQuant's per-group sensitivity intent is preserved (a group MagicQuant
# kept at Q6 shouldn't drop to fp4). Float/high-precision groups pass through
# unchanged (the ROCmFPX fork loads stock ggml types too). ROCmFPX-native
# scheme names (from a search run with --magicquant-rocmfpx) map to themselves.
SCHEME_TO_ROCMFPX = {
    "BF16": "BF16", "F16": "F16", "F32": "F32",
    "Q8_0": "Q8_0_ROCMFPX",
    "Q6_K": "Q6_0_ROCMFPX", "Q5_K": "Q6_0_ROCMFPX",
    "Q4_K_M": "Q4_0_ROCMFP4", "Q4_K": "Q4_0_ROCMFP4",
    "IQ4_NL": "Q4_0_ROCMFP4", "MXFP4_MOE": "Q4_0_ROCMFP4", "MXFP4": "Q4_0_ROCMFP4",
    "Q3_K": "Q3_0_ROCMFPX", "Q2_K": "Q3_0_ROCMFPX",
    "ROCMFP8": "Q8_0_ROCMFPX", "ROCMFP6": "Q6_0_ROCMFPX",
    "ROCMFP4": "Q4_0_ROCMFP4", "ROCMFP3": "Q3_0_ROCMFPX",
    # Opt-in MagicQuant IQ (importance-quant) schemes round UP to the nearest
    # ROCmFPX family type at-or-above their bit width; Q3_0_ROCMFPX is the
    # smallest ROCmFPX type, so every sub-3-bit IQ scheme bottoms out there.
    "IQ4_XS": "Q4_0_ROCMFP4",
    "IQ3_S": "Q3_0_ROCMFPX", "IQ3_XXS": "Q3_0_ROCMFPX",
    "IQ2_S": "Q3_0_ROCMFPX", "IQ2_XS": "Q3_0_ROCMFPX", "IQ2_XXS": "Q3_0_ROCMFPX",
    "IQ1_M": "Q3_0_ROCMFPX", "IQ1_S": "Q3_0_ROCMFPX",
}

# Quality order (best first) for picking a base type when a tier's groups
# disagree — used only as the default for tensors no group pattern covers.
_ROCMFPX_QUALITY_ORDER = [
    "BF16", "F16", "Q8_0_ROCMFPX", "Q6_0_ROCMFPX", "Q4_0_ROCMFP4",
    "Q3_0_ROCMFPX", "F32",
]

# tg (token generation) is memory-bandwidth-bound: fewer bytes streamed per
# token = faster, ~linearly. Q3_0_ROCMFPX and Q6_0_ROCMFPX are tg-SLOW on top
# of that: cross-word bit-stitch + __constant__ codebook LUT decode, and
# untuned occupancy (nwarps=1 vs ROCmFP4's 2) -- no AMD-ISA fast path. Their
# tg-fast siblings, Q4_0_ROCMFP4 and Q8_0_ROCMFPX, decode via register-permute
# / plain int8 dp4a and are meaningfully faster per token, at the cost of a
# few more bits per weight (bigger file, more bytes to stream -- but the
# bit-stitch/LUT tax dwarfs that extra bandwidth in wall-clock tg terms).
# Q4_0_ROCMFP4/Q4_0_ROCMFP4_FAST/Q8_0_ROCMFPX are already tg-fast and map to
# themselves (no steering needed).
TG_SAFE_ROCMFPX = {
    "Q3_0_ROCMFPX": "Q4_0_ROCMFP4",
    "Q6_0_ROCMFPX": "Q8_0_ROCMFPX",
}

# Groups steered by tg_safe: the bandwidth-heavy bulk that dominates tg (FFN
# up/down + MoE experts). Attention/embeddings/head groups (Q/K/O/E/H/...)
# keep faithful translation even under tg_safe -- steering only the
# high-traffic groups captures most of the tg win while limiting the size cost.
TG_SAFE_GROUPS = {"U", "D", "X"}

# Float types are non-quantizing: passing one as llama-quantize's positional
# base ftype makes the whole op a no-op copy that SKIPS the per-tensor
# overrides. The base must therefore be a quantizing type; these are excluded.
_NON_QUANTIZING = {"BF16", "F16", "F32"}
# Highest-quality quantizing base to fall back to if a tier is all-float.
_DEFAULT_QUANTIZING_BASE = "Q8_0_ROCMFPX"


def parse_config(cfg_path: str) -> dict:
    return json.loads(Path(cfg_path).read_text())


def parse_mq_spec(spec: str) -> str | None:
    """Return the MagicQuant tier for an ``mq-<tier>`` spec, else None.

    ``mq-q4`` -> ``"Q4"``, ``mq-q6`` -> ``"Q6"``. Case-insensitive. A plain
    preset spec (``rocmfp4-agent``) returns None so the caller routes it to
    the uniform-preset path.
    """
    s = spec.strip().lower()
    if not s.startswith("mq-"):
        return None
    tier = s[len("mq-"):]
    return tier.upper() if tier else None


def tg_safe_rocmfpx(rocmfpx_type: str) -> str:
    """Steer a tg-slow ROCmFPX fork type to its nearest tg-fast sibling.

    tg (token generation) is memory-bandwidth-bound: fewer bytes streamed per
    token = faster, ~linearly. ``Q3_0_ROCMFPX`` and ``Q6_0_ROCMFPX`` are
    tg-SLOW regardless of that: cross-word bit-stitch + ``__constant__``
    codebook LUT decode, plus untuned occupancy (nwarps=1 vs ROCmFP4's 2) --
    no AMD-ISA fast path. ``Q4_0_ROCMFP4`` and ``Q8_0_ROCMFPX`` decode via
    register-permute / plain int8 dp4a and are meaningfully faster per token.

    Tradeoff: the steered type is slightly *bigger* (Q3->Q4, Q6->Q8, a few
    more bits/weight, more bytes on disk and in the KV-adjacent weight
    stream) in exchange for a much faster tg decode path -- the bit-stitch/LUT
    tax the slow types pay dwarfs that extra bandwidth in wall-clock tg terms.
    Types already tg-fast (``Q4_0_ROCMFP4``, ``Q4_0_ROCMFP4_FAST``,
    ``Q8_0_ROCMFPX``) map to themselves -- no steering needed.
    """
    return TG_SAFE_ROCMFPX.get(rocmfpx_type, rocmfpx_type)


def translate_scheme(scheme: str, tg_safe: bool = False) -> str:
    """Translate a MagicQuant scheme name to its ROCmFPX-family ggml type.

    ``tg_safe`` (opt-in, default off) steers the faithful translation through
    ``tg_safe_rocmfpx`` to its tg-fast sibling instead of returning it as-is.
    Callers decide *which* groups get steered (see ``build_tensor_type_lines``,
    which only steers the high-traffic FFN/expert groups) -- this function
    just applies the steering when asked.
    """
    if scheme not in SCHEME_TO_ROCMFPX:
        raise ValueError(
            f"No ROCmFPX translation for MagicQuant scheme {scheme!r}. "
            f"Known: {sorted(SCHEME_TO_ROCMFPX)}"
        )
    ggml_type = SCHEME_TO_ROCMFPX[scheme]
    if tg_safe:
        ggml_type = tg_safe_rocmfpx(ggml_type)
    return ggml_type


def pick_base_type(config: dict) -> str:
    """Pick the positional base ggml type for the quantize call.

    MUST be a quantizing type: llama-quantize with a float base ftype
    (BF16/F16) is a no-op copy that never applies the per-tensor overrides, so
    the whole hybrid would ship uncompressed (learned the hard way — a BF16
    base produced a 69 GB "Q4" with quant size == model size). Every searched
    group has an explicit override, so the base only governs tensors no pattern
    covers (norms, which stay at source precision anyway); pick the
    highest-quality *quantizing* type present so any stray tensor errs toward
    precision. Falls back to a high-bit quantizing base for an all-float tier.
    """
    translated = {translate_scheme(s) for s in config.values()}
    for t in _ROCMFPX_QUALITY_ORDER:
        if t in translated and t not in _NON_QUANTIZING:
            return t
    return _DEFAULT_QUANTIZING_BASE


def build_tensor_type_lines(config: dict, group_patterns: dict, tg_safe: bool = False) -> list[str]:
    """Emit ``<regex>=<TYPE>`` lines for a per-group config.

    ``config`` maps group letters (E/H/Q/K/O/U/D/X/R/S) to MagicQuant scheme
    names; ``group_patterns`` is MagicQuant's ``TensorGroupClassifier.
    GROUP_PATTERNS`` (ordered dict, first-match-wins). Lines are emitted in the
    classifier's own group order so llama-quantize's first-match regex
    semantics reproduce the classifier's assignment (e.g. the X pattern
    ``ffn_(up|gate|down)_exps`` precedes the U pattern ``ffn_up``, so fused
    expert tensors resolve to X, not U).

    ``tg_safe`` (opt-in, default off -- faithful translation stays the
    default): when True, the high-traffic groups (``TG_SAFE_GROUPS`` -- FFN
    up/down + MoE experts, the bandwidth-heavy bulk that dominates tg) get
    their translated type steered through ``tg_safe_rocmfpx`` to its nearest
    tg-fast sibling. Attention/embedding/head groups (Q/K/O/E/H/...) always
    keep the faithful translation, steered or not -- limiting the size cost
    while still capturing most of the tg win.
    """
    lines: list[str] = []
    for group, patterns in group_patterns.items():
        if group not in config:
            continue  # groups the search didn't vary (N norms, V vision) keep defaults
        steer = tg_safe and group in TG_SAFE_GROUPS
        ggml_type = translate_scheme(config[group], tg_safe=steer)
        for pat in patterns:
            lines.append(f"{pat}={ggml_type}")
    return lines


def parse_format_spec(spec: str) -> tuple[str, str]:
    """Split a ``"<format>-<profile>"`` spec into ``(format, profile)``.

    Profile defaults to ``"straight"`` when omitted (e.g. ``"rocmfp3"``).
    Raises ``ValueError`` for an unknown format/profile combination.
    """
    parts = spec.strip().lower().split("-", 1)
    fmt = parts[0]
    profile = parts[1] if len(parts) > 1 else "straight"
    if (fmt, profile) not in FORMAT_TABLE:
        valid = ", ".join(f"{f}-{p}" for f, p in FORMAT_TABLE)
        raise ValueError(f"Unknown ROCmFPX format spec {spec!r} (valid: {valid})")
    return fmt, profile


def _has_full_family(quantize_bin: Path) -> bool:
    """True if this ``llama-quantize`` carries the full ROCmFPX family (not
    just a ROCmFP4-only build, e.g. a plain rocmfp4-llama checkout).
    """
    import subprocess

    try:
        out = subprocess.run(
            [str(quantize_bin), "--help"], capture_output=True, text=True, timeout=30,
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return False
    return all(t in out for t in REQUIRED_TYPES)


def find_rocmfpx(hint: str = "") -> str | None:
    """Return a ROCmFPX build dir containing a full-family llama-quantize, or None."""
    import os

    candidates = [
        hint,
        os.environ.get("ROCMFPX_PATH", ""),
        str(Path.home() / "ROCmFPX"),
        "./ROCmFPX",
        # This box's known ROCmFP4-focused sibling build -- only usable if it
        # happens to carry the full FP3/FP6/FP8 family too.
        "/server/ai/strix-halo-club/engines/rocmfp4-llama-src",
    ]
    for p in candidates:
        if not p:
            continue
        pp = Path(p)
        for sub in ("build-strix-rocmfp4/bin/llama-quantize", "build/bin/llama-quantize"):
            qbin = pp / sub
            if qbin.exists() and _has_full_family(qbin):
                return str(pp)
    return None


def _find_rocm_sdk_devel() -> str | None:
    """Locate a ``_rocm_sdk_devel`` pip package (CMake HIP-lang support)
    somewhere on this box, honoring ``ROCM_SDK_DEVEL_PATH`` first.
    """
    import os

    devel = os.environ.get("ROCM_SDK_DEVEL_PATH", "")
    if devel:
        return devel
    for cand in Path("/server").glob("*/*/.venv/lib/*/site-packages/_rocm_sdk_devel"):
        return str(cand)
    return None


def _overlay_missing(dst: Path, src: Path) -> None:
    """Symlink each top-level entry of ``src`` into ``dst`` that ``dst``
    doesn't already have (additive only -- never touches an existing entry).
    """
    if not src.exists():
        return
    for entry in src.iterdir():
        target = dst / entry.name
        if not target.exists():
            target.symlink_to(entry)
            print(f"  overlaid {target} -> {entry}", flush=True)


def _ensure_hip_lang_cmake() -> None:
    """Work around a TheRock pip-ROCm packaging split: ``rocm-sdk-core``
    (this venv's HIP runtime, where ``hipcc`` lives) ships no CMake package
    files or hipBLAS/etc. headers at all -- the CMake support
    (``hip-lang-config.cmake`` and friends) and the missing headers live in
    the separate ``rocm-sdk-devel`` wheel. CMake's own HIP-language detection
    derives the "ROCm root" strictly from ``hipcc``'s location and looks for
    ``hip-lang-config.cmake`` only inside that root (not searchable via
    ``CMAKE_PREFIX_PATH``), so the fix is a symlink overlay, not an env var.

    Best-effort and idempotent: does nothing if no devel package can be found
    anywhere on the box (the build then fails with CMake's/the compiler's own
    clear error rather than silently skipping).
    """
    try:
        import _rocm_sdk_core
    except ImportError:
        return
    core_root = Path(_rocm_sdk_core.__file__).resolve().parent
    devel = _find_rocm_sdk_devel()
    if not devel:
        return
    devel_root = Path(devel)
    _overlay_missing(core_root / "lib", devel_root / "lib")
    _overlay_missing(core_root / "include", devel_root / "include")
    _alias_versioned_libs(core_root / "lib", devel_root / "lib" / "cmake")


def _alias_versioned_libs(core_lib: Path, devel_cmake: Path) -> None:
    """The devel package's *-targets*.cmake files were generated against its
    own build and hardcode exact ``libFOO.so.<version>-<buildhash>`` filenames
    as ``IMPORTED_LOCATION``. This venv's matching ``rocm-sdk-core`` ships the
    same libraries but only under the plain soname (``libFOO.so.<major>``) --
    same binaries (verified: identical file size for libamdhip64), just a
    different wheel-packaging naming convention. Symlink each missing
    hardcoded name to the plain soname that is its prefix, so CMake's
    IMPORTED_LOCATION existence check passes without vendoring or rebuilding
    anything.
    """
    import re

    pattern = re.compile(r'IMPORTED_LOCATION\w*\s+"\$\{_IMPORT_PREFIX\}/lib/([\w.+-]+\.so\.[\w.-]+)"')
    required = set()
    for cmake_file in devel_cmake.rglob("*.cmake"):
        required.update(pattern.findall(cmake_file.read_text(errors="ignore")))

    existing = {p.name for p in core_lib.iterdir() if p.is_file() or p.is_symlink()}
    for name in sorted(required):
        if name in existing:
            continue
        # Longest existing name that is a strict prefix of the required one --
        # e.g. "libamdhip64.so.7" is the real file behind "libamdhip64.so.7.2.53150-aee46ad448".
        candidates = [e for e in existing if name.startswith(e) and e != name]
        if not candidates:
            continue
        real = max(candidates, key=len)
        (core_lib / name).symlink_to(real)
        print(f"  aliased {name} -> {real}", flush=True)


def _build_env() -> dict:
    """Subprocess env for the C++ build.

    ``enable_language(HIP)``'s ROCm-root auto-detection needed the
    ``lib/cmake`` symlink (``_ensure_hip_lang_cmake``); the ordinary
    ``find_package(hip)`` CMake calls that follow it need
    ``_rocm_sdk_core``'s root on ``CMAKE_PREFIX_PATH`` instead -- both are
    needed, for two different CMake lookup mechanisms.
    """
    import os

    env = dict(os.environ)
    try:
        import _rocm_sdk_core
        core_root = str(Path(_rocm_sdk_core.__file__).resolve().parent)
        prefix = env.get("CMAKE_PREFIX_PATH", "")
        env["CMAKE_PREFIX_PATH"] = f"{core_root}{os.pathsep}{prefix}" if prefix else core_root
    except ImportError:
        pass
    # This pip-packaged HIP has no hipconfig executable to auto-detect the
    # platform from; hip-config.cmake falls back to $ENV{HIP_PLATFORM} and
    # hard-errors if neither is set.
    env.setdefault("HIP_PLATFORM", "amd")
    return env


def ensure_rocmfpx(hint: str = "") -> str | None:
    """Find a full-family ROCmFPX build, auto-installing a pinned one if absent.

    Returns the install dir, or None if discovery + install both failed.
    """
    import multiprocessing
    import subprocess

    rocmfpx = find_rocmfpx(hint)
    if rocmfpx:
        return rocmfpx

    install_dir = Path.home() / "ROCmFPX"
    rc = 0
    if (install_dir / ".git").exists():
        print(f"Reusing existing checkout at {install_dir}", flush=True)
    else:
        print("ROCmFPX not found -- auto-installing (pinned commit)...", flush=True)
        rc = subprocess.run(["git", "clone", ROCMFPX_REPO, str(install_dir)]).returncode
        if rc == 0:
            rc = subprocess.run(["git", "checkout", ROCMFPX_PIN], cwd=str(install_dir)).returncode
    if rc == 0:
        _ensure_hip_lang_cmake()
        jobs = str(multiprocessing.cpu_count())
        env = _build_env()
        env["JOBS"] = jobs
        rc = subprocess.run(
            ["scripts/build-strix-rocmfp4-mtp.sh"], cwd=str(install_dir), env=env,
        ).returncode
    quantize_bin = install_dir / "build-strix-rocmfp4" / "bin" / "llama-quantize"
    if rc == 0 and quantize_bin.exists() and _has_full_family(quantize_bin):
        print(f"ROCmFPX installed: {install_dir}", flush=True)
        return str(install_dir)
    print("Error: ROCmFPX install/build failed", flush=True)
    return None


def resolve_source(override: str, out_dir: Path, pipeline_root: str) -> str | None:
    """Resolve the ROCmFPX source model (reap > heretic > merged > bf16 gguf).

    Mirrors ``_magicquant_entry.resolve_source``. Pure path logic; unit-testable.
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


def _ensure_bf16_gguf(rocmfpx_dir: str, source: str, out_dir: Path) -> str:
    """Return a BF16 GGUF path for ``source``.

    Converts via ROCmFPX's bundled ``convert_hf_to_gguf.py`` when ``source``
    is a safetensors directory and no cached ``model-bf16.gguf`` already
    exists (reused across stages/runs, matching the existing
    ``Artifacts.bf16_gguf`` convention).
    """
    import subprocess

    if source.endswith(".gguf"):
        return source

    cached = out_dir / "model-bf16.gguf"
    if cached.exists():
        print(f"Reusing cached BF16 GGUF: {cached}", flush=True)
        return str(cached)

    convert_script = Path(rocmfpx_dir) / "convert_hf_to_gguf.py"
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

    rocmfpx_dir = ensure_rocmfpx(cfg.get("rocmfpx_hint", ""))
    if not rocmfpx_dir:
        print("Error: ROCmFPX not available (build failed) -- aborting", flush=True)
        sys.exit(1)
    print(f"ROCmFPX: {rocmfpx_dir}", flush=True)

    out_dir = Path(cfg["out_abs_str"])
    source = resolve_source(cfg.get("source_override", ""), out_dir, cfg["pipeline_root_str"])
    if not source:
        print(
            "Error: no source model found. Enable Export/MagicQuant or set a "
            "Source Model path in ROCmFPX config.",
            flush=True,
        )
        sys.exit(1)
    print(f"ROCmFPX source: {source}", flush=True)

    bf16_gguf = _ensure_bf16_gguf(rocmfpx_dir, source, out_dir)

    quantize_bin = Path(rocmfpx_dir) / "build-strix-rocmfp4" / "bin" / "llama-quantize"
    rocmfpx_out_dir = out_dir / "rocmfpx"
    rocmfpx_out_dir.mkdir(parents=True, exist_ok=True)

    formats = json.loads(cfg["formats_json"])
    imatrix = cfg.get("imatrix", "")
    model_name = cfg["model_name"]
    tg_safe = cfg.get("tg_safe", False)

    import subprocess

    produced = []
    for spec in formats:
        tier = parse_mq_spec(spec)
        if tier is not None:
            out_path = _quantize_mq_hybrid(
                spec, tier, out_dir, rocmfpx_out_dir, model_name,
                quantize_bin, bf16_gguf, imatrix, tg_safe=tg_safe,
            )
        else:
            out_path = _quantize_preset(
                spec, rocmfpx_out_dir, model_name, quantize_bin, bf16_gguf, imatrix,
            )
        if out_path is not None:
            produced.append(out_path)

    if not produced:
        print("Error: no ROCmFPX GGUF files produced", flush=True)
        sys.exit(1)
    print(f"Generated {len(produced)} ROCmFPX GGUF files", flush=True)
    print("PIPELINE_STAGE_COMPLETE=rocmfpx", flush=True)


def _quantize_preset(spec, out_dir, model_name, quantize_bin, bf16_gguf, imatrix):
    """Run one uniform-preset quantize pass (rocmfp4-agent etc.)."""
    import subprocess

    try:
        fmt, profile = parse_format_spec(spec)
    except ValueError as e:
        print(f"Warning: skipping ROCmFPX format {spec!r}: {e}", flush=True)
        return None
    ggml_type = FORMAT_TABLE[(fmt, profile)]
    out_path = out_dir / f"{model_name}-{ggml_type}.gguf"
    cmd = [str(quantize_bin)]
    if imatrix:
        cmd += ["--imatrix", imatrix]
    cmd += [str(bf16_gguf), str(out_path), ggml_type]
    print(f"Quantizing {spec} ({ggml_type})...", flush=True)
    rc = subprocess.run(cmd).returncode
    if rc != 0 or not out_path.exists():
        print(f"Warning: {spec} ({ggml_type}) quantize failed (exit {rc})", flush=True)
        return None
    print(f"  {out_path.name} ({out_path.stat().st_size / 1e9:.1f} GB)", flush=True)
    return out_path


def _load_mq_tier_config(out_dir: Path, tier: str) -> dict:
    """Read the per-group config for ``tier`` from MagicQuant's search results.

    Raises with an actionable message if the file or tier is missing.
    """
    results_path = out_dir / "magicquant" / "search_results.json"
    if not results_path.exists():
        raise FileNotFoundError(
            f"MagicQuant search_results.json not found at {results_path}. "
            f"Run the MagicQuant stage first (it now persists results from "
            f"both search paths), or drop the mq-* formats."
        )
    data = json.loads(results_path.read_text())
    tiered = data.get("tiered") or {}
    if tier not in tiered:
        raise KeyError(
            f"Tier {tier!r} not in search_results.json (have: "
            f"{sorted(tiered)}). Use one of those or re-run the search."
        )
    config = tiered[tier].get("config")
    if not config:
        raise KeyError(f"Tier {tier!r} has no 'config' in search_results.json")
    return config


def _quantize_mq_hybrid(spec, tier, out_dir, rocmfpx_out_dir, model_name,
                        quantize_bin, bf16_gguf, imatrix, tg_safe=False):
    """Produce a ROCmFPX hybrid matching MagicQuant's per-group config for ``tier``.

    Translates each group's MagicQuant scheme to a ROCmFPX-family type and
    drives llama-quantize with a per-tensor override file so the AMD-native
    formats land exactly where MagicQuant's search placed each precision.

    ``tg_safe`` (opt-in, default off): steer the high-traffic FFN/expert
    groups (U/D/X) to their tg-fast sibling type -- see
    ``build_tensor_type_lines``/``tg_safe_rocmfpx``.
    """
    import subprocess

    try:
        from magicquant.gguf.tensor_groups import TensorGroupClassifier
    except ImportError as e:
        print(
            f"Error: mq-hybrid format {spec!r} needs the magicquant package "
            f"(pip install -e ../MagicQuant): {e}",
            flush=True,
        )
        return None

    try:
        config = _load_mq_tier_config(out_dir, tier)
        group_patterns = TensorGroupClassifier.GROUP_PATTERNS
        lines = build_tensor_type_lines(config, group_patterns, tg_safe=tg_safe)
        base_type = pick_base_type(config)
    except (FileNotFoundError, KeyError, ValueError) as e:
        print(f"Error ({spec}): {e}", flush=True)
        return None

    type_file = rocmfpx_out_dir / f"_ttf-mq-{tier}.txt"
    type_file.write_text("\n".join(lines) + "\n")
    out_path = rocmfpx_out_dir / f"{model_name}-ROCMFPX-MQ-{tier}.gguf"

    schemes = " ".join(f"{g}:{s}" for g, s in sorted(config.items()))
    tg_note = " (tg-safe: U/D/X steered to tg-fast siblings)" if tg_safe else ""
    print(f"Quantizing {spec}: MagicQuant {tier} layout in ROCmFPX types{tg_note}", flush=True)
    print(f"  base={base_type}  groups={schemes}", flush=True)

    cmd = [str(quantize_bin), "--tensor-type-file", str(type_file)]
    if imatrix:
        cmd += ["--imatrix", imatrix]
    cmd += [str(bf16_gguf), str(out_path), base_type]
    rc = subprocess.run(cmd).returncode
    if rc != 0 or not out_path.exists():
        print(f"Warning: {spec} quantize failed (exit {rc})", flush=True)
        return None
    print(f"  {out_path.name} ({out_path.stat().st_size / 1e9:.1f} GB)", flush=True)
    return out_path


if __name__ == "__main__":
    run()
