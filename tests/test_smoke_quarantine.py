"""Regression coverage for Foundry #2: a GGUF that fails the post-generation
PPL smoke gate (core/ppl_smoke.smoke_test_gguf) must be quarantined out of
hf_upload.discover_upload_files's reach, not merely logged and left in place.

Before this change: _rocmfpx_entry.run() / _magicquant_entry.run() computed
`failed = [...]` from the smoke gate and called sys.exit(1) -- the file itself
was never touched. discover_upload_files globs rocmfpx/*.gguf and
magicquant/*.gguf with no smoke-status filter, so a later upload-only run
(``enabled_stages: ["upload"]`` alone) could publish the pathological file.

Pure quarantine-mechanism tests (rename + sidecar, advisory failure handling)
live in tests/test_ppl_smoke.py, next to the rest of ppl_smoke.py's coverage.
This file covers the wiring: both entry modules' smoke-gate call sites
(mirrored per docs/decisions/rocmfpx-stage-failure-handling.md's framing --
"quarantine and continue, never ship it anyway") and the
discover_upload_files / plan_gguf_repos integration a quarantined file must
not be visible to.
"""

import json
import sys
import types
from pathlib import Path

import pytest

import _magicquant_entry
import _rocmfpx_entry
import ppl_smoke
from hf_upload import discover_upload_files, plan_gguf_repos

ROOT = Path(__file__).resolve().parent.parent


# ── hf_upload integration: AC1 (not matched by discover_upload_files) and
#    AC3 (a subsequent upload-only run publishes nothing from it) ───────────

def test_quarantined_rocmfpx_file_not_discovered_for_upload(tmp_path):
    out_dir = tmp_path / "out"
    fpx_dir = out_dir / "rocmfpx"
    fpx_dir.mkdir(parents=True)
    good = fpx_dir / "model-rocmfp4-agent.gguf"
    good.write_bytes(b"good")
    bad = fpx_dir / "model-rocmfp6-agent.gguf"
    bad.write_bytes(b"bad")

    ppl_smoke.quarantine_gguf(bad, "PPL 999.00 exceeds threshold 100.0")

    files = discover_upload_files(str(out_dir), gguf_family="rocmfpx")
    names = [repo_path for _, repo_path in files]
    assert "model-rocmfp4-agent.gguf" in names
    assert "model-rocmfp6-agent.gguf" not in names
    assert not any(n.endswith(".failed-smoke") for n in names)


def test_quarantined_magicquant_file_not_discovered_for_upload(tmp_path):
    out_dir = tmp_path / "out"
    mq_dir = out_dir / "magicquant"
    mq_dir.mkdir(parents=True)
    bad = mq_dir / "model-Q4.gguf"
    bad.write_bytes(b"bad")

    ppl_smoke.quarantine_gguf(bad, "PPL 999.00 exceeds threshold 100.0")

    # Simulates the "subsequent upload-only run" scenario from AC3: a fresh
    # discover_upload_files call against the same output dir, as an
    # enabled_stages: ["upload"]-only resume would make.
    files = discover_upload_files(str(out_dir), gguf_family="magicquant")
    assert files == []


def test_quarantined_only_rocmfpx_dir_degrades_like_empty_for_repo_planning(tmp_path):
    """plan_gguf_repos's has_fpx check globs rocmfpx/*.gguf too -- a directory
    holding only a quarantined file must plan the single-repo split, exactly
    as an empty rocmfpx/ directory does today (pins the degradation property
    docs/decisions/rocmfpx-stage-failure-handling.md's Option D relies on)."""
    out_dir = tmp_path / "out"
    mq_dir = out_dir / "magicquant"
    mq_dir.mkdir(parents=True)
    (mq_dir / "model-Q4.gguf").write_bytes(b"good")
    fpx_dir = out_dir / "rocmfpx"
    fpx_dir.mkdir(parents=True)
    bad = fpx_dir / "model-rocmfp6-agent.gguf"
    bad.write_bytes(b"bad")
    ppl_smoke.quarantine_gguf(bad, "some reason")

    plan = plan_gguf_repos(str(out_dir), "user/model-GGUF")
    assert plan == [("user/model-GGUF", "auto")]


# ── _rocmfpx_entry.run(): end-to-end smoke-gate + quarantine wiring ────────

def _rocmfpx_cfg(tmp_path, out_dir, formats):
    cfg_path = tmp_path / "rocmfpx_cfg.json"
    cfg_path.write_text(json.dumps({
        "pipeline_root": str(ROOT),
        "pipeline_root_str": str(ROOT),
        "out_abs_str": str(out_dir),
        "formats_json": json.dumps(formats),
        "model_name": "stub-model",
    }))
    return cfg_path


def _wire_rocmfpx_offline(monkeypatch, tmp_path, produced_path):
    """Minimal offline wiring for _rocmfpx_entry.run(): no real ROCmFPX
    checkout, no real llama-quantize invocation -- _quantize_preset is
    replaced with a stub that just returns the pre-placed GGUF path."""
    monkeypatch.setattr(_rocmfpx_entry, "ensure_rocmfpx", lambda hint="": str(tmp_path))
    monkeypatch.setattr(_rocmfpx_entry, "resolve_source",
                         lambda *a, **kw: str(tmp_path / "model.gguf"))
    monkeypatch.setattr(_rocmfpx_entry, "_ensure_bf16_gguf", lambda *a, **kw: "stub.gguf")
    monkeypatch.setattr(_rocmfpx_entry, "_quantize_preset",
                         lambda *a, **kw: produced_path)


def _wire_real_smoke_gate_failing(monkeypatch, tmp_path, module):
    """Point the smoke gate at a fake-but-existing perplexity binary and a
    fake corpus, then make the (mocked) subprocess report a pathological PPL
    -- exercises the REAL smoke_test_gguf/quarantine_gguf code path (not a
    monkeypatched bool), so the captured reason is the genuine one."""
    fake_bin = tmp_path / "llama-perplexity"
    fake_bin.write_text("#!/bin/sh\n")
    monkeypatch.setattr(module.ppl_smoke, "find_perplexity_bin", lambda *a, **kw: fake_bin)
    monkeypatch.setattr(module.ppl_smoke, "resolve_corpus",
                         lambda *a, **kw: str(tmp_path / "corpus.raw"))
    monkeypatch.setattr(
        module.ppl_smoke, "run_llama_perplexity",
        lambda *a, **kw: (0, "Final estimate: PPL = 999999.00 +/- 1.0\n"),
    )


def _wire_real_smoke_gate_passing(monkeypatch, tmp_path, module):
    fake_bin = tmp_path / "llama-perplexity"
    fake_bin.write_text("#!/bin/sh\n")
    monkeypatch.setattr(module.ppl_smoke, "find_perplexity_bin", lambda *a, **kw: fake_bin)
    monkeypatch.setattr(module.ppl_smoke, "resolve_corpus",
                         lambda *a, **kw: str(tmp_path / "corpus.raw"))
    monkeypatch.setattr(
        module.ppl_smoke, "run_llama_perplexity",
        lambda *a, **kw: (0, "Final estimate: PPL = 5.43 +/- 0.1\n"),
    )


def test_rocmfpx_run_quarantines_smoke_failed_file_and_still_aborts(
    tmp_path, monkeypatch, capsys,
):
    out_dir = tmp_path / "out"
    fpx_dir = out_dir / "rocmfpx"
    fpx_dir.mkdir(parents=True)
    produced = fpx_dir / "model-rocmfp4-agent.gguf"
    produced.write_bytes(b"pathological gguf bytes")

    _wire_rocmfpx_offline(monkeypatch, tmp_path, produced)
    _wire_real_smoke_gate_failing(monkeypatch, tmp_path, _rocmfpx_entry)

    cfg_path = _rocmfpx_cfg(tmp_path, out_dir, ["rocmfp4-agent"])

    with pytest.raises(SystemExit) as exc:
        _rocmfpx_entry.run(str(cfg_path))
    assert exc.value.code == 1

    # AC2: still on disk, at a discoverable location, reason recorded.
    quarantined = fpx_dir / "model-rocmfp4-agent.gguf.failed-smoke"
    assert quarantined.exists()
    assert not produced.exists()
    sidecar = fpx_dir / "model-rocmfp4-agent.gguf.failed-smoke.json"
    record = json.loads(sidecar.read_text())
    assert record["original_name"] == "model-rocmfp4-agent.gguf"
    assert "exceeds threshold" in record["reason"]
    assert "quarantined_at" in record

    out = capsys.readouterr().out
    assert "Quarantined smoke-failed GGUF" in out
    assert "PIPELINE_STAGE_COMPLETE" not in out

    # AC1/AC3: the run aborted (didn't reach upload), but prove the on-disk
    # state left behind is itself unpublishable by a later upload-only run.
    assert discover_upload_files(str(out_dir), gguf_family="rocmfpx") == []


def test_rocmfpx_run_passing_gguf_untouched(tmp_path, monkeypatch, capsys):
    """AC4: a passing GGUF pins today's behaviour -- no rename, no sidecar."""
    out_dir = tmp_path / "out"
    fpx_dir = out_dir / "rocmfpx"
    fpx_dir.mkdir(parents=True)
    produced = fpx_dir / "model-rocmfp4-agent.gguf"
    produced.write_bytes(b"healthy gguf bytes")

    _wire_rocmfpx_offline(monkeypatch, tmp_path, produced)
    _wire_real_smoke_gate_passing(monkeypatch, tmp_path, _rocmfpx_entry)

    cfg_path = _rocmfpx_cfg(tmp_path, out_dir, ["rocmfp4-agent"])
    _rocmfpx_entry.run(str(cfg_path))  # must not raise

    assert produced.exists()
    assert produced.read_bytes() == b"healthy gguf bytes"
    assert not (fpx_dir / "model-rocmfp4-agent.gguf.failed-smoke").exists()
    assert not (fpx_dir / "model-rocmfp4-agent.gguf.failed-smoke.json").exists()
    out = capsys.readouterr().out
    assert "PIPELINE_STAGE_COMPLETE=rocmfpx" in out
    assert discover_upload_files(str(out_dir), gguf_family="rocmfpx")[0][0] == produced


def test_rocmfpx_run_skip_env_override_unchanged(tmp_path, monkeypatch, capsys):
    """AC5: FOUNDRY_SKIP_SMOKE_PPL=1 bypasses the smoke test entirely (as
    before) -- nothing is quarantined, the file ships through untouched."""
    out_dir = tmp_path / "out"
    fpx_dir = out_dir / "rocmfpx"
    fpx_dir.mkdir(parents=True)
    produced = fpx_dir / "model-rocmfp4-agent.gguf"
    produced.write_bytes(b"unmeasured gguf bytes")

    _wire_rocmfpx_offline(monkeypatch, tmp_path, produced)
    # Even wire a failing gate underneath -- SKIP_ENV must short-circuit
    # before it's ever reached.
    _wire_real_smoke_gate_failing(monkeypatch, tmp_path, _rocmfpx_entry)
    monkeypatch.setenv("FOUNDRY_SKIP_SMOKE_PPL", "1")

    cfg_path = _rocmfpx_cfg(tmp_path, out_dir, ["rocmfp4-agent"])
    _rocmfpx_entry.run(str(cfg_path))  # must not raise

    assert produced.exists()
    assert not (fpx_dir / "model-rocmfp4-agent.gguf.failed-smoke").exists()
    out = capsys.readouterr().out
    assert "PPL smoke test SKIPPED" in out
    assert "PIPELINE_STAGE_COMPLETE=rocmfpx" in out


# ── _magicquant_entry.run(): end-to-end smoke-gate + quarantine wiring ─────

class _FakeMQOrchestrator:
    """Minimal signature-compatible stand-in -- only run_full_search and
    generate_tiered_models are exercised (measured=False, v1 tier-ladder
    path); see test_magicquant_knobs.py's _FakeOrchestrator for the
    fuller-featured version this mirrors."""

    def __init__(self, **kwargs):
        self.init_kwargs = kwargs

    def run_full_search(self, **kwargs):
        return [], {"Q4": {"config": {}}}

    def run_measured_search(self, **kwargs):
        return [], {"Q4": {"config": {}}}

    def generate_tiered_models(self, tiered, model_name_prefix, tiers, verify):
        return [str(_FakeMQOrchestrator._out_path)]


def _mq_cfg(tmp_path, src_file, out_dir):
    cfg = {
        "pipeline_root": str(ROOT),
        "pipeline_root_str": str(ROOT),
        "llamacpp_hint": "",
        "mq_source_override": str(src_file),
        "out_abs_str": str(out_dir),
        "generations": 5,
        "population_size": 10,
        "target_base_quant": "MXFP4_MOE",
        "tiers_json": json.dumps(["Q4"]),
        "model_name": "m",
        "verify": False,
        "measured": False,
        "measurement_rounds": 3,
        "rocmfpx_schemes": False,
        "iq_schemes": False,
        "seed": None,
        "use_imatrix": False,
        "imatrix_corpus": None,
        "enable_kl": False,
        "kl_weight": 0.1,
        "enable_speed_bench": False,
        "measurement_chunks": None,
        "stream_aware": False,
        "head_aggressive": False,
        "speed_aware": None,
        "speed_metric": "bytes",
        "speed_weight": None,
        "use_bytes_tps": False,
        "calibration_source": "",
        "write_calibration": False,
    }
    cfg_path = tmp_path / "mq_cfg.json"
    cfg_path.write_text(json.dumps(cfg))
    return cfg_path


def _wire_magicquant_offline(monkeypatch, tmp_path, produced_path):
    _FakeMQOrchestrator._out_path = produced_path
    fake_pkg = types.ModuleType("magicquant")
    fake_orch_mod = types.ModuleType("magicquant.orchestrator")
    fake_orch_mod.MagicQuantOrchestrator = _FakeMQOrchestrator
    monkeypatch.setitem(sys.modules, "magicquant", fake_pkg)
    monkeypatch.setitem(sys.modules, "magicquant.orchestrator", fake_orch_mod)
    monkeypatch.setattr(_magicquant_entry, "find_llamacpp", lambda hint="", **kw: "/fake/llamacpp")
    monkeypatch.setattr(
        _magicquant_entry, "_ensure_bf16_gguf",
        lambda llamacpp_dir, source, out_dir, model_name=None: source,
    )


def test_magicquant_run_quarantines_smoke_failed_file_and_still_aborts(
    tmp_path, monkeypatch, capsys,
):
    out_dir = tmp_path / "out"
    mq_dir = out_dir / "magicquant"
    mq_dir.mkdir(parents=True)
    produced = mq_dir / "model-Q4.gguf"
    produced.write_bytes(b"pathological gguf bytes")
    src_file = tmp_path / "src.safetensors"
    src_file.write_bytes(b"x")

    _wire_magicquant_offline(monkeypatch, tmp_path, produced)
    _wire_real_smoke_gate_failing(monkeypatch, tmp_path, _magicquant_entry)

    cfg_path = _mq_cfg(tmp_path, src_file, out_dir)

    with pytest.raises(SystemExit) as exc:
        _magicquant_entry.run(str(cfg_path))
    assert exc.value.code == 1

    quarantined = mq_dir / "model-Q4.gguf.failed-smoke"
    assert quarantined.exists()
    assert not produced.exists()
    sidecar = mq_dir / "model-Q4.gguf.failed-smoke.json"
    record = json.loads(sidecar.read_text())
    assert record["original_name"] == "model-Q4.gguf"
    assert "exceeds threshold" in record["reason"]

    out = capsys.readouterr().out
    assert "Quarantined smoke-failed GGUF" in out
    assert "PIPELINE_STAGE_COMPLETE" not in out
    assert discover_upload_files(str(out_dir), gguf_family="magicquant") == []


def test_magicquant_run_passing_gguf_untouched(tmp_path, monkeypatch, capsys):
    out_dir = tmp_path / "out"
    mq_dir = out_dir / "magicquant"
    mq_dir.mkdir(parents=True)
    produced = mq_dir / "model-Q4.gguf"
    produced.write_bytes(b"healthy gguf bytes")
    src_file = tmp_path / "src.safetensors"
    src_file.write_bytes(b"x")

    _wire_magicquant_offline(monkeypatch, tmp_path, produced)
    _wire_real_smoke_gate_passing(monkeypatch, tmp_path, _magicquant_entry)

    cfg_path = _mq_cfg(tmp_path, src_file, out_dir)
    _magicquant_entry.run(str(cfg_path))  # must not raise

    assert produced.exists()
    assert produced.read_bytes() == b"healthy gguf bytes"
    assert not (mq_dir / "model-Q4.gguf.failed-smoke").exists()
    out = capsys.readouterr().out
    assert "PIPELINE_STAGE_COMPLETE=magicquant" in out


def test_magicquant_run_skip_env_override_unchanged(tmp_path, monkeypatch, capsys):
    out_dir = tmp_path / "out"
    mq_dir = out_dir / "magicquant"
    mq_dir.mkdir(parents=True)
    produced = mq_dir / "model-Q4.gguf"
    produced.write_bytes(b"unmeasured gguf bytes")
    src_file = tmp_path / "src.safetensors"
    src_file.write_bytes(b"x")

    _wire_magicquant_offline(monkeypatch, tmp_path, produced)
    _wire_real_smoke_gate_failing(monkeypatch, tmp_path, _magicquant_entry)
    monkeypatch.setenv("FOUNDRY_SKIP_SMOKE_PPL", "1")

    cfg_path = _mq_cfg(tmp_path, src_file, out_dir)
    _magicquant_entry.run(str(cfg_path))  # must not raise

    assert produced.exists()
    assert not (mq_dir / "model-Q4.gguf.failed-smoke").exists()
    out = capsys.readouterr().out
    assert "PPL smoke test SKIPPED" in out
    assert "PIPELINE_STAGE_COMPLETE=magicquant" in out
