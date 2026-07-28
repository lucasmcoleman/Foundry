"""Pure-Python coverage for core/ppl_smoke.py -- the shared post-generation
PPL smoke gate used by both _magicquant_entry.run() and _rocmfpx_entry.run().

Covers: PPL parsing (valid/absent/nan/inf), pass/fail verdict logic, env
skip/threshold/corpus resolution, binary discovery across the layouts both
entry modules hand it, and the top-level orchestration function's
skip/pass/fail branches (with run_llama_perplexity monkeypatched out -- no
real subprocess is ever spawned here, matching _rocmfpx_entry's own
no-real-checkout test discipline).
"""

import math
from pathlib import Path

import pytest

import ppl_smoke


# ── parse_ppl ────────────────────────────────────────────────────────────────

def test_parse_ppl_valid():
    out = "some preamble\n[1]4.5000,[2]5.1000,\nFinal estimate: PPL = 5.4321 +/- 0.1234\n"
    assert ppl_smoke.parse_ppl(out) == pytest.approx(5.4321)


def test_parse_ppl_absent():
    assert ppl_smoke.parse_ppl("no ppl line here at all") is None


def test_parse_ppl_nan():
    ppl = ppl_smoke.parse_ppl("Final estimate: PPL = nan\n")
    assert ppl is not None and math.isnan(ppl)


def test_parse_ppl_inf():
    ppl = ppl_smoke.parse_ppl("Final estimate: PPL = inf\n")
    assert ppl is not None and math.isinf(ppl)


def test_parse_ppl_unparseable_token_returns_none():
    assert ppl_smoke.parse_ppl("Final estimate: PPL = garbage\n") is None


# ── smoke_verdict ────────────────────────────────────────────────────────────

def test_smoke_verdict_ok():
    ok, reason = ppl_smoke.smoke_verdict(0, 5.4, threshold=100.0)
    assert ok is True
    assert "5.4" in reason


def test_smoke_verdict_high_ppl_fails():
    ok, reason = ppl_smoke.smoke_verdict(0, 248320.0, threshold=100.0)
    assert ok is False
    assert "exceeds threshold" in reason


def test_smoke_verdict_no_estimate_fails():
    ok, reason = ppl_smoke.smoke_verdict(0, None, threshold=100.0)
    assert ok is False
    assert "no 'Final estimate" in reason


def test_smoke_verdict_nonzero_exit_fails_even_with_good_ppl():
    ok, reason = ppl_smoke.smoke_verdict(1, 5.0, threshold=100.0)
    assert ok is False
    assert "exited 1" in reason


def test_smoke_verdict_nan_fails():
    ok, reason = ppl_smoke.smoke_verdict(0, float("nan"), threshold=100.0)
    assert ok is False
    assert "NaN/inf" in reason


def test_smoke_verdict_inf_fails():
    ok, reason = ppl_smoke.smoke_verdict(0, float("inf"), threshold=100.0)
    assert ok is False
    assert "NaN/inf" in reason


def test_smoke_verdict_ppl_exactly_at_threshold_passes():
    """Boundary: at-threshold is OK, only strictly-over fails."""
    ok, _ = ppl_smoke.smoke_verdict(0, 100.0, threshold=100.0)
    assert ok is True


def test_smoke_verdict_default_threshold_is_100():
    ok, _ = ppl_smoke.smoke_verdict(0, 99.9)
    assert ok is True
    ok, _ = ppl_smoke.smoke_verdict(0, 100.1)
    assert ok is False


# ── env resolution: is_skipped / resolve_corpus / resolve_threshold ─────────

def test_is_skipped_default_false():
    assert ppl_smoke.is_skipped({}) is False


def test_is_skipped_true_when_env_set():
    assert ppl_smoke.is_skipped({"FOUNDRY_SKIP_SMOKE_PPL": "1"}) is True


def test_is_skipped_false_for_other_values():
    assert ppl_smoke.is_skipped({"FOUNDRY_SKIP_SMOKE_PPL": "0"}) is False
    assert ppl_smoke.is_skipped({"FOUNDRY_SKIP_SMOKE_PPL": "true"}) is False


def test_resolve_corpus_env_wins(monkeypatch):
    assert ppl_smoke.resolve_corpus({"FOUNDRY_SMOKE_CORPUS": "/some/corpus.raw"}) == "/some/corpus.raw"


def test_resolve_corpus_falls_back_to_default_when_it_exists(monkeypatch, tmp_path):
    fake_default = tmp_path / "wiki.test.raw"
    fake_default.write_text("x")
    monkeypatch.setattr(ppl_smoke, "DEFAULT_CORPUS", str(fake_default))
    assert ppl_smoke.resolve_corpus({}) == str(fake_default)


def test_resolve_corpus_none_when_nothing_available(monkeypatch, tmp_path):
    monkeypatch.setattr(ppl_smoke, "DEFAULT_CORPUS", str(tmp_path / "nonexistent.raw"))
    assert ppl_smoke.resolve_corpus({}) is None


def test_resolve_threshold_default():
    assert ppl_smoke.resolve_threshold({}) == ppl_smoke.DEFAULT_PPL_MAX


def test_resolve_threshold_env_override():
    assert ppl_smoke.resolve_threshold({"FOUNDRY_SMOKE_PPL_MAX": "42.5"}) == 42.5


def test_resolve_threshold_ignores_unparseable_override():
    assert ppl_smoke.resolve_threshold({"FOUNDRY_SMOKE_PPL_MAX": "not-a-number"}) == ppl_smoke.DEFAULT_PPL_MAX


# ── find_perplexity_bin ──────────────────────────────────────────────────────

def test_find_perplexity_bin_direct(tmp_path):
    (tmp_path / "llama-perplexity").write_text("")
    assert ppl_smoke.find_perplexity_bin(str(tmp_path)) == tmp_path / "llama-perplexity"


def test_find_perplexity_bin_in_bin_subdir(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "llama-perplexity").write_text("")
    assert ppl_smoke.find_perplexity_bin(str(tmp_path)) == bin_dir / "llama-perplexity"


def test_find_perplexity_bin_in_build_bin_subdir(tmp_path):
    bin_dir = tmp_path / "build" / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "llama-perplexity").write_text("")
    assert ppl_smoke.find_perplexity_bin(str(tmp_path)) == bin_dir / "llama-perplexity"


def test_find_perplexity_bin_rocmfpx_layout(tmp_path):
    """ROCmFPX's fixed build dir name, matching _rocmfpx_entry's own
    ``Path(rocmfpx_dir) / "build-strix-rocmfp4" / "bin" / "llama-quantize"``
    sibling layout."""
    bin_dir = tmp_path / "build-strix-rocmfp4" / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "llama-perplexity").write_text("")
    assert ppl_smoke.find_perplexity_bin(str(tmp_path)) == bin_dir / "llama-perplexity"


def test_find_perplexity_bin_walks_up_to_parent(tmp_path):
    """A hint pointing at a build subdir (like _magicquant_entry.find_llamacpp
    can return) must still resolve a binary that lives in a sibling bin/ one
    level up."""
    root = tmp_path / "llama.cpp"
    build = root / "build-custom"
    build.mkdir(parents=True)
    bin_dir = root / "bin"
    bin_dir.mkdir()
    (bin_dir / "llama-perplexity").write_text("")
    assert ppl_smoke.find_perplexity_bin(str(build)) == bin_dir / "llama-perplexity"


def test_find_perplexity_bin_none_when_missing(tmp_path):
    assert ppl_smoke.find_perplexity_bin(str(tmp_path / "nope")) is None


def test_find_perplexity_bin_none_for_empty_base_dir():
    assert ppl_smoke.find_perplexity_bin("") is None
    assert ppl_smoke.find_perplexity_bin(None) is None


# ── smoke_test_gguf: top-level orchestration (subprocess mocked out) ────────

def test_smoke_test_gguf_skipped_by_env(monkeypatch, tmp_path):
    monkeypatch.setenv("FOUNDRY_SKIP_SMOKE_PPL", "1")
    gguf = tmp_path / "model.gguf"
    gguf.write_bytes(b"x")
    logs = []
    ok = ppl_smoke.smoke_test_gguf(Path("/some/bin"), gguf, log=logs.append)
    assert ok is True
    assert any("SKIPPED" in m for m in logs)


def test_smoke_test_gguf_skipped_when_binary_missing(monkeypatch, tmp_path):
    monkeypatch.delenv("FOUNDRY_SKIP_SMOKE_PPL", raising=False)
    gguf = tmp_path / "model.gguf"
    gguf.write_bytes(b"x")
    logs = []
    ok = ppl_smoke.smoke_test_gguf(None, gguf, log=logs.append)
    assert ok is True
    assert any("binary not found" in m for m in logs)


def test_smoke_test_gguf_skipped_when_no_corpus(monkeypatch, tmp_path):
    monkeypatch.delenv("FOUNDRY_SKIP_SMOKE_PPL", raising=False)
    perplexity_bin = tmp_path / "llama-perplexity"
    perplexity_bin.write_text("")
    gguf = tmp_path / "model.gguf"
    gguf.write_bytes(b"x")
    monkeypatch.setattr(ppl_smoke, "resolve_corpus", lambda *a, **k: None)
    logs = []
    ok = ppl_smoke.smoke_test_gguf(perplexity_bin, gguf, log=logs.append)
    assert ok is True
    assert any("no corpus available" in m for m in logs)


def test_smoke_test_gguf_passes_on_healthy_ppl(monkeypatch, tmp_path):
    monkeypatch.delenv("FOUNDRY_SKIP_SMOKE_PPL", raising=False)
    perplexity_bin = tmp_path / "llama-perplexity"
    perplexity_bin.write_text("")
    gguf = tmp_path / "model.gguf"
    gguf.write_bytes(b"x")
    monkeypatch.setattr(ppl_smoke, "resolve_corpus", lambda *a, **k: "/fake/corpus.raw")
    monkeypatch.setattr(
        ppl_smoke, "run_llama_perplexity",
        lambda *a, **k: (0, "Final estimate: PPL = 6.789 +/- 0.01\n"),
    )
    logs = []
    ok = ppl_smoke.smoke_test_gguf(perplexity_bin, gguf, log=logs.append)
    assert ok is True
    assert any("PASSED" in m for m in logs)


def test_smoke_test_gguf_fails_on_pathological_ppl(monkeypatch, tmp_path):
    monkeypatch.delenv("FOUNDRY_SKIP_SMOKE_PPL", raising=False)
    perplexity_bin = tmp_path / "llama-perplexity"
    perplexity_bin.write_text("")
    gguf = tmp_path / "model.gguf"
    gguf.write_bytes(b"x")
    monkeypatch.setattr(ppl_smoke, "resolve_corpus", lambda *a, **k: "/fake/corpus.raw")
    monkeypatch.setattr(
        ppl_smoke, "run_llama_perplexity",
        lambda *a, **k: (0, "Final estimate: PPL = 248320.0 +/- 500\n"),
    )
    logs = []
    ok = ppl_smoke.smoke_test_gguf(perplexity_bin, gguf, log=logs.append)
    assert ok is False
    assert any("FAILED" in m for m in logs)


def test_smoke_test_gguf_fails_on_nonzero_exit(monkeypatch, tmp_path):
    monkeypatch.delenv("FOUNDRY_SKIP_SMOKE_PPL", raising=False)
    perplexity_bin = tmp_path / "llama-perplexity"
    perplexity_bin.write_text("")
    gguf = tmp_path / "model.gguf"
    gguf.write_bytes(b"x")
    monkeypatch.setattr(ppl_smoke, "resolve_corpus", lambda *a, **k: "/fake/corpus.raw")
    monkeypatch.setattr(ppl_smoke, "run_llama_perplexity", lambda *a, **k: (1, "segfault"))
    ok = ppl_smoke.smoke_test_gguf(perplexity_bin, gguf, log=lambda m: None)
    assert ok is False


def test_smoke_test_gguf_default_log_uses_print(tmp_path, capsys):
    """No log= passed -- default logger must actually print (flush=True, per
    house convention for streamed-subprocess logs)."""
    gguf = tmp_path / "model.gguf"
    gguf.write_bytes(b"x")
    ppl_smoke.smoke_test_gguf(None, gguf)
    out = capsys.readouterr().out
    assert "SKIPPED" in out
