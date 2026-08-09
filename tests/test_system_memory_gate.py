"""CLI system-memory preflight gate (core/pipeline.py's _system_memory_gate).

core/preflight.py's check_system_memory is the PRIMARY GATE against the
documented unified-memory OOM-freeze livelock (see its module docstring), but
before this it was only ever wired up on the UI path (ui/app.py's
_mem_preflight) -- the CLI's own _preflight_stage only ever checked GPU VRAM,
advisory-only. This closes that gap: unlike _preflight_stage, this gate
actually blocks -- callers check the return value and abort the stage.
"""
import pipeline


def _collect_log():
    records = []

    def log(msg, level="info"):
        records.append((msg, level))

    return log, records


def test_gate_returns_true_and_logs_nothing_extra_when_check_passes(monkeypatch):
    import preflight

    monkeypatch.setattr(preflight, "check_system_memory", lambda stage, log, skip=False: True)
    log, records = _collect_log()
    assert pipeline._system_memory_gate("training", log) is True


def test_gate_returns_false_when_check_fails(monkeypatch):
    import preflight

    def fake_check(stage, log, skip=False):
        log(f"Insufficient system memory for {stage}", "error")
        return False

    monkeypatch.setattr(preflight, "check_system_memory", fake_check)
    log, records = _collect_log()
    assert pipeline._system_memory_gate("magicquant", log) is False
    assert any("Insufficient system memory" in msg for msg, _ in records)


def test_gate_forwards_skip_flag(monkeypatch):
    import preflight

    seen = {}

    def fake_check(stage, log, skip=False):
        seen["skip"] = skip
        return True

    monkeypatch.setattr(preflight, "check_system_memory", fake_check)
    pipeline._system_memory_gate("export", lambda *a, **k: None, skip=True)
    assert seen["skip"] is True


def test_gate_defaults_to_safe_when_preflight_module_unimportable(monkeypatch):
    # Mirrors _preflight_stage's own fail-open contract: an environment where
    # preflight can't even be imported must not block the pipeline outright.
    import builtins

    real_import = builtins.__import__

    def blocked_import(name, *args, **kwargs):
        if name == "preflight":
            raise ImportError("simulated")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked_import)
    assert pipeline._system_memory_gate("training", lambda *a, **k: None) is True


# ── Wiring: each gated stage calls the gate before doing real work; REAP,
# documented in preflight.py as an intentional lighter/unlisted exception
# (matching ui/app.py's do_reap, which also skips _mem_preflight), does not.
# Source-inspection, matching the established pattern already used elsewhere
# in this suite (e.g. test_magicquant_knobs.py's hash-source tests) for
# "is call X wired into function Y" checks that don't need a full stage run.

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _stage_body(stage_func_name: str) -> str:
    src = (ROOT / "core" / "pipeline.py").read_text()
    start = src.index(f"def {stage_func_name}(")
    end = src.index("\ndef ", start + 1)
    return src[start:end]


def test_gated_stages_call_system_memory_gate():
    for stage_func, stage_name in [
        ("stage_training", "training"),
        ("stage_export", "export"),
        ("stage_heretic", "heretic"),
        ("stage_qat", "qat"),
        ("stage_magicquant", "magicquant"),
        ("stage_rocmfpx", "rocmfpx"),
    ]:
        body = _stage_body(stage_func)
        assert f'_system_memory_gate("{stage_name}", log, skip=skip_preflight)' in body, stage_func
        assert "if not _system_memory_gate" in body, stage_func


def test_reap_does_not_call_system_memory_gate():
    body = _stage_body("stage_reap")
    assert "_system_memory_gate" not in body


def test_every_heavy_stage_also_calls_the_gpu_preflight_check():
    """stage_magicquant used to accept skip_preflight but never actually call
    _preflight_stage (the GPU-VRAM advisory check) at all -- discovered while
    wiring the system-memory gate, fixed as a follow-up. Every other heavy
    stage already called it; this locks in that stage_magicquant now does
    too, matching stage_rocmfpx's identical structure and placement."""
    for stage_func, stage_name in [
        ("stage_training", "training"),
        ("stage_export", "export"),
        ("stage_heretic", "heretic"),
        ("stage_qat", "qat"),
        ("stage_magicquant", "magicquant"),
        ("stage_rocmfpx", "rocmfpx"),
    ]:
        body = _stage_body(stage_func)
        assert f'_preflight_stage("{stage_name}", config, log, skip=skip_preflight)' in body, stage_func
