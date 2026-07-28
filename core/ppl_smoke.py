"""Shared post-generation perplexity "smoke test" for the magicquant and
rocmfpx stages (H2 precedent: see ``core/reap_common.py`` for shared-helper
placement when two entry modules need the same logic).

Why this exists: 2026-07-27, MagicQuant's SafetensorsSource silently produced
garbage quants for the qwen3_5 arch (missing HF->GGUF value transforms). The
GGUF-source path was always correct, and the arch gate added in MagicQuant
c169090 now blocks that specific case -- but the pipeline itself had NO output
sanity check of any kind; the broken files were only caught by a perplexity run
an operator happened to run externally, by hand, after the fact. This module
gives every GGUF the stage produces one cheap, automatic look before it can
reach upload: a short llama-perplexity pass over a fixed corpus. A healthy
model lands in the single/low-double digits on wikitext; a pathological one
(uniform logits, garbage weights, wrong tensor layout, ...) reads out near
vocab_size (e.g. ~248320) -- an unmistakable signal cheaply distinguishable
from a real quality regression, which this is NOT trying to catch.

Advisory-on-unknown, like ``core/preflight.py``: a missing binary or missing
corpus SKIPS with a warning rather than blocking (we can't smoke-test what we
can't run) -- but a *completed* run that comes back pathological is a hard
stage failure, not a warning, because that is exactly the silent-garbage
failure mode this module exists to close.

Module import is stdlib-only (no torch/magicquant/etc.) so it loads cleanly
from both entry modules' lightweight startup path, matching their existing
import discipline.
"""

from __future__ import annotations

import math
import os
import re
import subprocess
from pathlib import Path
from typing import Callable, Optional, Tuple

# Env var names (all overridable per the task spec).
SKIP_ENV = "FOUNDRY_SKIP_SMOKE_PPL"
CORPUS_ENV = "FOUNDRY_SMOKE_CORPUS"
THRESHOLD_ENV = "FOUNDRY_SMOKE_PPL_MAX"

DEFAULT_CORPUS = "/server/ai/wikitext/wikitext-2-raw/wiki.test.raw"
# Healthy quants land in the single/low-double digits on wikitext; the known
# uniform-logits pathology reads out near vocab_size (e.g. ~248320) -- 100.0
# sits comfortably above any legitimate model's PPL yet nowhere near that
# failure signature, so it can't be tripped by ordinary quantization loss.
DEFAULT_PPL_MAX = 100.0

_PPL_RE = re.compile(r"Final estimate:\s*PPL\s*=\s*(\S+)")


# ── pure: parsing + verdict (unit-testable without any subprocess) ──────────

def parse_ppl(output: str) -> Optional[float]:
    """Extract the PPL value from ``llama-perplexity`` stdout/stderr text.

    Looks for llama.cpp's own ``Final estimate: PPL = <value>`` line. Returns
    None if the line is absent. ``float()`` parses ``nan``/``inf`` tokens
    directly (case-insensitively) if llama-perplexity ever emits one, so a
    caller only needs ``math.isnan``/``math.isinf`` on the result -- no
    separate NaN-string handling needed here.
    """
    m = _PPL_RE.search(output)
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


def smoke_verdict(
    exit_code: int, ppl: Optional[float], threshold: float = DEFAULT_PPL_MAX,
) -> Tuple[bool, str]:
    """Pass/fail verdict for one smoke run, from its raw outcome.

    FAIL conditions (in priority order, matching the task spec): nonzero exit,
    no parsed PPL, NaN/inf PPL, or PPL over ``threshold``. Returns
    ``(ok, human_reason)`` -- the reason is logged either way so a PASS is
    just as traceable as a FAIL.
    """
    if exit_code != 0:
        return False, f"llama-perplexity exited {exit_code}"
    if ppl is None:
        return False, "no 'Final estimate: PPL = ...' line found in output"
    if math.isnan(ppl) or math.isinf(ppl):
        return False, f"PPL is {ppl} (NaN/inf -- pathological output)"
    if ppl > threshold:
        return False, (
            f"PPL {ppl:.2f} exceeds threshold {threshold:.1f} "
            "(pathological quant -- e.g. the uniform-logits failure mode reads "
            "out near vocab_size)"
        )
    return True, f"PPL {ppl:.2f} <= threshold {threshold:.1f}"


# ── env resolution (environ injectable for tests, matching
#    _magicquant_entry.apply_dequant_env's pattern) ─────────────────────────

def is_skipped(environ: Optional[dict] = None) -> bool:
    environ = os.environ if environ is None else environ
    return environ.get(SKIP_ENV) == "1"


def resolve_corpus(environ: Optional[dict] = None) -> Optional[str]:
    """Return the smoke-test corpus path, or None if none is available.

    ``FOUNDRY_SMOKE_CORPUS`` wins when set; else the default wikitext-2-raw
    path if it exists on this box; else None (caller must skip -- no corpus,
    no perplexity, nothing to smoke-test against).
    """
    environ = os.environ if environ is None else environ
    corpus = environ.get(CORPUS_ENV)
    if corpus:
        return corpus
    if Path(DEFAULT_CORPUS).exists():
        return DEFAULT_CORPUS
    return None


def resolve_threshold(environ: Optional[dict] = None) -> float:
    """Return the PPL fail threshold: ``FOUNDRY_SMOKE_PPL_MAX`` if set (and
    parseable) and ``DEFAULT_PPL_MAX`` otherwise."""
    environ = os.environ if environ is None else environ
    raw = environ.get(THRESHOLD_ENV)
    if raw:
        try:
            return float(raw)
        except ValueError:
            pass
    return DEFAULT_PPL_MAX


# ── binary discovery ─────────────────────────────────────────────────────────

def find_perplexity_bin(base_dir: Optional[str]) -> Optional[Path]:
    """Locate a ``llama-perplexity`` binary near ``base_dir``.

    Mirrors ``_magicquant_entry._find_convert_script``'s search shape:
    ``base_dir`` may be a llama.cpp/ROCmFPX *source root* or a *build subdir*
    (e.g. ``~/ROCmFPX/build-strix-rocmfp4`` or ``~/llama.cpp/build``), so this
    checks the dir itself, its ``bin/``, ``build/bin/``, and the
    ROCmFPX-specific ``build-strix-rocmfp4/bin/`` layout, then walks up to 4
    parents doing the same (covers a hint that already points *at* a bin/
    dir). Returns None (never raises) on a bad/missing/unbuilt dir --
    ``smoke_test_gguf`` treats an unresolved binary as advisory-skip, not a
    failure.
    """
    if not base_dir:
        return None
    d = Path(base_dir)
    rel_candidates = [
        Path("llama-perplexity"),
        Path("bin/llama-perplexity"),
        Path("build/bin/llama-perplexity"),
        Path("build-strix-rocmfp4/bin/llama-perplexity"),
    ]
    candidates = [d / rel for rel in rel_candidates]
    for parent in list(d.parents)[:4]:
        candidates += [parent / rel for rel in rel_candidates]
    for c in candidates:
        if c.exists():
            return c
    return None


# ── subprocess invocation (thin -- the testable logic is parse_ppl/
#    smoke_verdict above) ────────────────────────────────────────────────────

def run_llama_perplexity(
    perplexity_bin: Path, gguf_path: Path, corpus: str, chunks: int = 4,
) -> Tuple[int, str]:
    """Run one llama-perplexity smoke pass. Returns ``(exit_code, combined
    stdout+stderr)``. Deliberately thin -- no parsing/verdict logic here."""
    cmd = [
        str(perplexity_bin), "-m", str(gguf_path), "-f", str(corpus),
        "--ctx-size", "512", "--batch-size", "512", "--ubatch-size", "128",
        "--chunks", str(chunks),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


# ── top-level orchestration, called once per produced GGUF ─────────────────

def smoke_test_gguf(
    perplexity_bin: Optional[Path],
    gguf_path: Path,
    *,
    log: Optional[Callable[[str], None]] = None,
) -> bool:
    """Run the full post-generation PPL smoke gate for one GGUF file.

    Returns True when the file passed, or when the gate is skipped/advisory
    (unknown binary, no corpus, ``FOUNDRY_SKIP_SMOKE_PPL=1``) -- False only
    when a smoke run actually completed and came back pathological. Callers
    (``_magicquant_entry.run`` / ``_rocmfpx_entry.run``) must hard-abort
    (``sys.exit(1)``) if this returns False for any produced file, BEFORE
    printing their ``PIPELINE_STAGE_COMPLETE`` marker.
    """
    if log is None:
        log = lambda msg: print(msg, flush=True)  # noqa: E731

    if is_skipped():
        log(f"PPL smoke test SKIPPED for {gguf_path.name} ({SKIP_ENV}=1)")
        return True
    if perplexity_bin is None or not Path(perplexity_bin).exists():
        log(
            f"PPL smoke test SKIPPED for {gguf_path.name}: llama-perplexity "
            "binary not found (advisory -- an unresolvable binary never "
            "blocks, matching preflight.py's philosophy)"
        )
        return True
    corpus = resolve_corpus()
    if corpus is None:
        log(
            f"PPL smoke test SKIPPED for {gguf_path.name}: no corpus available "
            f"(set {CORPUS_ENV}, or provide {DEFAULT_CORPUS})"
        )
        return True

    threshold = resolve_threshold()
    log(
        f"PPL smoke test: {gguf_path.name} vs {corpus} "
        f"(threshold {threshold:.1f})...",
    )
    exit_code, output = run_llama_perplexity(Path(perplexity_bin), gguf_path, corpus)
    ppl = parse_ppl(output)
    ok, reason = smoke_verdict(exit_code, ppl, threshold)
    if ok:
        log(f"PPL smoke test PASSED: {gguf_path.name} -- {reason}")
    else:
        log(f"PPL smoke test FAILED: {gguf_path.name} -- {reason}")
        tail = "\n".join(output.strip().splitlines()[-15:])
        if tail:
            log(f"  llama-perplexity output (tail):\n{tail}")
    return ok
