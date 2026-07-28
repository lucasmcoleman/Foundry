"""System-memory preflight gate (offline, monkeypatched).

Covers the MemAvailable-primary / GTT-informational-only design added after
the hard-freeze incident: a resident llama-swap model pinned ~21 GB of GTT,
a pipeline run launched on top of it, and the kernel OOM-killed bystander
processes. The gate must key off system MemAvailable and never block on GTT
usage alone (M-mem-preflight).
"""

import preflight


def _collect_log():
    """Return (log_fn, records) where records collects (msg, level) tuples."""
    records = []

    def log(msg, level="info"):
        records.append((msg, level))

    return log, records


# ── parse_meminfo_available_gb ──────────────────────────────────────────────

def test_parse_meminfo_valid_text():
    sample = (
        "MemTotal:       131000000 kB\n"
        "MemFree:         20000000 kB\n"
        "MemAvailable:    64000000 kB\n"
        "Buffers:          1000000 kB\n"
    )
    gb = preflight.parse_meminfo_available_gb(sample)
    assert gb is not None
    assert abs(gb - 65.536) < 0.01  # 64000000 * 1024 / 1e9


def test_parse_meminfo_missing_field():
    sample = "MemTotal:       131000000 kB\nMemFree:         20000000 kB\n"
    assert preflight.parse_meminfo_available_gb(sample) is None


def test_parse_meminfo_garbage():
    assert preflight.parse_meminfo_available_gb("not /proc/meminfo content at all") is None


# ── estimate_stage_system_gb ────────────────────────────────────────────────

def test_estimate_constants_sanity():
    # magicquant (probe/tier GGUF writes + llama-perplexity loading into GTT)
    # dominates; export (streaming merge) is light.
    assert preflight.estimate_stage_system_gb("magicquant") > preflight.estimate_stage_system_gb("export")
    assert preflight.estimate_stage_system_gb("magicquant") == 48.0
    assert preflight.estimate_stage_system_gb("export") == 12.0
    assert preflight.estimate_stage_system_gb("training") == 32.0
    assert preflight.estimate_stage_system_gb("heretic") == 24.0
    assert preflight.estimate_stage_system_gb("rocmfpx") == 16.0
    assert preflight.estimate_stage_system_gb("qat") == 32.0


def test_estimate_default_for_unknown_stage():
    assert preflight.estimate_stage_system_gb("some_new_stage") == 12.0


# ── check_system_memory ─────────────────────────────────────────────────────

def test_below_requirement_fails_with_error_log(monkeypatch):
    monkeypatch.delenv("FOUNDRY_MIN_AVAILABLE_GB", raising=False)
    monkeypatch.delenv("FOUNDRY_SKIP_MEM_PREFLIGHT", raising=False)
    monkeypatch.setattr(preflight, "get_mem_available_gb", lambda: 5.0)
    monkeypatch.setattr(preflight, "get_gtt_used_gb", lambda: None)
    log, records = _collect_log()
    ok = preflight.check_system_memory("magicquant", log=log)  # needs 48 GB
    assert ok is False
    assert any(level == "error" for _msg, level in records)
    # Error message names actual vs required and points at the overrides.
    msg = next(m for m, lvl in records if lvl == "error")
    assert "5.0" in msg or "5" in msg
    assert "48" in msg
    assert "FOUNDRY_MIN_AVAILABLE_GB" in msg
    assert "FOUNDRY_SKIP_MEM_PREFLIGHT" in msg


def test_above_requirement_passes(monkeypatch):
    monkeypatch.delenv("FOUNDRY_MIN_AVAILABLE_GB", raising=False)
    monkeypatch.delenv("FOUNDRY_SKIP_MEM_PREFLIGHT", raising=False)
    monkeypatch.setattr(preflight, "get_mem_available_gb", lambda: 80.0)
    monkeypatch.setattr(preflight, "get_gtt_used_gb", lambda: None)
    log, records = _collect_log()
    ok = preflight.check_system_memory("export", log=log)  # needs 12 GB
    assert ok is True
    assert not any(level == "error" for _msg, level in records)


def test_unknown_mem_available_proceeds_with_warning(monkeypatch):
    monkeypatch.delenv("FOUNDRY_MIN_AVAILABLE_GB", raising=False)
    monkeypatch.delenv("FOUNDRY_SKIP_MEM_PREFLIGHT", raising=False)
    monkeypatch.setattr(preflight, "get_mem_available_gb", lambda: None)
    monkeypatch.setattr(preflight, "get_gtt_used_gb", lambda: None)
    log, records = _collect_log()
    ok = preflight.check_system_memory("training", log=log)
    assert ok is True
    assert any(level == "warn" for _msg, level in records)
    assert not any(level == "error" for _msg, level in records)


def test_skip_param_bypasses(monkeypatch):
    monkeypatch.delenv("FOUNDRY_MIN_AVAILABLE_GB", raising=False)
    monkeypatch.delenv("FOUNDRY_SKIP_MEM_PREFLIGHT", raising=False)
    monkeypatch.setattr(preflight, "get_mem_available_gb", lambda: 0.1)
    monkeypatch.setattr(preflight, "get_gtt_used_gb", lambda: None)
    log, records = _collect_log()
    ok = preflight.check_system_memory("magicquant", log=log, skip=True)
    assert ok is True
    assert any(level == "warn" for _msg, level in records)


def test_env_skip_flag_bypasses(monkeypatch):
    monkeypatch.setenv("FOUNDRY_SKIP_MEM_PREFLIGHT", "1")
    monkeypatch.delenv("FOUNDRY_MIN_AVAILABLE_GB", raising=False)
    monkeypatch.setattr(preflight, "get_mem_available_gb", lambda: 0.1)
    monkeypatch.setattr(preflight, "get_gtt_used_gb", lambda: None)
    log, records = _collect_log()
    ok = preflight.check_system_memory("magicquant", log=log)
    assert ok is True
    assert any(level == "warn" for _msg, level in records)


def test_min_available_gb_override_respected(monkeypatch):
    # Stage estimate (export=12) would pass at 20 GB free, but the override
    # raises the bar past what's available -> the override wins.
    monkeypatch.setenv("FOUNDRY_MIN_AVAILABLE_GB", "64")
    monkeypatch.delenv("FOUNDRY_SKIP_MEM_PREFLIGHT", raising=False)
    monkeypatch.setattr(preflight, "get_mem_available_gb", lambda: 20.0)
    monkeypatch.setattr(preflight, "get_gtt_used_gb", lambda: None)
    log, records = _collect_log()
    ok = preflight.check_system_memory("export", log=log)
    assert ok is False
    msg = next(m for m, lvl in records if lvl == "error")
    assert "64" in msg


def test_min_available_gb_override_can_relax_requirement(monkeypatch):
    # Override lowers the bar below the stage's normal (large) estimate.
    monkeypatch.setenv("FOUNDRY_MIN_AVAILABLE_GB", "4")
    monkeypatch.delenv("FOUNDRY_SKIP_MEM_PREFLIGHT", raising=False)
    monkeypatch.setattr(preflight, "get_mem_available_gb", lambda: 8.0)
    monkeypatch.setattr(preflight, "get_gtt_used_gb", lambda: None)
    log, records = _collect_log()
    ok = preflight.check_system_memory("magicquant", log=log)  # normally needs 48 GB
    assert ok is True


def test_gtt_high_but_mem_available_high_still_passes(monkeypatch):
    """The user's key requirement: GTT-in-use is informational only, never blocking."""
    monkeypatch.delenv("FOUNDRY_MIN_AVAILABLE_GB", raising=False)
    monkeypatch.delenv("FOUNDRY_SKIP_MEM_PREFLIGHT", raising=False)
    monkeypatch.setattr(preflight, "get_mem_available_gb", lambda: 80.0)
    monkeypatch.setattr(preflight, "get_gtt_used_gb", lambda: 21.0)  # resident model pinning GTT
    log, records = _collect_log()
    ok = preflight.check_system_memory("magicquant", log=log)
    assert ok is True
    assert not any(level == "error" for _msg, level in records)
    # The GTT usage is still surfaced, just as a warning, not a failure.
    assert any("21" in m and level == "warn" for m, level in records)


def test_gtt_low_no_informational_warning(monkeypatch):
    monkeypatch.delenv("FOUNDRY_MIN_AVAILABLE_GB", raising=False)
    monkeypatch.delenv("FOUNDRY_SKIP_MEM_PREFLIGHT", raising=False)
    monkeypatch.setattr(preflight, "get_mem_available_gb", lambda: 80.0)
    monkeypatch.setattr(preflight, "get_gtt_used_gb", lambda: 1.0)  # below the 4 GB threshold
    log, records = _collect_log()
    ok = preflight.check_system_memory("export", log=log)
    assert ok is True
    assert not any("GTT" in m for m, _level in records)


def test_gtt_unknown_never_blocks(monkeypatch):
    monkeypatch.delenv("FOUNDRY_MIN_AVAILABLE_GB", raising=False)
    monkeypatch.delenv("FOUNDRY_SKIP_MEM_PREFLIGHT", raising=False)
    monkeypatch.setattr(preflight, "get_mem_available_gb", lambda: 80.0)
    monkeypatch.setattr(preflight, "get_gtt_used_gb", lambda: None)
    log, records = _collect_log()
    ok = preflight.check_system_memory("rocmfpx", log=log)
    assert ok is True
