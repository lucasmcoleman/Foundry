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
import re
import sys
from pathlib import Path

import ppl_smoke

ROCMFPX_REPO = "https://github.com/ciru-ai/ROCmFPX.git"
ROCMFPX_PIN = "68f23f34c12d7e61177a034b0d8d3fea2129565e"  # laguna-capable; bump deliberately

# Env override to bypass the runtime type-support probe (validate_types_supported)
# entirely -- e.g. a CI sandbox with no real ROCmFPX binary at all. Advisory
# only; the probe itself is already advisory-on-unknown (a --help that can't
# be run warns and proceeds rather than blocking).
SKIP_TYPE_PROBE_ENV = "FOUNDRY_SKIP_TYPE_PROBE"

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


def translate_scheme(scheme: str) -> str:
    """Translate a MagicQuant scheme name to its ROCmFPX-family ggml type."""
    if scheme not in SCHEME_TO_ROCMFPX:
        raise ValueError(
            f"No ROCmFPX translation for MagicQuant scheme {scheme!r}. "
            f"Known: {sorted(SCHEME_TO_ROCMFPX)}"
        )
    return SCHEME_TO_ROCMFPX[scheme]


def predict_rendered_tier(config: dict, bf16_gguf: str) -> tuple:
    """Predict which tier band a MagicQuant config will land in once rendered
    into ROCmFPX types.

    Returns ``(predicted_gib, baseline_gib, predicted_tier)``.

    The ROCmFPX family is sparse -- ROCMFP3/4/6/8 at 3.5/4.5/6.5/8.25 bpw,
    with **no 5-bit type**. ``SCHEME_TO_ROCMFPX`` therefore rounds Q5_K *up*
    to Q6_0_ROCMFPX, so a Q5 tier can render larger than the Q6 tier of the
    same model. Measured on Qwen3.6-35B-A3B: mq-q5 came out at 27.51 GiB
    against mq-q6's 26.94 GiB, and measured worse (PPL 7.07 vs 6.77 for
    mq-q4), i.e. strictly dominated on every axis.

    Predicting rather than measuring after the fact matters: a 35B render
    costs several minutes of quantize time, and the resulting file is
    unshippable.

    Bits-per-weight come from the block/size facts of each fork type rather
    than a second hand-maintained table -- see ``magicquant.quant.ggml_facts``.
    """
    from magicquant.gguf.reader import GGUFReader
    from magicquant.gguf.tensor_groups import TensorGroupClassifier
    from magicquant.quant.ggml_facts import FORK_TYPES
    from magicquant.quant.schemes import get_scheme_by_name

    def _bpw(ggml_type: str) -> float:
        fact = FORK_TYPES.get(ggml_type)
        if fact:
            return fact["size"] * 8.0 / fact["block"]
        # Non-fork types keep their registry bpw (BF16/F32 groups pass through).
        for name, mapped in SCHEME_TO_ROCMFPX.items():
            if mapped == ggml_type:
                scheme = get_scheme_by_name(name)
                if scheme:
                    return float(scheme.bits_per_weight)
        return 16.0

    reader = GGUFReader(bf16_gguf)
    reader.open()
    classifier = TensorGroupClassifier()
    per_group: dict = {}
    try:
        for name in reader.get_tensor_names():
            group = classifier.classify_tensor(name)
            info = reader.get_tensor_info(name)
            count = 1
            for dim in info["shape"]:
                count *= dim
            per_group[group] = per_group.get(group, 0) + count
    finally:
        reader.close()

    total = sum(per_group.values())
    rendered_bits = 0.0
    for group, count in per_group.items():
        scheme = config.get(group)
        if scheme is None:                     # untouched (norms etc.)
            rendered_bits += count * 32.0
            continue
        rendered_bits += count * _bpw(translate_scheme(scheme))

    predicted_gib = rendered_bits / 8.0 / 2 ** 30
    baseline_gib = total * 16.0 / 8.0 / 2 ** 30

    from magicquant.quant.tiers import classify_tier
    return predicted_gib, baseline_gib, classify_tier(predicted_gib, baseline_gib)


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


def build_tensor_type_lines(config: dict, group_patterns: dict) -> list[str]:
    """Emit ``<regex>=<TYPE>`` lines for a per-group config.

    ``config`` maps group letters (E/H/Q/K/O/U/D/X/R/S) to MagicQuant scheme
    names; ``group_patterns`` is MagicQuant's ``TensorGroupClassifier.
    GROUP_PATTERNS`` (ordered dict, first-match-wins). Lines are emitted in the
    classifier's own group order so llama-quantize's first-match regex
    semantics reproduce the classifier's assignment (e.g. the X pattern
    ``ffn_(up|gate|down)_exps`` precedes the U pattern ``ffn_up``, so fused
    expert tensors resolve to X, not U).
    """
    lines: list[str] = []
    for group, patterns in group_patterns.items():
        if group not in config:
            continue  # groups the search didn't vary (N norms, V vision) keep defaults
        ggml_type = translate_scheme(config[group])
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


def _run_quantize_help(quantize_bin: Path) -> str | None:
    """Return ``<quantize_bin> --help``'s stdout, or None if it couldn't be run
    (missing binary, not executable, hung past the timeout). Shared probe seam
    for both family discovery (``_has_full_family``) and per-run type-support
    validation (``validate_types_supported``) -- one subprocess contract, not
    two hand-rolled ones.
    """
    import subprocess

    try:
        return subprocess.run(
            [str(quantize_bin), "--help"], capture_output=True, text=True, timeout=30,
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None


def _has_full_family(quantize_bin: Path) -> bool:
    """True if this ``llama-quantize`` carries the full ROCmFPX family (not
    just a ROCmFP4-only build, e.g. a plain rocmfp4-llama checkout).
    """
    out = _run_quantize_help(quantize_bin)
    if out is None:
        return False
    return all(t in out for t in REQUIRED_TYPES)


def parse_quantize_help_types(help_text: str) -> set[str]:
    """Extract the set of ggml type NAMES a ``llama-quantize`` build actually
    supports from its own ``--help`` output, instead of hand-maintaining a
    local list that can silently drift from the binary (mission: probe
    binaries for facts about themselves rather than shadow them locally).

    The "allowed quantization types" table lists one type per line as
    ``  <numeric-id>  or  <NAME>  :  <description>`` (see the trimmed real
    sample embedded in tests/test_rocmfpx_entry.py). Pure string parsing --
    no dependency on the binary or on any local type-name registry.

    NAMESPACE WARNING (read before touching the numeric ids): the table this
    parses is llama-quantize's own listing of ``LLAMA_FTYPE`` values -- the
    allowed values for the *positional* base-type argument
    (``model-f32.gguf model-quant.gguf <type> [nthreads]``). Those numeric ids
    (100-106, 110-115 on this fork; see the sample table) are LLAMA_FTYPE ids.
    They are NOT the same namespace as the ggml type ids used internally when
    the binary resolves a ``--tensor-type-file`` entry (``<regex>=<NAME>``,
    written by ``build_tensor_type_lines``/``_quantize_mq_hybrid``) -- those
    are looked up by a separate ggml-type-name table inside the binary with
    its own (different) numeric ids. This function only ever extracts and
    returns the NAME string (the regex capture group, ``\\S+`` after "or"),
    never the numeric id, and both ``validate_types_supported`` call sites
    (uniform preset base type + mq-hybrid base type/override types) only ever
    check name membership -- so the two distinct id spaces happen to share
    the same name strings for every type both paths care about today
    (verified against a real full-family build; no false positive), and
    nothing here parses or reuses the ids across the two tables. If a future
    change starts threading a numeric id from this parse through to a
    ``--tensor-type`` or ``--tensor-type-file`` call (instead of a name), stop
    -- that would be conflating LLAMA_FTYPE ids with ggml type ids, two
    different namespaces that just happen to overlap in the ids-discarded,
    names-only path used here.
    """
    return set(re.findall(r"^\s*\d+\s+or\s+(\S+)", help_text, re.MULTILINE))


def validate_types_supported(required_types, quantize_bin: Path) -> None:
    """Raise ``RuntimeError`` if ``quantize_bin --help`` doesn't advertise
    every type in ``required_types``.

    Run BEFORE quantizing so an unsupported target type (a stale/partial
    ROCmFPX build missing a newer fork type) surfaces as a clear, actionable
    message here instead of an opaque llama-quantize failure deep in a run.
    Advisory-on-unknown: if ``--help`` itself can't be run, warn and proceed
    rather than blocking the stage on the probe's own failure. Set
    ``FOUNDRY_SKIP_TYPE_PROBE=1`` to bypass the probe entirely.

    NAMESPACE NOTE: ``required_types`` here (from callers) and the
    ``supported`` set below (from ``parse_quantize_help_types``) are both
    ggml type NAME strings, never LLAMA_FTYPE numeric ids -- see the
    namespace warning on ``parse_quantize_help_types`` above.
    ``required_types`` is used both as the positional base ftype (a
    LLAMA_FTYPE by name) and as ``--tensor-type-file`` override values (a
    ggml type by name); this function's name-only membership check is valid
    for both because it never touches either side's numeric id, only the
    name string each side happens to share.
    """
    import os

    if os.environ.get(SKIP_TYPE_PROBE_ENV) == "1":
        return
    help_text = _run_quantize_help(quantize_bin)
    if help_text is None:
        print(
            f"Warning: could not run {quantize_bin} --help to verify type "
            "support -- proceeding without the probe",
            flush=True,
        )
        return
    supported = parse_quantize_help_types(help_text)
    missing = sorted(t for t in required_types if t not in supported)
    if missing:
        raise RuntimeError(
            f"llama-quantize at {quantize_bin} does not support type(s): "
            f"{', '.join(missing)}. This ROCmFPX build is likely stale or "
            f"missing the required fork commit (pinned: {ROCMFPX_PIN}) -- "
            "rebuild/update the checkout. Set "
            f"{SKIP_TYPE_PROBE_ENV}=1 to bypass this check (not recommended)."
        )


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


def _disclose_if_lora_adapter_dir(c: Path) -> None:
    """Loudly flag when a resolve_source() candidate is a LoRA ADAPTER
    directory (adapter_model.safetensors + adapter_config.json), not a
    merged model.

    Mirrors ``_magicquant_entry._disclose_if_lora_adapter_dir`` -- see its
    docstring for the incident (rsLoRA audit, 2026-07-28) this fixes.
    Behavior is UNCHANGED (the adapter dir is still returned); this only
    makes the choice loud instead of silent.
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
        f"NOTE: ROCmFPX source {c} is a LoRA ADAPTER directory "
        f"(adapter_config.json + adapter_model.safetensors), not a merged "
        f"model. It will be used as-is, merged onto base model "
        f"'{base_model}' (read from adapter_config.json) wherever "
        f"downstream conversion/quantization expects a full checkpoint. If "
        f"you meant to quantize the fully-merged model instead, run the "
        f"export stage first (produces merged_model/) or point the source "
        f"at that directory explicitly.",
        flush=True,
    )


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
                _disclose_if_lora_adapter_dir(c)
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

    # The converter lives in the llama.cpp source root, but rocmfpx_dir is
    # usually a build subdir (e.g. <src>/build-strix-rocmfp4). Check the dir,
    # its bin/, and walk up to the source root.
    d = Path(rocmfpx_dir)
    convert_script = None
    for c in [d / "convert_hf_to_gguf.py", d / "bin" / "convert_hf_to_gguf.py",
              *[p / "convert_hf_to_gguf.py" for p in list(d.parents)[:4]]]:
        if c.exists():
            convert_script = c
            break
    if convert_script is None:
        raise RuntimeError(
            f"convert_hf_to_gguf.py not found near {rocmfpx_dir} "
            "(needed to convert safetensors -> BF16 GGUF)"
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
    allow_requantize = cfg.get("allow_requantize", False)

    import subprocess

    produced = []
    for spec in formats:
        tier = parse_mq_spec(spec)
        if tier is not None:
            out_path = _quantize_mq_hybrid(
                spec, tier, out_dir, rocmfpx_out_dir, model_name,
                quantize_bin, bf16_gguf, imatrix, allow_requantize,
            )
        else:
            out_path = _quantize_preset(
                spec, rocmfpx_out_dir, model_name, quantize_bin, bf16_gguf, imatrix,
                allow_requantize,
            )
        if out_path is not None:
            produced.append(out_path)

    if not produced:
        print("Error: no ROCmFPX GGUF files produced", flush=True)
        sys.exit(1)
    print(f"Generated {len(produced)} ROCmFPX GGUF files", flush=True)

    # Post-generation PPL smoke gate (mirrors _magicquant_entry.run's -- see
    # ppl_smoke module docstring for the incident this closes). Advisory on
    # unknown binary/corpus; a completed run that comes back pathological
    # hard-fails the stage before upload.
    perplexity_bin = ppl_smoke.find_perplexity_bin(rocmfpx_dir)
    failed = [p for p in produced if not ppl_smoke.smoke_test_gguf(perplexity_bin, Path(p))]
    if failed:
        print(
            f"Error: PPL smoke test FAILED for {len(failed)}/{len(produced)} "
            f"file(s): {[Path(p).name for p in failed]} -- aborting before "
            f"upload. Override with {ppl_smoke.SKIP_ENV}=1 if this is a known "
            "false positive (not recommended).",
            flush=True,
        )
        sys.exit(1)

    print("PIPELINE_STAGE_COMPLETE=rocmfpx", flush=True)


def _quantize_cmd_base(quantize_bin, allow_requantize: bool = False) -> list[str]:
    """Base llama-quantize argv: the binary, plus --allow-requantize (double-
    quantization of an already-quantized source) when opted in. llama-quantize
    flags must precede its positionals, so this goes first; pure/testable.
    """
    cmd = [str(quantize_bin)]
    if allow_requantize:
        cmd.append("--allow-requantize")
    return cmd


def _quantize_preset(spec, out_dir, model_name, quantize_bin, bf16_gguf, imatrix,
                     allow_requantize=False):
    """Run one uniform-preset quantize pass (rocmfp4-agent etc.)."""
    import subprocess

    try:
        fmt, profile = parse_format_spec(spec)
    except ValueError as e:
        print(f"Warning: skipping ROCmFPX format {spec!r}: {e}", flush=True)
        return None
    ggml_type = FORMAT_TABLE[(fmt, profile)]
    try:
        validate_types_supported({ggml_type}, quantize_bin)
    except RuntimeError as e:
        print(f"Error: skipping ROCmFPX format {spec!r}: {e}", flush=True)
        return None
    out_path = out_dir / f"{model_name}-{ggml_type}.gguf"
    cmd = _quantize_cmd_base(quantize_bin, allow_requantize)
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

    Compatibility: a search_results.json written before MagicQuant's 2026-07
    TIER_SCHEME_VERSION fix has no ``tier_scheme_version`` field and its
    tier labels follow the OLD, wider size-ratio boundaries (e.g. its "Q5"
    entry can actually be Q6_K-sized -- see magicquant.quant.tiers' module
    docstring). The config STILL LOADS -- the per-group scheme mapping
    stored under a tier key is unaffected by which boundaries produced the
    label -- but a non-fatal warning is printed so an ``mq-<tier>`` ROCmFPX
    build (whose OUTPUT FILENAME bakes the tier string in, e.g.
    "...-MQ-Q5.gguf") doesn't silently ship a file whose name no longer
    matches what that tier means under the current scheme.
    """
    results_path = out_dir / "magicquant" / "search_results.json"
    if not results_path.exists():
        raise FileNotFoundError(
            f"MagicQuant search_results.json not found at {results_path}. "
            f"Run the MagicQuant stage first (it now persists results from "
            f"both search paths), or drop the mq-* formats."
        )
    data = json.loads(results_path.read_text())

    # Lazy import (magicquant is a heavier optional dependency; every other
    # magicquant touchpoint in this module imports it function-local too --
    # see _quantize_mq_hybrid).
    from magicquant.quant.tiers import CURRENT_TIER_SCHEME_VERSION, tier_scheme_version

    version = tier_scheme_version(data)
    if version < CURRENT_TIER_SCHEME_VERSION:
        print(
            f"Warning: {results_path} was written under tier_scheme_version="
            f"{version} (current: {CURRENT_TIER_SCHEME_VERSION}) -- its tier "
            f"labels follow OLDER, wider size-ratio boundaries. The "
            f"requested tier {tier!r} config still loads correctly, but the "
            f"resulting mq-{tier} filename may not match what {tier!r} "
            f"means under the current scheme (e.g. an old 'Q5' can be "
            f"Q6_K-sized). Disclose this on the model card, or re-run the "
            f"MagicQuant search for labels matching current semantics.",
            flush=True,
        )

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
                        quantize_bin, bf16_gguf, imatrix, allow_requantize=False):
    """Produce a ROCmFPX hybrid matching MagicQuant's per-group config for ``tier``.

    Translates each group's MagicQuant scheme to a ROCmFPX-family type and
    drives llama-quantize with a per-tensor override file so the AMD-native
    formats land exactly where MagicQuant's search placed each precision.
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

        # A tier name is a SIZE BAND. If rendering this config into the sparse
        # ROCmFPX family lands it in a different band, the artifact would be
        # mislabelled no matter how it is named -- refuse before spending the
        # quantize. Checked by prediction, not by a hardcoded "q5 is bad" rule,
        # so it also catches whatever the next sparse family or new scheme does.
        try:
            pred_gib, base_gib, pred_tier = predict_rendered_tier(config, bf16_gguf)
            if pred_tier != tier:
                print(
                    f"Refusing {spec}: rendering MagicQuant's {tier} config into "
                    f"ROCmFPX types predicts {pred_gib:.2f} GiB against a "
                    f"{base_gib:.2f} GiB BF16 baseline (ratio "
                    f"{pred_gib / base_gib:.4f}), which is the {pred_tier} band, "
                    f"not {tier}.",
                    flush=True,
                )
                print(
                    f"  The ROCmFPX family has no type between "
                    f"{4.5:.1f} and {6.5:.1f} bpw, so schemes round to the "
                    f"nearest available and a tier can render outside its own "
                    f"band. Skipping rather than shipping a mislabelled file.",
                    flush=True,
                )
                return None
        except Exception as exc:  # noqa: BLE001 -- advisory, never blocks a valid build
            print(f"  (tier-band prediction unavailable: {exc})", flush=True)

        group_patterns = TensorGroupClassifier.GROUP_PATTERNS
        lines = build_tensor_type_lines(config, group_patterns)
        base_type = pick_base_type(config)
        required_types = {base_type} | {translate_scheme(s) for s in config.values()}
        validate_types_supported(required_types, quantize_bin)
    except (FileNotFoundError, KeyError, ValueError, RuntimeError) as e:
        print(f"Error ({spec}): {e}", flush=True)
        return None

    type_file = rocmfpx_out_dir / f"_ttf-mq-{tier}.txt"
    type_file.write_text("\n".join(lines) + "\n")
    out_path = rocmfpx_out_dir / f"{model_name}-ROCMFPX-MQ-{tier}.gguf"

    schemes = " ".join(f"{g}:{s}" for g, s in sorted(config.items()))
    print(f"Quantizing {spec}: MagicQuant {tier} layout in ROCmFPX types", flush=True)
    print(f"  base={base_type}  groups={schemes}", flush=True)

    cmd = _quantize_cmd_base(quantize_bin, allow_requantize)
    cmd += ["--tensor-type-file", str(type_file)]
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
