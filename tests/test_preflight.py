"""M-gpu-preflight: GPU-memory preflight check (offline, monkeypatched)."""

import preflight


def test_ok_when_free_exceeds_needed(monkeypatch):
    monkeypatch.setattr(preflight, "get_free_vram_gb", lambda: 40.0)
    assert preflight.check_gpu_memory(30.0) is True


def test_abort_when_free_below_needed(monkeypatch):
    monkeypatch.setattr(preflight, "get_free_vram_gb", lambda: 10.0)
    assert preflight.check_gpu_memory(30.0) is False


def test_unknown_free_proceeds_with_warning(monkeypatch):
    monkeypatch.setattr(preflight, "get_free_vram_gb", lambda: None)
    assert preflight.check_gpu_memory(30.0) is True


def test_skip_flag_bypasses(monkeypatch):
    monkeypatch.setattr(preflight, "get_free_vram_gb", lambda: 1.0)
    assert preflight.check_gpu_memory(30.0, skip=True) is True


def test_rocm_smi_parser_handles_sample():
    sample = (
        "GPU[0] : VRAM Total Memory (B): 128000000000\n"
        "GPU[0] : VRAM Total Used Memory (B): 8000000000\n"
    )
    free = preflight.parse_rocm_smi_free_gb(sample)
    assert free is not None
    assert abs(free - 120.0) < 0.001


def test_rocm_smi_parser_returns_none_on_garbage():
    assert preflight.parse_rocm_smi_free_gb("no useful fields here") is None


def test_stage_estimates_scale_with_size():
    # Training dominates; bigger model -> bigger estimate.
    small = preflight.estimate_stage_gb("training", 9.0)
    big = preflight.estimate_stage_gb("training", 40.0)
    assert big > small
    # Export/merge is light.
    assert preflight.estimate_stage_gb("export", 40.0) < preflight.estimate_stage_gb("training", 40.0)
