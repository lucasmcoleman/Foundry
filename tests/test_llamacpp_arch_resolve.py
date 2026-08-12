"""Field report Issue B, Improvement 3: arch-aware resolve fall-through.

find_llamacpp's candidate order (hint, LLAMACPP_PATH, ROCmFPX fork builds,
stock ~/llama.cpp, ./llama.cpp, /usr/local) deliberately prefers the
ROCmFPX fork -- whose own comment already admits "a brand-new arch may load
there first [on stock] -- override with the explicit hint" (see
core/_magicquant_entry.py). A muse-glimmer run once hit exactly that: the
default sent it to a fork build that could not load the arch, and it died
40 minutes into baseline measurement.

MagicQuant's own fail-fast (magicquant.utils.llamacpp.LlamaBinaryArchError
/ orchestrator._run_arch_support_check, master commit 22a17e0) now catches
this before any measurement subprocess runs -- but only AFTER Foundry has
already committed to a build. This is the Foundry-side improvement layered
on top: find_llamacpp, when it knows the source GGUF path, SKIPS a
candidate whose resolved llama-perplexity binary DEFINITIVELY (a real
``False`` verdict from magicquant.utils.llamacpp.binary_supports_arch) does
not support that architecture, steering toward a build that might actually
work instead of just failing fast on the wrong one. It never duplicates
MagicQuant's hard failure itself -- if every candidate is definitively
incompatible, it still returns the first one, loudly, and lets MagicQuant's
own fail-fast name the error.

These tests use REAL magicquant.utils.llamacpp.binary_supports_arch /
resolve_source_gguf_arch (the actual contracts Foundry imports, not a
reimplementation) against tiny synthetic files -- same technique as
MagicQuant's own tests/test_llamacpp_arch_check.py.
"""
import json
import struct
import sys
import types
from pathlib import Path

import pytest

pytest.importorskip("magicquant.utils.llamacpp")

import _magicquant_entry as entry  # noqa: E402  (core is on sys.path via conftest)

ROOT = Path(__file__).resolve().parent.parent
ARCH = "muse-glimmer"


def _write_gguf_stub(path, arch=None):
    """Minimal on-disk GGUF: magic + version + 0 tensors + an optional
    general.architecture STRING key -- exactly what
    magicquant.gguf.reader.GGUFReader (resolve_source_gguf_arch's reader)
    needs, without depending on the optional `gguf` package. Mirrors
    MagicQuant's own tests/test_llamacpp_arch_check.py::_write_gguf_stub."""
    def _string(s: str) -> bytes:
        b = s.encode("utf-8")
        return struct.pack("<Q", len(b)) + b

    buf = bytearray()
    buf += struct.pack("<I", 0x46554747)  # "GGUF" magic
    buf += struct.pack("<I", 3)           # version
    buf += struct.pack("<Q", 0)           # tensor_count
    if arch is not None:
        buf += struct.pack("<Q", 1)       # metadata_key_count
        buf += _string("general.architecture")
        buf += struct.pack("<I", 8)       # GGUF STRING type
        buf += _string(arch)
    else:
        buf += struct.pack("<Q", 0)
    Path(path).write_bytes(bytes(buf))


def _make_llamacpp_dir(base: Path, *, perplexity_contains: bytes | None = None) -> Path:
    """A fake llama.cpp build dir: bin/llama-quantize (so find_llamacpp's
    own "is this a llama.cpp dir" check passes) and, unless
    perplexity_contains is None, bin/llama-perplexity carrying the given
    bytes -- fed straight to binary_supports_arch's real byte scan.
    perplexity_contains=None omits the perplexity binary entirely, forcing
    an undeterminable (None) verdict."""
    (base / "bin").mkdir(parents=True, exist_ok=True)
    (base / "bin" / "llama-quantize").write_bytes(b"")
    if perplexity_contains is not None:
        (base / "bin" / "llama-perplexity").write_bytes(perplexity_contains)
    return base


# A binary that plainly lacks the arch and does not look dynamically linked
# (no "libllama" bytes) -- binary_supports_arch's real, documented case for
# a trustworthy False verdict (see magicquant/utils/llamacpp.py).
_NO_ARCH_STATIC_LOOKING = b"totally unrelated static binary content, no arch table" * 4
_HAS_ARCH_STATIC_LOOKING = b"static build, arch table entry: " + ARCH.encode()


# ── find_llamacpp: arch-aware candidate filtering ────────────────────────────


def test_skips_candidate_with_definitive_false_verdict_and_tries_next(tmp_path, monkeypatch, capsys):
    gguf = tmp_path / "source.gguf"
    _write_gguf_stub(gguf, arch=ARCH)

    bad = tmp_path / "bad-fork"
    _make_llamacpp_dir(bad, perplexity_contains=_NO_ARCH_STATIC_LOOKING)
    good = tmp_path / "home" / "llama.cpp"
    _make_llamacpp_dir(good, perplexity_contains=_HAS_ARCH_STATIC_LOOKING)

    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path / "home"))
    monkeypatch.setenv("LLAMACPP_PATH", str(bad))

    result = entry.find_llamacpp("", source_gguf_path=str(gguf))

    assert result == str(good)
    out = capsys.readouterr().out
    assert str(bad) in out
    assert "trying next candidate" in out


def test_none_verdict_does_not_skip(tmp_path, monkeypatch, capsys):
    """A candidate whose arch support is undeterminable (no perplexity
    binary reachable at all) must be accepted immediately -- never treated
    as a skip -- even when a LATER, arch-compatible candidate exists.

    Reviewer-caught non-discriminating-test fix: with only a single
    candidate on disk, a `verdict is False` -> `not verdict` mutant (None
    ALSO skips -- the exact named regression) still passed here, because
    with nothing left to try, the loop's own "every candidate was
    definitively False" fallback returns that same lone candidate anyway --
    the two code paths converge on one candidate, so the test couldn't
    tell them apart. Adding a second, DEFINITELY-True candidate right after
    the None one makes them diverge: correct code returns the FIRST (None)
    candidate without ever logging a skip; the mutant skips it, logs
    "trying next candidate", and returns the second (True) one instead --
    both the return value AND the absence of that log line are asserted so
    either half of the mutant's wrong behavior is caught on its own."""
    gguf = tmp_path / "source.gguf"
    _write_gguf_stub(gguf, arch=ARCH)

    undeterminable = tmp_path / "undeterminable-candidate"
    _make_llamacpp_dir(undeterminable, perplexity_contains=None)
    later_true = tmp_path / "home" / "llama.cpp"
    _make_llamacpp_dir(later_true, perplexity_contains=_HAS_ARCH_STATIC_LOOKING)

    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path / "home"))
    monkeypatch.setenv("LLAMACPP_PATH", str(undeterminable))

    result = entry.find_llamacpp("", source_gguf_path=str(gguf))

    assert result == str(undeterminable)
    assert "trying next candidate" not in capsys.readouterr().out


def test_hint_never_skipped_even_on_definitive_false_verdict(tmp_path, monkeypatch, capsys):
    """User authority: an explicit hint is arch-checked only to warn, never
    to be skipped in favor of a different (even arch-compatible) candidate."""
    gguf = tmp_path / "source.gguf"
    _write_gguf_stub(gguf, arch=ARCH)

    hinted = tmp_path / "user-hinted-fork"
    _make_llamacpp_dir(hinted, perplexity_contains=_NO_ARCH_STATIC_LOOKING)
    good = tmp_path / "home" / "llama.cpp"
    _make_llamacpp_dir(good, perplexity_contains=_HAS_ARCH_STATIC_LOOKING)

    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path / "home"))
    monkeypatch.delenv("LLAMACPP_PATH", raising=False)

    result = entry.find_llamacpp(str(hinted), source_gguf_path=str(gguf))

    assert result == str(hinted)
    out = capsys.readouterr().out
    assert "honoring the explicit hint anyway" in out
    assert str(hinted) in out


def test_all_candidates_false_returns_first_found_with_loud_warning(tmp_path, monkeypatch, capsys):
    gguf = tmp_path / "source.gguf"
    _write_gguf_stub(gguf, arch=ARCH)

    first = tmp_path / "env-candidate"
    _make_llamacpp_dir(first, perplexity_contains=_NO_ARCH_STATIC_LOOKING)
    second = tmp_path / "home" / "llama.cpp"
    _make_llamacpp_dir(second, perplexity_contains=b"also no arch match here, static-looking" * 4)

    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path / "home"))
    monkeypatch.setenv("LLAMACPP_PATH", str(first))

    result = entry.find_llamacpp("", source_gguf_path=str(gguf))

    assert result == str(first)  # first FOUND candidate, returned anyway
    out = capsys.readouterr().out
    assert "no candidate llama.cpp build definitively supports" in out
    assert str(first) in out


def test_no_source_gguf_path_is_byte_identical_to_arch_agnostic_behavior(tmp_path, monkeypatch):
    """Omitting source_gguf_path (its default) must reproduce the original
    arch-agnostic resolution -- no candidate is ever scanned or skipped."""
    only = tmp_path / "the-only-candidate"
    _make_llamacpp_dir(only, perplexity_contains=_NO_ARCH_STATIC_LOOKING)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path / "home-empty"))
    monkeypatch.setenv("LLAMACPP_PATH", str(only))

    assert entry.find_llamacpp("") == str(only)


def test_unreadable_source_disables_arch_filtering_entirely(tmp_path, monkeypatch):
    """resolve_source_gguf_arch returns None for a non-GGUF / archless file
    -- find_llamacpp must then behave exactly as if source_gguf_path had
    never been given (no scanning, no skip logic engaged at all)."""
    not_a_gguf = tmp_path / "model.safetensors"
    not_a_gguf.write_bytes(b"not a real gguf")

    cand = tmp_path / "only"
    _make_llamacpp_dir(cand, perplexity_contains=b"never scanned, irrelevant content")

    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path / "home-empty"))
    monkeypatch.setenv("LLAMACPP_PATH", str(cand))

    assert entry.find_llamacpp("", source_gguf_path=str(not_a_gguf)) == str(cand)


# ── run(): the post-conversion re-resolve hook ───────────────────────────────


def _base_run_cfg(tmp_path, src_dir):
    return {
        "pipeline_root": str(ROOT), "pipeline_root_str": str(ROOT),
        "llamacpp_hint": "", "mq_source_override": str(src_dir),
        "out_abs_str": str(tmp_path / "out"),
        "generations": 5, "population_size": 10, "target_base_quant": "MXFP4_MOE",
        "tiers_json": json.dumps(["Q4"]), "model_name": "m", "verify": False,
        "measured": False, "measurement_rounds": 3, "rocmfpx_schemes": False,
        "iq_schemes": False, "seed": None, "use_imatrix": False,
        "imatrix_corpus": None, "enable_kl": False, "kl_weight": 0.1,
        "enable_speed_bench": False, "measurement_chunks": None,
        "stream_aware": False, "head_aggressive": False, "speed_aware": None,
        "speed_metric": "bytes", "speed_weight": None, "use_bytes_tps": False,
        "calibration_source": "", "write_calibration": False,
    }


def test_run_re_resolves_llamacpp_after_bf16_conversion_for_arch_compat(
    tmp_path, monkeypatch,
):
    """End-to-end guard for the run() hook placement: once a directory
    source is converted to a BF16 GGUF, run() must re-resolve llamacpp
    with the now-known source_gguf_path, and use whatever THAT call
    returns -- not the original arch-unaware resolution -- for the
    orchestrator. This is the "handle both orders" case: the GGUF path
    isn't known at the first (ensure_llamacpp) call, only after
    _ensure_bf16_gguf runs."""
    calls = []

    def fake_find_llamacpp(hint="", *, source_gguf_path=None):
        calls.append(source_gguf_path)
        return "/first/llamacpp" if source_gguf_path is None else "/second/arch-compatible/llamacpp"

    monkeypatch.setattr(entry, "find_llamacpp", fake_find_llamacpp)

    src_dir = tmp_path / "merged_model"
    src_dir.mkdir()
    (src_dir / "model.safetensors").write_bytes(b"x")

    converted = tmp_path / "model-bf16.gguf"
    converted.write_bytes(b"GGUF-stub")

    monkeypatch.setattr(
        entry, "_ensure_bf16_gguf",
        lambda llamacpp_dir, source, out_dir, model_name=None: str(converted),
    )

    captured_orch_kwargs = {}

    class _FakeOrch:
        def __init__(self, **kw):
            captured_orch_kwargs.update(kw)

        def run_full_search(self, **kw):
            return [], {"Q4": {"config": {}}}

        def generate_tiered_models(self, tiered, model_name_prefix, tiers, verify):
            out = tmp_path / "model-Q4.gguf"
            out.write_bytes(b"x")
            return [str(out)]

    fake_pkg = types.ModuleType("magicquant")
    fake_orch_mod = types.ModuleType("magicquant.orchestrator")
    fake_orch_mod.MagicQuantOrchestrator = _FakeOrch
    monkeypatch.setitem(sys.modules, "magicquant", fake_pkg)
    monkeypatch.setitem(sys.modules, "magicquant.orchestrator", fake_orch_mod)

    import ppl_smoke
    monkeypatch.setattr(ppl_smoke, "find_perplexity_bin", lambda *a, **k: None)
    monkeypatch.setattr(ppl_smoke, "smoke_test_gguf", lambda *a, **k: True)

    cfg_path = tmp_path / "cfg.json"
    cfg_path.write_text(json.dumps(_base_run_cfg(tmp_path, src_dir)))

    entry.run(str(cfg_path))

    assert calls[0] is None                    # first call: arch-unaware (ensure_llamacpp)
    assert calls[-1] == str(converted)          # second call: arch-aware, post-conversion
    assert captured_orch_kwargs["llamacpp_path"] == "/second/arch-compatible/llamacpp"


def test_run_skips_re_resolve_when_source_never_becomes_a_gguf(tmp_path, monkeypatch):
    """When llama.cpp is unavailable (or conversion never happens), source
    stays a directory and the re-resolve hook must be a no-op -- find_llamacpp
    is called exactly once (ensure_llamacpp's arch-unaware call), never twice."""
    calls = []

    def fake_find_llamacpp(hint="", *, source_gguf_path=None):
        calls.append(source_gguf_path)
        return None  # llama.cpp unavailable

    monkeypatch.setattr(entry, "find_llamacpp", fake_find_llamacpp)

    src_dir = tmp_path / "merged_model"
    src_dir.mkdir()
    (src_dir / "model.safetensors").write_bytes(b"x")

    class _FakeOrch:
        def __init__(self, **kw):
            pass

        def run_full_search(self, **kw):
            return [], {"Q4": {"config": {}}}

        def generate_tiered_models(self, tiered, model_name_prefix, tiers, verify):
            out = tmp_path / "model-Q4.gguf"
            out.write_bytes(b"x")
            return [str(out)]

    fake_pkg = types.ModuleType("magicquant")
    fake_orch_mod = types.ModuleType("magicquant.orchestrator")
    fake_orch_mod.MagicQuantOrchestrator = _FakeOrch
    monkeypatch.setitem(sys.modules, "magicquant", fake_pkg)
    monkeypatch.setitem(sys.modules, "magicquant.orchestrator", fake_orch_mod)

    import ppl_smoke
    monkeypatch.setattr(ppl_smoke, "find_perplexity_bin", lambda *a, **k: None)
    monkeypatch.setattr(ppl_smoke, "smoke_test_gguf", lambda *a, **k: True)

    cfg_path = tmp_path / "cfg.json"
    cfg_path.write_text(json.dumps(_base_run_cfg(tmp_path, src_dir)))

    entry.run(str(cfg_path))

    assert calls == [None]
