"""
HuggingFace Hub upload module.

Handles repo creation, model card generation, and GGUF/LoRA/merged file upload
with progress reporting and dry-run mode.

Can be used standalone or called from pipeline.py / the web UI.

Token is sourced from HF_TOKEN env var — never hardcoded.
"""

import os
import re
import sys
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Optional

import requests
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

# Shared logging helper (single source of truth — see core/log.py).
try:
    from log import LogFn, default_log as _default_log
except ImportError:  # pragma: no cover - when imported as the `core` package
    from core.log import LogFn, default_log as _default_log

# Recommended serve command for MTP-capable GGUFs (see core/serving.py).
try:
    from serving import build_serve_command, detect_mtp, format_serve_command
except ImportError:  # pragma: no cover - when imported as the `core` package
    from core.serving import build_serve_command, detect_mtp, format_serve_command

# ROCmFPX fork pin for model-card usage snippets -- single-sourced from
# core/_rocmfpx_entry.py (its own known-good commit) rather than a second
# hand-typed SHA here, which would silently drift the moment the pin is
# bumped in one place and not the other.
try:
    from _rocmfpx_entry import ROCMFPX_PIN, ROCMFPX_REPO
except ImportError:  # pragma: no cover - when imported as the `core` package
    from core._rocmfpx_entry import ROCMFPX_PIN, ROCMFPX_REPO


# ── Configuration ────────────────────────────────────────────────────────────

@dataclass
class HFUploadConfig:
    """All parameters needed for a HuggingFace upload."""
    repo_id: str = ""
    private: bool = True
    license: str = "apache-2.0"

    # What to upload
    upload_gguf: bool = True
    upload_lora: bool = False
    upload_merged: bool = False
    upload_dataset: bool = True

    # Model metadata (for the model card)
    base_model: str = ""
    dataset_name: str = ""
    model_description: str = ""

    # Pipeline stages that were actually run (for conditional card sections)
    did_training: bool = True
    did_heretic: bool = False
    did_reap: bool = False
    did_magicquant: bool = True

    # Tiers the publish stage built but deliberately did NOT ship, as
    # [{"tier", "reason", "gib", "loss", "beaten_by"}]. A ladder with a gap in
    # it reads as something having gone wrong, and users notice: removing a
    # mislabeled ThinkingCap Q5 in Aug 2026 produced a "was it deleted, or is
    # something wrong with it?" comment within a day. Saying so on the card is
    # cheaper than answering it repeatedly, and honest either way.
    dropped_tiers: list = field(default_factory=list)

    # GGUFs already on the hub that THIS run did not produce, as
    # [{"name", "gib"}]. The uploader adds files and never reconciles, so a
    # repo accumulates artifacts across runs; without saying so, a download
    # page silently mixes outputs from different searches under one model
    # card that describes only the latest.
    carried_over: list = field(default_factory=list)

    # Tiers the pipeline REFUSED to build, as [{"tier", "reason", "family"}].
    # Distinct from dropped_tiers (built, then rejected on measurement): a
    # refusal means no file was produced at all, so a repo still holding an
    # older one for that tier would otherwise imply a fresh build is pending.
    refused_tiers: list = field(default_factory=list)

    # Training details (for the model card)
    lora_r: int = 32
    lora_alpha: int = 64
    lora_dropout: float = 0.05
    num_epochs: int = 3
    learning_rate: float = 2e-4
    max_seq_length: int = 8192
    batch_size: int = 2
    gradient_accumulation: int = 4
    optimizer: str = "adamw_8bit"
    lr_scheduler: str = "cosine"


# ── File discovery ───────────────────────────────────────────────────────────

def discover_upload_files(
    output_dir: str,
    upload_gguf: bool = True,
    upload_lora: bool = False,
    upload_merged: bool = False,
    gguf_family: str = "auto",
) -> list[tuple[Path, str]]:
    """Find files to upload from the output directory.

    ``gguf_family`` selects which quant family's GGUFs to include:
    "magicquant" (magicquant/*.gguf), "rocmfpx" (rocmfpx/*.gguf), or "auto"
    (magicquant, else rocmfpx, else the bf16 conversion artifact).

    Returns a list of (local_path, repo_path) tuples.
    """
    out = Path(output_dir)
    files = []

    if upload_lora:
        lora_dir = out / "lora_adapters"
        if lora_dir.exists():
            for f in sorted(lora_dir.iterdir()):
                if f.is_file():
                    files.append((f, f"lora/{f.name}"))

    if upload_merged:
        merged_dir = out / "merged_model"
        if merged_dir.exists():
            for f in sorted(merged_dir.iterdir()):
                if f.is_file():
                    files.append((f, f"merged/{f.name}"))

    if upload_gguf:
        mq_files = sorted((out / "magicquant").glob("*.gguf")) if (out / "magicquant").exists() else []
        fpx_files = sorted((out / "rocmfpx").glob("*.gguf")) if (out / "rocmfpx").exists() else []
        if gguf_family == "magicquant":
            gguf_files = mq_files
        elif gguf_family == "rocmfpx":
            gguf_files = fpx_files
        else:  # auto
            gguf_files = mq_files or fpx_files
            if not gguf_files:
                bf16 = out / "model-bf16.gguf"
                if bf16.exists():
                    gguf_files = [bf16]
        for f in gguf_files:
            files.append((f, f.name))
        # Vision projector (mmproj) for multimodal models — auto-generated by the
        # MagicQuant stage next to the text quants; ship it so image input works.
        mmproj_dir = out / "mmproj"
        if mmproj_dir.exists():
            for f in sorted(mmproj_dir.glob("*.gguf")):
                files.append((f, f.name))

    return files


# Suffixes recognized (and stripped) when deriving sibling/dataset repo names.
_REPO_SUFFIXES = ("-MagicQuant-GGUF", "-ROCmFPX-GGUF", "-GGUF", "-gguf")


def _repo_base(repo_id: str) -> str:
    """Strip a known quant-repo suffix from a repo id: user/X-MagicQuant-GGUF -> user/X."""
    for suffix in _REPO_SUFFIXES:
        if repo_id.endswith(suffix):
            return repo_id[: -len(suffix)]
    return repo_id


def plan_gguf_repos(output_dir: str, repo_id: str) -> list[tuple[str, str]]:
    """Decide which repos to upload GGUFs to, one repo per quant family.

    When both MagicQuant and ROCmFPX quants exist, each family gets its own
    sibling repo (``<base>-MagicQuant-GGUF`` / ``<base>-ROCmFPX-GGUF``) —
    ROCmFPX files only load on the fork, so mixing them in one repo buries
    the stock-llama.cpp files behind fork-only ones. With a single family
    the configured repo_id is used as-is.

    Returns a list of (repo_id, gguf_family) tuples; family is a
    ``discover_upload_files`` gguf_family value.
    """
    out = Path(output_dir)
    has_mq = bool(list((out / "magicquant").glob("*.gguf"))) if (out / "magicquant").exists() else False
    has_fpx = bool(list((out / "rocmfpx").glob("*.gguf"))) if (out / "rocmfpx").exists() else False

    if has_mq and has_fpx:
        base = _repo_base(repo_id)
        return [(f"{base}-MagicQuant-GGUF", "magicquant"), (f"{base}-ROCmFPX-GGUF", "rocmfpx")]
    if has_fpx:
        return [(repo_id, "rocmfpx")]
    return [(repo_id, "auto")]


# ── Model card generation ────────────────────────────────────────────────────

def _pick_example_gguf(files_to_upload: list[tuple[Path, str]]) -> Optional[str]:
    """Pick a real, already-planned GGUF filename for usage snippets.

    Never fabricates a name (e.g. a synthesized ``"<repo>-Q5.gguf"`` that may
    not exist in the repo) -- prefers the Q5-ish "middle" tier when the
    filenames make one identifiable, else the first non-mmproj GGUF in
    upload order. Returns None if no non-mmproj GGUF was planned.
    """
    gguf_names = [repo_path for _, repo_path in files_to_upload
                  if _is_tier_gguf(repo_path)]
    if not gguf_names:
        return None
    for name in gguf_names:
        if "q5" in name.lower():
            return name
    return gguf_names[0]


def _find_mmproj(files_to_upload: list[tuple[Path, str]]) -> Optional[str]:
    """Return the mmproj (vision projector) repo filename, if one was planned."""
    return next(
        (repo_path for _, repo_path in files_to_upload
         if repo_path.lower().endswith(".gguf") and "mmproj" in repo_path.lower()),
        None,
    )


def _find_legacy_tier_scheme_note(files_to_upload: list[tuple[Path, str]]) -> str:
    """Look for a sibling MagicQuant ``search_results.json`` next to this
    run's planned files and, if found and written under an older
    ``tier_scheme_version`` than current, return a disclosure suffix to
    append to Q4/Q5/Q6 quant hints in the GGUF table.

    Tier labels ("Q4"/"Q5"/"Q6") are, before the 2026-07 fix, SIZE BANDS
    that didn't line up with the scheme they're named after (a "Q5" file
    could actually be Q6_K-sized -- see magicquant.quant.tiers' module
    docstring). This card generator infers quant hints purely from
    filenames, which are correct for a FRESH run under the current
    boundaries -- but a card generated for pre-existing output from an old
    run must not silently claim the current meaning. Returns "" (no
    disclosure) when no search_results.json is found or it's already
    current -- this is best-effort, not a hard requirement (a card without
    this note is still accurate for any freshly-generated run).
    """
    try:
        from magicquant.quant.tiers import CURRENT_TIER_SCHEME_VERSION, tier_scheme_version
    except ImportError:
        return ""

    candidate_dirs = set()
    for local_path, _ in files_to_upload:
        parent = local_path.parent
        if parent.name in ("magicquant", "rocmfpx"):
            candidate_dirs.add(parent.parent)

    for output_dir in candidate_dirs:
        results_path = output_dir / "magicquant" / "search_results.json"
        if not results_path.is_file():
            continue
        try:
            import json as _json
            data = _json.loads(results_path.read_text())
        except (OSError, ValueError):
            continue
        version = tier_scheme_version(data)
        if version < CURRENT_TIER_SCHEME_VERSION:
            return (
                f" (legacy tier boundaries, tier_scheme_version={version} -- "
                f"verify actual size before assuming today's Q4/Q5/Q6 meaning)"
            )
    return ""


def _is_tier_gguf(name: str) -> bool:
    """A published quant file whose NAME claims a tier -- mmproj excluded.

    Single definition, because two different ones disagreed: the "don't claim
    a tier is missing" pre-check filtered mmproj and required .gguf, while the
    card audit re-derived the same question over every repo file including
    README.md, so a stray tier-named JSON could make the audit assert a tier
    "IS in the repo" when no GGUF for it existed.
    """
    low = name.lower()
    return low.endswith(".gguf") and "mmproj" not in low


def _tier_of(name: str) -> Optional[str]:
    """The Qn tier a published filename claims, or None."""
    m = re.search(r"(Q\d)", name)
    return m.group(1) if m else None


def _repo_tiers_present(names) -> set[str]:
    """Tiers actually backed by a file, for "never claim a tier is absent"."""
    return {t for n in names if _is_tier_gguf(n) for t in [_tier_of(n)] if t}


def _resolve_size_bytes(
    local_path: Path, repo_path: str, known_sizes: Optional[dict[str, int]]
) -> Optional[int]:
    """Resolve a file's size for the model card, preferring an authoritative
    known_sizes entry over stat()ing the local path.

    Regenerating a card after the cleanup stage deletes local GGUFs meant
    stat()ing paths that no longer existed, which silently dropped every
    file-table row but mmproj -- the repo still held the real GGUFs, but
    files_to_upload's local Path was gone so the generator couldn't see them.
    known_sizes (keyed by repo_path, the same string used as the table's file
    name) lets a caller source sizes from the repo's own metadata instead
    (e.g. HfApi().repo_info(..., files_metadata=True).siblings), so a row
    survives even when nothing local backs it. Falls back to stat() when no
    known_sizes entry exists, so the common in-run case is unchanged. Returns
    None only when there is truly no size available anywhere -- callers skip
    the row rather than fabricate a number.
    """
    if known_sizes and repo_path in known_sizes:
        return known_sizes[repo_path]
    try:
        return local_path.stat().st_size
    except OSError:
        return None


def card_rows_from_repo(
    repo_files: "list", local_dir: Optional[Path] = None
) -> tuple[list[tuple[Path, str]], dict[str, int]]:
    """Build ``(files_to_upload, known_sizes)`` for a card sourced from the REPO.

    ``generate_model_card``'s table iterates ``files_to_upload``, and the only
    producer of that list -- ``discover_upload_files`` -- builds it by globbing
    the local output dir. So after the cleanup stage deletes local GGUFs there
    is no ROW for a size to attach to, and the table collapses to whatever
    survived (in the incident: just mmproj). Supplying sizes alone cannot fix
    that; the ROWS have to come from the repo too.

    Pass HfApi().repo_info(repo_id, files_metadata=True).siblings (anything
    with ``.rfilename`` and ``.size``, or plain ``(name, size)`` pairs). The
    local paths returned point into ``local_dir`` when given, so a file that
    IS still on disk keeps working for the parts of card generation that read
    it (MTP detection, the legacy-tier-scheme probe); ones that are gone still
    get a row, from the repo's own recorded size.
    """
    rows: list[tuple[Path, str]] = []
    sizes: dict[str, int] = {}
    base = Path(local_dir) if local_dir else Path(".")
    for f in repo_files:
        name = getattr(f, "rfilename", None)
        size = getattr(f, "size", None)
        if name is None:
            name, size = f
        rows.append((base / name, name))
        if size is not None:
            sizes[name] = int(size)
    return rows, sizes


def generate_model_card(
    cfg: HFUploadConfig,
    files_to_upload: list[tuple[Path, str]],
    dataset_repo_id: str = "",
    rocmfpx: bool = False,
    sibling_repo_id: str = "",
    known_sizes: Optional[dict[str, int]] = None,
    log: LogFn = _default_log,
) -> str:
    """Generate a complete model card with YAML front matter.

    Includes: description, base model credit, quantization method,
    training details, caveats, limitations, and usage instructions.

    ``rocmfpx=True`` flavors the card for AMD-native fork-only GGUFs (loud
    "does not load on stock llama.cpp" banner, ROCm tags, fork usage).
    ``sibling_repo_id`` cross-links the other quant family's repo when the
    upload was split into MagicQuant + ROCmFPX siblings.
    ``known_sizes`` (optional, keyed by repo_path) supplies authoritative
    sizes for entries whose local file may not exist -- see
    ``_resolve_size_bytes`` and ``card_rows_from_repo``. Entries with neither
    a live local file nor a known_sizes hit are skipped rather than crashing
    or fabricating a size, and every such skip is logged: a card whose file
    table silently shrank to just mmproj is what made that bug take a day to
    notice.
    """
    repo_name = cfg.repo_id.split("/")[-1] if "/" in cfg.repo_id else cfg.repo_id
    base_model = cfg.base_model or "unknown"
    base_model_short = base_model.split("/")[-1] if "/" in base_model else base_model
    dataset_name = cfg.dataset_name or "custom dataset"

    # Build the GGUF file table
    gguf_rows = ""
    has_gguf = False
    mtp_gguf_path: Optional[Path] = None
    # Computed once: "" for a fresh/current run, else a disclosure suffix
    # appended to every MagicQuant-tier-derived quant hint below (see
    # _find_legacy_tier_scheme_note's docstring).
    legacy_tier_note = _find_legacy_tier_scheme_note(files_to_upload)
    for local_path, repo_path in files_to_upload:
        if repo_path.endswith(".gguf"):
            has_gguf = True
            if mtp_gguf_path is None:
                try:
                    if detect_mtp(str(local_path)):
                        mtp_gguf_path = local_path
                except Exception:
                    pass  # unreadable/partial GGUF — card just omits the MTP section
            size_bytes = _resolve_size_bytes(local_path, repo_path, known_sizes)
            if size_bytes is None:
                # No local file and no known_sizes entry. Dropping the row
                # SILENTLY is what reduced a regenerated card's file table to
                # just mmproj after cleanup deleted the GGUFs, so say it.
                log(f"  Card: no size for '{repo_path}' (no local file at "
                    f"{local_path}, no known_sizes entry) -- omitting its row. "
                    f"Pass known_sizes (see card_rows_from_repo) to keep it.",
                    "warn")
                continue
            size_gb = size_bytes / 1e9
            name = repo_path
            # Infer quant tier from filename
            quant_hint = ""
            name_lower = name.lower()
            mq_hybrid_tier = re.search(r"mq-q(\d+)", name_lower)
            if mq_hybrid_tier:
                # MagicQuant-hybrid ROCmFPX file (_rocmfpx_entry._quantize_mq_hybrid's
                # "<model>-ROCMFPX-MQ-<tier>.gguf" convention): MagicQuant's
                # per-group layout re-expressed in ROCmFPX types. There's no
                # direct Qn->ROCmFPn mapping (e.g. MagicQuant Q5 rounds UP to
                # FP6), so this must be matched explicitly rather than falling
                # through to the ROCmFPn/Qn substring checks below -- those
                # would leave the cell empty for exactly this case.
                quant_hint = (
                    f"MagicQuant Q{mq_hybrid_tier.group(1)} layout in "
                    f"ROCmFPX types (hybrid, fork-only){legacy_tier_note}"
                )
            elif "rocmfp" in name_lower:
                for bits in ("3", "4", "6", "8"):
                    if f"q{bits}" in name_lower or f"rocmfp{bits}" in name_lower:
                        quant_hint = f"ROCmFP{bits} (fork-only)"
                        break
                if quant_hint:
                    if "_agent" in name_lower:
                        quant_hint += ", agent preset"
                    elif "_coherent" in name_lower:
                        quant_hint += ", coherent preset"
                else:
                    quant_hint = "hybrid (fork-only)"
            elif "q4" in name_lower:
                quant_hint = f"Q4 hybrid{legacy_tier_note}"
            elif "q5" in name_lower:
                quant_hint = f"Q5 hybrid{legacy_tier_note}"
            elif "q6" in name_lower:
                quant_hint = f"Q6 hybrid{legacy_tier_note}"
            elif "bf16" in name_lower:
                quant_hint = "BF16 (unquantized)"
            elif "f16" in name_lower:
                quant_hint = "F16 (unquantized)"
            if not quant_hint:
                quant_hint = "—"
            gguf_rows += f"| [{name}](./{name}) | {size_gb:.1f} GB | {quant_hint} |\n"

    # Non-GGUF files table
    other_rows = ""
    for local_path, repo_path in files_to_upload:
        if not repo_path.endswith(".gguf"):
            size_bytes = _resolve_size_bytes(local_path, repo_path, known_sizes)
            if size_bytes is None:
                log(f"  Card: no size for '{repo_path}' -- omitting its row.",
                    "warn")
                continue
            size_mb = size_bytes / 1e6
            other_rows += f"| {repo_path} | {size_mb:.0f} MB |\n"

    # Build a dynamic description based on which pipeline stages ran
    description = cfg.model_description
    if not description:
        parts = []
        if cfg.did_training:
            parts.append(f"fine-tuned on {dataset_name} with QLoRA")
        if cfg.did_heretic:
            parts.append("abliterated with [Heretic](https://github.com/p-e-w/heretic) for uncensored responses")
        if cfg.did_reap:
            parts.append("pruned with [REAP](https://github.com/CerebrasResearch/reap) (Router-weighted Expert Activation Pruning)")
        if cfg.did_magicquant:
            parts.append("quantized using MagicQuant hybrid evolutionary per-tensor search")
        if rocmfpx:
            parts.append("quantized to AMD-native [ROCmFPX](https://github.com/ciru-ai/ROCmFPX) formats "
                         "(fork-only) tuned for Strix Halo (gfx1151)")
        elif not cfg.did_magicquant and has_gguf:
            parts.append("exported to GGUF format")

        if parts:
            actions = ", ".join(parts[:-1]) + (" and " if len(parts) > 1 else "") + parts[-1] if len(parts) > 1 else parts[0]
            description = (
                f"Derivative of [{base_model_short}](https://huggingface.co/{base_model}), "
                f"{actions}."
            )
        else:
            description = f"Derivative of [{base_model_short}](https://huggingface.co/{base_model})."

    # Build effective batch size
    effective_batch = cfg.batch_size * cfg.gradient_accumulation

    # Dynamic tags based on pipeline stages
    tags = []
    if has_gguf:
        tags.append("gguf")
    if rocmfpx:
        tags.extend(["rocm", "amd", "strix-halo", "gfx1151", "rocmfpx"])
    if cfg.did_magicquant:
        tags.extend(["quantized", "magicquant"])
    if cfg.did_training:
        tags.extend(["fine-tuned", "qlora"])
    if cfg.did_heretic:
        tags.extend(["abliterated", "uncensored", "heretic"])
    if cfg.did_reap:
        tags.extend(["pruned", "reap", "moe"])
    if not tags:
        tags = ["gguf"]
    tags_yaml = "\n".join(f"  - {t}" for t in tags)

    # Determine quantization types from GGUF filenames for metadata
    quant_types = set()
    for local_path, repo_path in files_to_upload:
        name = repo_path.lower()
        if name.endswith(".gguf"):
            for qt in ["q3", "q4", "q5", "q6", "q8", "mxfp4", "iq4", "bf16", "f16"]:
                if qt in name:
                    quant_types.add(qt.upper())

    is_finetune = cfg.did_training and cfg.num_epochs > 0 and cfg.lora_r > 0
    base_model_relation = "finetune" if is_finetune else "quantized"

    # Dynamic library_name and quantized_by
    library_name = "llama.cpp" if has_gguf else "transformers"
    quantized_by = "ROCmFPX" if rocmfpx else ("MagicQuant" if cfg.did_magicquant else "")

    yaml_block = f"""---
license: {cfg.license}
library_name: {library_name}
base_model:
  - {base_model}
base_model_relation: {base_model_relation}
{"datasets:" + chr(10) + "  - " + dataset_repo_id if dataset_repo_id else ""}
pipeline_tag: text-generation
{"quantized_by: " + quantized_by if quantized_by else ""}
language:
  - en
tags:
{tags_yaml}
---"""

    # Files section
    files_section = ""
    if has_gguf:
        files_section += f"""
## GGUF Files

| File | Size | Quant |
|------|------|-------|
{gguf_rows}"""

    if other_rows:
        files_section += f"""
## Other Files

| File | Size |
|------|------|
{other_rows}"""

    # ── Build the card body with conditional sections ──

    body_sections = []

    # Header
    header = f"# {repo_name}\n\n{description}"
    if rocmfpx:
        sibling_note = ""
        if sibling_repo_id:
            sibling_note = (
                f"\n> For files that work with stock llama.cpp / LM Studio / Ollama, use the sibling repo:\n"
                f"> [{sibling_repo_id}](https://huggingface.co/{sibling_repo_id})."
            )
        header = (
            f"# {repo_name}\n\n"
            f"> ## ⚠️ These files do NOT load on standard llama.cpp\n"
            f"> They use AMD-native `*_ROCMFPX` tensor types from the experimental\n"
            f"> [ciru-ai/ROCmFPX](https://github.com/ciru-ai/ROCmFPX) llama.cpp fork (build from source)."
            f"{sibling_note}\n\n"
            f"{description}"
        )
    elif sibling_repo_id:
        header += (
            f"\n\nSibling repo with AMD-native (ROCmFPX fork-only) builds: "
            f"[{sibling_repo_id}](https://huggingface.co/{sibling_repo_id})."
        )
    body_sections.append(header)

    # Base model credit (always)
    body_sections.append(f"""## Base Model

This is a derivative of [{base_model_short}](https://huggingface.co/{base_model}).
All credit for the base model architecture and weights goes to the original authors.
The base model's license applies to this derivative.""")

    # Heretic section (only if abliteration was applied)
    if cfg.did_heretic:
        body_sections.append("""## Abliteration (Heretic)

Safety alignment has been removed using **[Heretic](https://github.com/p-e-w/heretic)** directional ablation:

- A "refusal direction" is identified in each transformer layer's residual stream
- The model's projection matrices are orthogonalized with respect to that direction
- An Optuna TPE optimizer automatically tunes all ablation parameters
- The result is a model that responds to all prompts without refusal behavior

**This model will comply with any request.** Use responsibly.""")

    # REAP section (only if expert pruning was applied)
    if cfg.did_reap:
        body_sections.append("""## Expert Pruning (REAP)

This is a Mixture-of-Experts model pruned using **[REAP](https://github.com/CerebrasResearch/reap)**
(Router-weighted Expert Activation Pruning) from Cerebras Research:

- A calibration pass records router decisions and expert activations on representative data
- Each expert is scored with a saliency metric weighted by router usage
- The lowest-ranked experts in each MoE layer are dropped
- The router is trimmed accordingly so the remaining experts cover the full routing distribution

The result is a smaller MoE model with fewer experts per layer, trading a small amount of quality
for reduced parameter count and inference cost.""")

    # MagicQuant section (only if quantized with MagicQuant)
    if cfg.did_magicquant:
        body_sections.append("""## Quantization Method

Quantized using **[MagicQuant](https://github.com/lucasmcoleman/MagicQuant)** hybrid evolutionary per-tensor quantization,
based on the methodology by **[magiccodingman](https://github.com/magiccodingman/MagicQuant-Wiki)**:

- Tensors are classified into sensitivity groups (Embeddings, Head, Query, Key, Output, FFN Up/Down, MoE Experts, Router)
- An evolutionary search finds the optimal quantization type per group, balancing size vs. perplexity
- **Q4/Q5/Q6 tier targets** are searched, and each one ships only if it earns its place (see below)
- Small-row tensors and sensitivity-critical layers (embeddings, output head, router) are kept at F32/F16/BF16
- This is NOT a uniform quantization -- each tensor group gets its own optimal type

A tier name here is a **size band**, not a promise that every tensor uses that
exact type. A "Q5" is whatever mix of schemes landed in the Q5 size band with
the lowest measured perplexity loss -- which is the point of the search.""")

    # Explain any gap in the ladder, rather than leaving a hole for people to
    # wonder about.
    if getattr(cfg, "dropped_tiers", None):
        rows = []
        for d in cfg.dropped_tiers:
            tier = d.get("tier", "?")
            why = d.get("reason", "did not meet the publishing criteria")
            detail = ""
            if d.get("gib") and d.get("loss") is not None:
                detail = f" It measured {d['gib']:.2f} GiB at {d['loss']:+.4f} loss"
                if d.get("beaten_by"):
                    # .get, not [...]: the ROCmFPX family compares on absolute
                    # PPL and puts "ppl" here where MagicQuant puts "loss".
                    # Indexing "loss" unconditionally would KeyError midway
                    # through card generation the first time a ROCmFPX drop
                    # entry carried a measured loss.
                    b = d["beaten_by"]
                    metric = b.get("loss")
                    shown = (f"{metric:+.4f}" if metric is not None
                             else f"PPL {b['ppl']:.4f}" if b.get("ppl") is not None
                             else "an unrecorded measurement")
                    detail += (f", against {b['tier']} at {b['gib']:.2f} GiB and "
                               f"{shown}")
                detail += "."
            rows.append(f"- **{tier} was not published.** {why}{detail}")
        dropped_names = ", ".join(str(d.get("tier", "?")) for d in cfg.dropped_tiers)
        # The closing paragraph must match the RULE that actually fired. A
        # single dominance-shaped blurb ("a smaller tier already beats it")
        # was flatly untrue on a ROCmFPX card where the tier was dropped for
        # lack of a speed advantage, contradicting the bullet above it.
        # Falling back to the dominance blurb for anything unrecognised is how
        # a tier dropped for an unparseable perplexity got explained with "a
        # smaller tier already beats it" -- false, and contradicting its own
        # bullet. An unknown or mixed rule set gets a rationale that asserts
        # only what is true in every case: which check failed is in the
        # per-tier reason above.
        rationales = {
            "speed": (
                "This is deliberate. ROCmFPX types exist to trade a little "
                "quality for throughput on AMD hardware, so a ROCmFPX tier is "
                "only worth publishing when it is measurably **faster** than "
                "the equivalent MagicQuant tier. When it isn't, it would be "
                "strictly worse: same size, lower quality, no speed."
            ),
            "band": (
                "This is deliberate. A tier name here is a **size band**, and "
                "a file that lands outside the band its name claims is "
                "mislabelled -- so it is withheld rather than shipped under "
                "the wrong name."
            ),
            "dominance": (
                "This is deliberate. Each tier is measured against the others "
                "on **size and quality together**, and a tier is dropped when "
                "another *smaller* tier in this same repo already matches or "
                "beats it. Shipping it anyway would mean offering a bigger "
                "download for no measurable gain."
            ),
        }
        rules = {d.get("rule") or "unspecified" for d in cfg.dropped_tiers}
        rationale = rationales.get(
            next(iter(rules)) if len(rules) == 1 else "",
            "This is deliberate. Every tier clears three checks before "
            "release: it lands in the size band its name claims, it is not "
            "beaten on quality by a smaller tier in this same repo, and -- "
            "for ROCmFPX -- it is measurably faster than its MagicQuant "
            "equivalent. The note on each file above says which of those it "
            "did not clear.",
        )
        body_sections.append(
            "## Why a tier is missing\n\n"
            + "\n".join(rows)
            + f"\n\n{rationale} The {dropped_names} file is not missing by "
            "accident, and nothing here is broken.\n\n"
            "If you specifically want that size point, open an issue in the "
            "Community tab and I'll build it -- the search results are kept, "
            "so it's a rebuild rather than a re-search."
        )

    # Tiers the pipeline REFUSED to build at all (distinct from built-then-
    # rejected above). Without this, a repo carrying an older file for that
    # tier reads as though a fresh one is simply pending.
    if getattr(cfg, "refused_tiers", None):
        rows = [f"- **{r.get('tier', '?')}** -- {r.get('reason', '')}"
                for r in cfg.refused_tiers]
        body_sections.append(
            "## Tiers this build does not produce\n\n"
            + "\n".join(rows)
            + "\n\nThese were not built at all. This is a property of how the "
            "schemes round into the ROCmFPX type ladder for this particular "
            "model, not a temporary gap, so a file for them will not appear "
            "in a later build either."
            + ("  Any file for them currently in this repo therefore comes "
               "from an earlier run -- see below."
               if getattr(cfg, "carried_over", None) else "")
        )

    # Files that predate this run. The uploader adds and never reconciles, so
    # a repo accumulates across searches; a card that describes only the
    # latest run silently misrepresents them.
    if getattr(cfg, "carried_over", None):
        rows = []
        for c in cfg.carried_over:
            size = f" ({c['gib']:.2f} GiB)" if c.get("gib") else ""
            note = ""
            if c.get("mislabeled"):
                note = (f" -- **its size puts it in the {c['actual_band']} "
                        f"band, not the {c['claimed_band']} its name claims.** "
                        f"Treat the name as unreliable for this file.")
            elif c.get("actual_band"):
                note = f" -- size verified as {c['actual_band']} band"
            rows.append(f"- `{c.get('name', '?')}`{size}{note}")
        body_sections.append(
            "## Files from an earlier build\n\n"
            "These files were produced by a **previous quantization run**, not "
            "the one this card describes:\n\n"
            + "\n".join(rows)
            + "\n\nThey are kept because they are correctly sized for their "
            "tier and remain usable. But they were selected by an earlier "
            "version of the search, so their quality was not measured on the "
            "same footing as the other files here, and the per-group scheme "
            "breakdown above does not describe them.\n\n"
            "If you are comparing tiers against each other, prefer the files "
            "from the current run -- the comparison is only apples-to-apples "
            "within a single search."
        )

    # ROCmFPX section (only for AMD-native fork builds)
    if rocmfpx:
        body_sections.append(f"""## ROCmFPX (AMD-native, fork-only)

These GGUFs use AMD-native quantization schemes from the experimental
**[ciru-ai/ROCmFPX](https://github.com/ciru-ai/ROCmFPX)** llama.cpp fork,
tuned for and benchmarked on AMD Strix Halo (Radeon 8060S iGPU, gfx1151, unified memory):

- `ROCmFP3/4/6/8` tensor types with straight and "agent" presets (agent presets keep
  tool-calling / JSON-structured output reliable at low bit-widths)
- Files load **only** on the fork -- it is an experimental upstream research
  build, so build from the pinned commit that produced these files (the
  default branch may have moved on since):

```bash
git clone {ROCMFPX_REPO} ROCmFPX
cd ROCmFPX
git checkout {ROCMFPX_PIN}
# then build per the fork's own README
```""")

    # Training details (only if training was done)
    if cfg.did_training:
        dataset_link = f"[{Path(dataset_name).stem}](https://huggingface.co/datasets/{dataset_repo_id})" if dataset_repo_id else dataset_name
        body_sections.append(f"""## Training Details

| Parameter | Value |
|-----------|-------|
| Method | QLoRA with completion-only loss masking |
| LoRA rank (r) | {cfg.lora_r} |
| LoRA alpha | {cfg.lora_alpha} |
| LoRA dropout | {cfg.lora_dropout} |
| Epochs | {cfg.num_epochs} |
| Learning rate | {cfg.learning_rate} |
| LR scheduler | {cfg.lr_scheduler} |
| Batch size | {cfg.batch_size} (effective {effective_batch} with gradient accumulation) |
| Optimizer | {cfg.optimizer} |
| Training sequence length | {cfg.max_seq_length} |
| Precision | BF16 |
| Dataset | {dataset_link} |
| Hardware | AMD Ryzen AI Max+ 395 (Strix Halo), 128 GB unified memory (GTT), ROCm |

**Completion-only loss**: Only assistant response turns contribute to the training loss.
System and user turns are masked, so the model learns to generate responses rather
than memorizing prompts.""")

    # Files section
    if files_section:
        body_sections.append(files_section.strip())

    # Usage section (only if GGUF files present). Snippets always use a real
    # filename from files_to_upload -- never a synthesized "<repo>-Q5.gguf".
    if has_gguf:
        example_gguf = _pick_example_gguf(files_to_upload)
        mmproj_name = _find_mmproj(files_to_upload)
        vision_snippet = ""
        if mmproj_name and example_gguf:
            vision_snippet = f"""

### Vision (image input)

```bash
llama-server -m {example_gguf} --mmproj {mmproj_name} -c 8192 --port 8080 -ngl 99 -fa on
```"""
        example_gguf = example_gguf or f"{repo_name}.gguf"

        if rocmfpx:
            body_sections.append(f"""## Usage

Requires a from-source build of the [ROCmFPX fork](https://github.com/ciru-ai/ROCmFPX)
(stock llama.cpp, LM Studio, and Ollama cannot load these files):

```bash
# Interactive chat (--jinja uses the model's embedded chat template)
llama-cli -m {example_gguf} -c 8192 --jinja -cnv

# Server mode
llama-server -m {example_gguf} -c 8192 --port 8080 -ngl 99 -fa on --jinja
```{vision_snippet}""")
        else:
            body_sections.append(f"""## Usage

### LM Studio

1. Download the GGUF file of your preferred quantization tier
2. Place it in your LM Studio models directory
3. Load the model in LM Studio -- it will auto-detect the chat template
4. The model supports the base model's full context length

### llama.cpp

```bash
# Interactive chat (--jinja uses the model's embedded chat template, not a hardcoded one)
llama-cli -m {example_gguf} -c 8192 --jinja -cnv

# Single prompt
llama-cli -m {example_gguf} -c 8192 -p "Your prompt here"

# Server mode
llama-server -m {example_gguf} -c 8192 --port 8080 --jinja
```

### Python (llama-cpp-python)

```python
from llama_cpp import Llama

llm = Llama(model_path="./{example_gguf}", n_ctx=8192)
output = llm.create_chat_completion(
    messages=[
        {{"role": "user", "content": "Hello, how are you?"}}
    ]
)
print(output["choices"][0]["message"]["content"])
```{vision_snippet}""")

    # MTP speculative decoding (only if a produced GGUF carries "nextn" draft tensors)
    if mtp_gguf_path is not None:
        # Rewrite local absolute paths (server binary, GGUF) to repo-relative
        # names — the card is public; box-local paths mean nothing to readers.
        argv = build_serve_command(str(mtp_gguf_path))
        argv[0] = "llama-server"
        argv = [mtp_gguf_path.name if a == str(mtp_gguf_path) else a for a in argv]
        serve_command = format_serve_command(argv)
        body_sections.append(f"""## Serving: MTP Speculative Decoding

This model includes **MTP ("nextn") draft tensors**, enabling self-speculative
decoding -- measured **~1.6-1.9x faster generation** with a ~95% first-token
accept rate (no separate draft model needed; it drafts from itself):

```bash
{serve_command}
```

**Memory cost:** MTP needs its own draft context alongside the main context,
so serving with it uses roughly **2x the model's memory** compared to serving
without ``-md``/``--spec-type draft-mtp``.""")

    # Caveats (dynamic)
    caveats = []
    caveats.append(f"- The base model's license ({cfg.license}) applies to all derivative files")
    if cfg.did_training:
        caveats.append("- This is a **personal fine-tune**, not an official release from the base model authors")
        caveats.append("- Quality depends on the training data and may not generalize to all tasks")
    if cfg.did_heretic:
        caveats.append("- **Safety alignment has been removed** -- this model may produce harmful, offensive, or dangerous content")
        caveats.append("- The abliteration process may slightly degrade model quality on some benchmarks")
    if cfg.did_reap:
        caveats.append("- Expert pruning removes a fraction of experts per MoE layer; some task-specific knowledge may be lost")
        caveats.append("- The pruned model has a different number of experts from the original — tooling that hardcodes expert count may need adjustment")
    if rocmfpx:
        caveats.append("- **Fork-only files**: stock llama.cpp, LM Studio, and Ollama cannot load these -- build [ciru-ai/ROCmFPX](https://github.com/ciru-ai/ROCmFPX) from source")
    if cfg.did_magicquant or has_gguf:
        caveats.append("- Quantization reduces precision -- verify outputs for your specific use case")
    if cfg.did_magicquant:
        caveats.append("- The hybrid quantization assigns different precision to different tensor groups, which means quality characteristics may differ from uniform quantizations")
    body_sections.append("## Caveats\n\n" + "\n".join(caveats))

    # Limitations (dynamic)
    limitations = []
    if cfg.did_training:
        limitations.append(f"- Training data used sequences up to {cfg.max_seq_length} tokens; the model retains the base model's full context window")
        limitations.append("- Performance on tasks not represented in the training data may be degraded")
    if cfg.did_reap:
        limitations.append("- Pruned experts cannot be recovered; any capabilities concentrated in removed experts are lost")
    if cfg.did_magicquant or has_gguf:
        limitations.append("- Quantized models may exhibit subtle differences from the full-precision fine-tune")
    limitations.append("- This model inherits any limitations and biases present in the base model")
    body_sections.append("## Limitations\n\n" + "\n".join(limitations))

    # Pipeline credit
    pipeline_parts = []
    if cfg.did_training:
        pipeline_parts.append("[Foundry](https://github.com/lucasmcoleman/Foundry)")
    if cfg.did_heretic:
        pipeline_parts.append("[Heretic](https://github.com/p-e-w/heretic)")
    if cfg.did_reap:
        pipeline_parts.append("[REAP](https://github.com/CerebrasResearch/reap)")
    if cfg.did_magicquant:
        pipeline_parts.append("[MagicQuant](https://github.com/lucasmcoleman/MagicQuant)")
    body_sections.append("---\n*Generated with " + " + ".join(pipeline_parts or ["Foundry"]) + "*")

    body = "\n\n".join(body_sections)
    card = f"{yaml_block}\n\n{body}\n"
    return card


# ── Retry wrappers ──────────────────────────────────────────────────────────


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(min=2, max=30),
    retry=retry_if_exception_type(requests.exceptions.RequestException),
)
def _resolve_hf_token() -> Optional[str]:
    """HF token from the env var, else the standard HF credential store.

    Only HF_TOKEN was consulted, which meant an upload failed with
    "HF_TOKEN environment variable is not set" on a machine that was
    perfectly well authenticated -- `huggingface-cli login` writes
    ~/.cache/huggingface/token, and every download in the same pipeline had
    just used it happily. Failing at the upload step, after hours of search,
    is the worst possible place to discover that.

    Env var still wins so a caller can override the logged-in identity.
    """
    env = os.environ.get("HF_TOKEN")
    if env:
        return env
    try:
        from huggingface_hub import get_token
    except ImportError:
        return None
    return get_token()


def _create_repo_with_retry(api, **kwargs):
    """Create or verify a HuggingFace repo with automatic retry on transient failures."""
    return api.create_repo(**kwargs)


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(min=2, max=30),
    retry=retry_if_exception_type(requests.exceptions.RequestException),
)
def _upload_with_retry(api, **kwargs):
    """Upload a single file to HuggingFace with automatic retry on transient failures."""
    return api.upload_file(**kwargs)


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(min=2, max=30),
    retry=retry_if_exception_type(requests.exceptions.RequestException),
)
def _whoami_with_retry(api):
    """Validate HuggingFace token with automatic retry on transient failures."""
    return api.whoami()


# ── Dry-run mode ─────────────────────────────────────────────────────────────

@dataclass
class DryRunReport:
    """Result of a dry-run upload check."""
    token_valid: bool = False
    token_username: str = ""
    repo_accessible: bool = False
    repo_exists: bool = False
    repo_id: str = ""
    files: list[tuple[str, str, float]] = field(default_factory=list)  # (local, repo_path, size_gb)
    total_size_gb: float = 0.0
    model_card_preview: str = ""
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.token_valid and self.repo_accessible and len(self.files) > 0 and not self.errors


def dry_run(
    cfg: HFUploadConfig,
    output_dir: str,
    log: LogFn = _default_log,
    token: Optional[str] = None,
) -> DryRunReport:
    """Validate credentials and repo access, report what would be uploaded.

    Does NOT upload anything.

    Args:
        cfg: Upload configuration
        output_dir: Directory containing artifacts to upload
        log: Logging callback
        token: HF token override (defaults to HF_TOKEN env var)

    Returns:
        DryRunReport with validation results and file list
    """
    report = DryRunReport(repo_id=cfg.repo_id)

    try:
        from huggingface_hub import HfApi
    except ImportError:
        report.errors.append("huggingface_hub is not installed (pip install huggingface_hub)")
        log("huggingface_hub not installed", "error")
        return report

    # 1. Validate token
    log("Dry run: validating HF credentials", "stage")
    hf_token = token or _resolve_hf_token()
    if not hf_token:
        report.errors.append("HF_TOKEN environment variable is not set")
        log("HF_TOKEN not set", "error")
        return report

    api = HfApi(token=hf_token)
    try:
        user_info = _whoami_with_retry(api)
        report.token_valid = True
        report.token_username = user_info.get("name", user_info.get("fullname", "unknown"))
        log(f"  Authenticated as: {report.token_username}")
    except Exception as e:
        report.errors.append(f"Token validation failed: {e}")
        log(f"Token validation failed: {e}", "error")
        return report

    # 2. Check repo access
    if not cfg.repo_id:
        report.errors.append("No repo_id configured")
        log("No repo_id configured", "error")
        return report

    log(f"  Checking repo: {cfg.repo_id}")
    try:
        api.repo_info(repo_id=cfg.repo_id, repo_type="model")
        report.repo_exists = True
        report.repo_accessible = True
        log(f"  Repo exists and is accessible")
    except Exception:
        # Repo doesn't exist yet -- check if we can create it
        report.repo_exists = False
        # Verify the namespace matches the authenticated user or their orgs
        namespace = cfg.repo_id.split("/")[0] if "/" in cfg.repo_id else report.token_username
        try:
            orgs = [o.get("name", "") for o in user_info.get("orgs", [])]
        except (AttributeError, TypeError):
            orgs = []

        if namespace == report.token_username or namespace in orgs:
            report.repo_accessible = True
            log(f"  Repo does not exist -- will be created on upload")
        else:
            report.repo_accessible = False
            report.errors.append(
                f"Cannot create repo under namespace '{namespace}' "
                f"(authenticated as '{report.token_username}', orgs: {orgs})"
            )
            log(f"  No access to namespace '{namespace}'", "error")

    # 3. Discover files
    log("  Scanning for uploadable files...")
    file_tuples = discover_upload_files(
        output_dir,
        upload_gguf=cfg.upload_gguf,
        upload_lora=cfg.upload_lora,
        upload_merged=cfg.upload_merged,
    )

    if not file_tuples:
        report.warnings.append("No files found to upload in the output directory")
        log("  No files found to upload", "warn")
    else:
        total = 0.0
        for local_path, repo_path in file_tuples:
            size_gb = local_path.stat().st_size / 1e9
            total += size_gb
            report.files.append((str(local_path), repo_path, size_gb))
            log(f"    {repo_path} ({size_gb:.2f} GB)")
        report.total_size_gb = total
        log(f"  Total upload size: {total:.2f} GB")

    # 4. Generate model card preview
    report.model_card_preview = generate_model_card(cfg, file_tuples)

    # Summary
    log("", "info")
    log("Dry run summary:", "stage")
    log(f"  Token valid:      {report.token_valid}")
    log(f"  User:             {report.token_username}")
    log(f"  Repo:             {report.repo_id}")
    log(f"  Repo exists:      {report.repo_exists}")
    log(f"  Repo accessible:  {report.repo_accessible}")
    log(f"  Files to upload:  {len(report.files)}")
    log(f"  Total size:       {report.total_size_gb:.2f} GB")
    if report.errors:
        for e in report.errors:
            log(f"  ERROR: {e}", "error")
    if report.warnings:
        for w in report.warnings:
            log(f"  WARNING: {w}", "warn")
    if report.ok:
        log("Dry run PASSED -- ready to upload", "success")
    else:
        log("Dry run FAILED -- see errors above", "error")

    return report


# ── Upload with progress ─────────────────────────────────────────────────────

def upload(
    cfg: HFUploadConfig,
    output_dir: str,
    log: LogFn = _default_log,
    token: Optional[str] = None,
) -> bool:
    """Upload artifacts to HuggingFace Hub with progress reporting.

    Args:
        cfg: Upload configuration
        output_dir: Directory containing artifacts to upload
        log: Logging callback (msg, level)
        token: HF token override (defaults to HF_TOKEN env var)

    Returns:
        True if all uploads succeeded
    """
    try:
        from huggingface_hub import HfApi, ModelCard
    except ImportError:
        log("huggingface_hub not installed (pip install huggingface_hub)", "error")
        return False

    hf_token = token or _resolve_hf_token()
    if not hf_token:
        log("HF_TOKEN environment variable is not set", "error")
        return False

    if not cfg.repo_id:
        log("No repo_id configured for upload", "error")
        return False

    api = HfApi(token=hf_token)

    # Validate credentials
    try:
        user_info = _whoami_with_retry(api)
        username = user_info.get("name", "unknown")
        log(f"Authenticated as: {username}")
    except Exception as e:
        log(f"Authentication failed: {e}", "error")
        return False

    # Plan repos: one per quant family present (MagicQuant / ROCmFPX siblings
    # when both exist), so fork-only files never bury stock-llama.cpp ones.
    repo_plan = plan_gguf_repos(output_dir, cfg.repo_id) if cfg.upload_gguf else [(cfg.repo_id, "auto")]
    if len(repo_plan) > 1:
        log(f"Both MagicQuant and ROCmFPX quants found — splitting into sibling repos: "
            + ", ".join(r for r, _ in repo_plan), "stage")

    # Upload dataset as a separate HF dataset repo if configured (before model card so we can link it)
    dataset_repo_id = ""
    if cfg.upload_dataset and cfg.dataset_name:
        ds_path = Path(cfg.dataset_name)
        if not ds_path.is_absolute():
            ds_path = Path(output_dir).parent / cfg.dataset_name
            if not ds_path.exists():
                ds_path = Path(output_dir) / cfg.dataset_name
        if ds_path.exists() and ds_path.is_file():
            # Derive dataset repo name from model repo: "user/model-GGUF" -> "user/model-training-data"
            base = _repo_base(cfg.repo_id)
            namespace = base.split("/")[0] if "/" in base else username
            model_short = base.split("/")[-1]
            dataset_repo_id = f"{namespace}/{model_short}-training-data"

            log(f"Uploading dataset to {dataset_repo_id}", "stage")
            try:
                _create_repo_with_retry(
                    api,
                    repo_id=dataset_repo_id,
                    repo_type="dataset",
                    private=cfg.private,
                    exist_ok=True,
                )
                _upload_with_retry(
                    api,
                    path_or_fileobj=str(ds_path),
                    path_in_repo=ds_path.name,
                    repo_id=dataset_repo_id,
                    repo_type="dataset",
                    commit_message=f"Upload training data ({ds_path.name})",
                )
                log(f"  Dataset uploaded to https://huggingface.co/datasets/{dataset_repo_id}", "success")
            except Exception as e:
                log(f"  Dataset upload failed (continuing): {e}", "warn")
                dataset_repo_id = ""

    # Upload each planned repo: its family's GGUFs + shared extras (lora/merged
    # go with the first repo only, so siblings don't duplicate them)
    for repo_index, (repo_id, family) in enumerate(repo_plan):
        is_primary = repo_index == 0
        files_to_upload = discover_upload_files(
            output_dir,
            upload_gguf=cfg.upload_gguf,
            upload_lora=cfg.upload_lora and is_primary,
            upload_merged=cfg.upload_merged and is_primary,
            gguf_family=family,
        )
        if not files_to_upload:
            log(f"No files found to upload for {repo_id}", "warn")
            return False

        log(f"Creating/verifying repo: {repo_id}", "stage")
        try:
            _create_repo_with_retry(
                api,
                repo_id=repo_id,
                repo_type="model",
                private=cfg.private,
                exist_ok=True,
            )
            log(f"  Repo ready: https://huggingface.co/{repo_id}")
        except Exception as e:
            log(f"Failed to create/access repo: {e}", "error")
            return False

        total_size = sum(f.stat().st_size for f, _ in files_to_upload) / 1e9
        log(f"Found {len(files_to_upload)} files ({total_size:.2f} GB total)")

        # Generate and upload model card (after dataset upload so we have the repo ID to link)
        log("Generating model card", "stage")
        sibling_repo_id = ""
        if len(repo_plan) > 1:
            sibling_repo_id = next(r for r, _ in repo_plan if r != repo_id)
        # dropped_tiers covers the WHOLE run, but a card is per sibling repo.
        # Unpartitioned, the MagicQuant card claimed "Q5 was not published"
        # using the ROCmFPX Q5's rejection reason -- on a repo where Q5 is
        # published. Keep only this family's entries.
        fam_dropped = [
            d for d in (getattr(cfg, "dropped_tiers", None) or [])
            if d.get("family", family) == family
        ]
        # And never claim a tier is absent when the repo actually has one:
        # an earlier run's file can still be sitting there (the uploader adds,
        # it does not reconcile), and a card contradicting its own file list
        # is worse than no note at all.
        # Same tier-matching rule the post-push audit uses (_repo_tiers_present),
        # so the check that suppresses a claim and the check that flags an
        # unsuppressed one can never disagree about what counts as a file.
        repo_files_now: list[str] = []
        present = _repo_tiers_present(rp for _, rp in files_to_upload)
        try:
            from huggingface_hub import list_repo_files
            repo_files_now = list(list_repo_files(repo_id, token=hf_token))
            present |= _repo_tiers_present(repo_files_now)
        except Exception:
            pass    # repo may not exist yet; the local set is enough
        suppressed = [d for d in fam_dropped if d.get("tier") in present]
        for d in suppressed:
            log(f"  NOT claiming {d.get('tier')} is missing -- a file for it "
                f"is present in {repo_id} (likely from an earlier run)", "warn")
        fam_dropped = [d for d in fam_dropped if d.get("tier") not in present]

        # Anything on the hub this run did not upload came from an earlier
        # search. Disclose it rather than let one card speak for two runs.
        # A caller that ran reconciliation (see _publish_tiers.reconcile)
        # supplies richer entries -- band verified against the current
        # boundaries, mislabelled ones flagged. Fall back to a name+size scan
        # so any caller still discloses something.
        carried = [c for c in (getattr(cfg, "carried_over", None) or [])
                   if c.get("family", family) == family]
        if not carried:
            mine = {rp for _, rp in files_to_upload}
            try:
                from huggingface_hub import HfApi
                for s in HfApi().repo_info(repo_id, files_metadata=True,
                                           token=hf_token).siblings:
                    n = s.rfilename
                    if _is_tier_gguf(n) and n not in mine:
                        carried.append({"name": n,
                                        "gib": (s.size or 0) / 2 ** 30})
            except Exception:
                pass    # new repo or metadata unavailable: nothing to disclose
        for c in carried:
            log(f"  carried over from an earlier run: {c['name']} "
                f"({c['gib']:.2f} GiB)", "warn")

        fam_refused = [r for r in (getattr(cfg, "refused_tiers", None) or [])
                       if r.get("family", family) == family]
        card_cfg = replace(cfg, repo_id=repo_id, dropped_tiers=fam_dropped,
                           carried_over=carried, refused_tiers=fam_refused)
        card_content = generate_model_card(
            card_cfg,
            files_to_upload,
            dataset_repo_id=dataset_repo_id,
            rocmfpx=(family == "rocmfpx"),
            sibling_repo_id=sibling_repo_id,
            log=log,
        )

        # Audit BEFORE the card goes live, against what the repo holds now
        # plus what this run is about to add. Auditing only after the push
        # meant the contradicting card was already public by the time the
        # warning printed, and the repo listing needed for it was already
        # fetched above -- so there was nothing to gain by waiting.
        try:
            audit_card_against_repo(
                card_content,
                sorted(set(repo_files_now) | {rp for _, rp in files_to_upload}),
                log=log, repo_id=repo_id,
            )
        except Exception as e:
            log(f"  Pre-push card audit skipped: {e}", "warn")

        try:
            card = ModelCard(card_content)
            card.push_to_hub(repo_id, token=hf_token)
            log("  Model card uploaded", "success")
        except Exception as e:
            log(f"  Model card upload failed (continuing with files): {e}", "warn")

        # Upload files with progress
        log(f"Uploading {len(files_to_upload)} files", "stage")
        for i, (local_path, repo_path) in enumerate(files_to_upload, 1):
            size_gb = local_path.stat().st_size / 1e9
            log(f"  [{i}/{len(files_to_upload)}] {repo_path} ({size_gb:.2f} GB)")

            try:
                _upload_with_retry(
                    api,
                    path_or_fileobj=str(local_path),
                    path_in_repo=repo_path,
                    repo_id=repo_id,
                    repo_type="model",
                    commit_message=f"Upload {repo_path}",
                )
                pct = int(100 * i / len(files_to_upload))
                log(f"    Uploaded ({pct}% complete)", "success")
            except Exception as e:
                log(f"    Failed to upload {repo_path}: {e}", "error")
                return False

        log(f"All files uploaded to https://huggingface.co/{repo_id}", "success")

        # Post-upload self-audit: verify the card we just pushed doesn't
        # contradict what the repo actually holds now that the upload is
        # done. Detector only -- an upload that succeeded must not be
        # reported as failed, so failures here are logged and swallowed.
        try:
            from huggingface_hub import list_repo_files
            final_files = list_repo_files(repo_id, token=hf_token)
            audit_card_against_repo(card_content, final_files, log=log, repo_id=repo_id)
        except Exception as e:
            log(f"  Card audit skipped (could not list repo files): {e}", "warn")

    return True


# ── Post-upload card audit ───────────────────────────────────────────────────

def audit_card_against_repo(
    card_content: str,
    repo_files: list[str],
    log: LogFn = _default_log,
    repo_id: str = "",
) -> list[str]:
    """Verify a just-pushed model card doesn't contradict the repo it describes.

    Runs against the actual repo contents, not a log -- the previous
    find_refusals() scraped log text with a regex, which broke the moment
    logs were rotated, cleaned up, or reworded. This checks real artifacts.
    Never raises and never fails the upload; it is purely a detector so this
    class of bug (three of which reached public cards on 2026-08-0x) is
    caught the moment it recurs instead of days later:
      1. dropped_tiers computed for the wrong sibling repo -> a card claims
         "<TIER> was not published" while a file for that tier is right there.
      2. the uploader adds files but never reconciles -> an older file for a
         tier survives a later run whose card still calls the tier missing.
      3. a card regenerated after local GGUFs were cleaned up -> the file
         table silently drops every row but mmproj.

    Returns the list of warning strings (also emitted via ``log``) so callers
    and tests can inspect findings without re-parsing logs.
    """
    warnings: list[str] = []
    where = f" in {repo_id}" if repo_id else ""

    # 1. Every .gguf in the repo must be ACCOUNTED FOR somewhere in the card
    #    -- a table row, or a named entry under "Files from an earlier build".
    #    Requiring a table row specifically was wrong: carried-over files are
    #    deliberately disclosed in prose, not the table, so a card handling
    #    the 23 GiB Q6 exactly right still got flagged, and that noise looked
    #    identical to the one signal that matters (a table that collapsed
    #    after cleanup deleted the local GGUFs).
    table_names = set(re.findall(r"\[([^\]]+\.gguf)\]\(\./", card_content))
    prose_names = set(re.findall(r"`([^`]+\.gguf)`", card_content))
    accounted = table_names | prose_names
    for f in repo_files:
        if f.lower().endswith(".gguf") and f not in accounted:
            warnings.append(
                f"'{f}' is in the repo{where} but the card never mentions it "
                f"-- no table row and no carried-over entry"
            )

    # 2. No claim that a tier is absent may be contradicted by a file for that
    #    tier actually sitting in the repo. TWO claim shapes reach a page:
    #    dropped tiers ("**Q5 was not published.**") and refused ones
    #    ("- **Q5** -- ..." under "Tiers this build does not produce", which
    #    goes further and promises the file "will not appear in a later build
    #    either"). The second was unchecked, so the strongest contradiction in
    #    the set was the one nothing looked for.
    claimed_absent = [
        (t, "was not published")
        for t in re.findall(r"\*\*(.+?) was not published\.\*\*", card_content)
    ]
    refused_section = card_content.split(
        "## Tiers this build does not produce", 1)
    if len(refused_section) > 1:
        body = refused_section[1].split("\n## ", 1)[0]
        claimed_absent += [
            (t, "is not produced by this build")
            for t in re.findall(r"^- \*\*(.+?)\*\* --", body, re.M)
        ]
    #    Matched only against files that could actually BE that tier: bounded
    #    (so "Q4" doesn't hit "Q40") and .gguf-only via _is_tier_gguf, because
    #    a tier-named imatrix or search-results artifact is not a published
    #    quant and must not suppress a true claim.
    for tier, phrasing in claimed_absent:
        boundary = re.compile(
            rf"(?<![A-Za-z0-9]){re.escape(tier)}(?![A-Za-z0-9])", re.IGNORECASE
        )
        hit = next((f for f in repo_files
                    if _is_tier_gguf(f) and boundary.search(f)), None)
        if hit:
            warnings.append(
                f"card claims '{tier}' {phrasing}{where}, but "
                f"'{hit}' matches that tier and IS in the repo"
            )

    # 3. Every "Files from an earlier build" entry must actually exist --
    #    that section names files NOT in this run's own upload, so nothing
    #    else in the pipeline guarantees they're real.
    parts = card_content.split("## Files from an earlier build", 1)
    if len(parts) > 1:
        section = parts[1].split("\n## ", 1)[0]  # stop at the next heading
        for name in re.findall(r"`([^`]+)`", section):
            if name not in repo_files:
                warnings.append(
                    f"card lists '{name}' as carried over from an earlier "
                    f"build{where}, but it is not in the repo"
                )

    for w in warnings:
        log(f"  CARD AUDIT: {w}", "warn")
    if not warnings:
        log(f"  Card audit passed -- no mismatches found{where}", "info")

    return warnings


# ── CLI ──────────────────────────────────────────────────────────────────────

def main():
    """CLI entry point for standalone upload / dry-run."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Upload model artifacts to HuggingFace Hub",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Dry run (validate without uploading)
  python hf_upload.py --repo user/model-name --output-dir ./output --dry-run

  # Upload GGUF files
  python hf_upload.py --repo user/model-name --output-dir ./output

  # Upload everything (GGUF + LoRA + merged)
  python hf_upload.py --repo user/model-name --output-dir ./output --lora --merged

  # Show generated model card
  python hf_upload.py --repo user/model-name --output-dir ./output --dry-run --show-card
""",
    )
    parser.add_argument("--repo", required=True, help="HuggingFace repo ID (user/model-name)")
    parser.add_argument("--output-dir", required=True, help="Pipeline output directory")
    parser.add_argument("--base-model", default="", help="Base model ID for model card")
    parser.add_argument("--dataset", default="", help="Dataset name for model card")
    parser.add_argument("--license", default="apache-2.0", help="License identifier")
    parser.add_argument("--private", action="store_true", help="Create as private repo")
    parser.add_argument("--public", action="store_true", help="Create as public repo")
    parser.add_argument("--lora", action="store_true", help="Upload LoRA adapters")
    parser.add_argument("--merged", action="store_true", help="Upload merged model")
    parser.add_argument("--no-gguf", action="store_true", help="Skip GGUF upload")
    parser.add_argument("--dry-run", action="store_true", help="Validate without uploading")
    parser.add_argument("--show-card", action="store_true", help="Print the generated model card")
    parser.add_argument("--lora-r", type=int, default=32, help="LoRA rank (for model card)")
    parser.add_argument("--lora-alpha", type=int, default=64, help="LoRA alpha (for model card)")
    parser.add_argument("--epochs", type=int, default=3, help="Training epochs (for model card)")
    parser.add_argument("--seq-length", type=int, default=8192, help="Max sequence length (for model card)")
    args = parser.parse_args()

    cfg = HFUploadConfig(
        repo_id=args.repo,
        private=not args.public if args.public else (args.private if args.private else True),
        license=args.license,
        upload_gguf=not args.no_gguf,
        upload_lora=args.lora,
        upload_merged=args.merged,
        base_model=args.base_model,
        dataset_name=args.dataset,
        lora_r=args.lora_r,
        lora_alpha=args.lora_alpha,
        num_epochs=args.epochs,
        max_seq_length=args.seq_length,
    )

    if args.dry_run:
        report = dry_run(cfg, args.output_dir)
        if args.show_card and report.model_card_preview:
            print("\n" + "=" * 60)
            print("MODEL CARD PREVIEW")
            print("=" * 60)
            print(report.model_card_preview)
        sys.exit(0 if report.ok else 1)
    else:
        ok = upload(cfg, args.output_dir)
        sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
