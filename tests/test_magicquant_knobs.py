"""MagicQuant orchestrator knobs (use_imatrix/imatrix_corpus/enable_kl/kl_weight/
enable_speed_bench) wired end-to-end: dataclass -> CLI -> service -> entry ->
UI config model -> UI card.

Mirrors the existing rocmfpx_schemes/iq_schemes/seed wiring and its test
coverage style (tests/test_qat_service.py's CLI/UIConfig checks,
tests/test_stage_entries.py's build_config checks).

Also covers the tps-aware speed knobs (speed_aware/speed_metric/speed_weight/
use_bytes_tps/calibration_source/write_calibration) and the "optimize for
speed" CLI/UI convenience that expands into that group -- same end-to-end
wiring pattern, plus the measured-vs-both-paths split (speed_aware/
speed_metric/write_calibration are measured-search-only; speed_weight/
use_bytes_tps/calibration_source reach both run_measured_search and
run_full_search).
"""

import json
import sys
import types
from pathlib import Path

import pytest

import ppl_smoke
from services import MagicQuantService

ROOT = Path(__file__).resolve().parent.parent


# ── core/pipeline.py: MagicQuantConfig dataclass defaults ────────────────────

def _pipeline():
    import importlib

    import pipeline as pl

    return importlib.reload(pl)


def test_magicquant_config_new_knob_defaults():
    pl = _pipeline()
    mc = pl.MagicQuantConfig()
    assert mc.use_imatrix is False
    assert mc.imatrix_corpus is None
    assert mc.enable_kl is False
    assert mc.kl_weight == 0.1
    assert mc.enable_speed_bench is False
    assert mc.measurement_chunks is None
    assert mc.stream_aware is False
    assert mc.head_aggressive is False


def test_magicquant_config_new_speed_knob_defaults():
    pl = _pipeline()
    mc = pl.MagicQuantConfig()
    # None, not False: "no explicit Foundry choice" -> the actual behavior
    # comes from MagicQuantOrchestrator.run_measured_search's own default
    # (see core/services.py's speed_aware docstring and
    # test_entry_speed_aware_omitted_lets_library_default_apply below).
    assert mc.speed_aware is None
    assert mc.speed_metric == "bytes"
    assert mc.speed_weight is None
    assert mc.use_bytes_tps is False
    assert mc.calibration_source == ""
    assert mc.write_calibration is False


# ── core/pipeline.py: CLI flag wiring ─────────────────────────────────────────

def test_magicquant_cli_flags_wire_into_config(monkeypatch):
    pl = _pipeline()
    captured = {}

    def _fake_run_pipeline(cfg, **kwargs):
        captured["cfg"] = cfg
        return {"magicquant": True}

    monkeypatch.setattr(pl, "run_pipeline", _fake_run_pipeline)

    # Defaults: none of the new flags passed.
    pl.main(["--model", "org/m", "--no-export", "--no-heretic", "--no-reap"])
    mc = captured["cfg"].magicquant
    assert mc.use_imatrix is False
    assert mc.imatrix_corpus is None
    assert mc.enable_kl is False
    assert mc.kl_weight == 0.1
    assert mc.enable_speed_bench is False
    assert mc.stream_aware is False
    assert mc.head_aggressive is False

    # All seven flags flipped on.
    pl.main([
        "--model", "org/m", "--no-export", "--no-heretic", "--no-reap",
        "--magicquant-use-imatrix", "--magicquant-imatrix-corpus", "corpus.txt",
        "--magicquant-kl", "--magicquant-kl-weight", "0.42",
        "--magicquant-speed-bench",
        "--magicquant-stream-aware", "--magicquant-head-aggressive",
    ])
    mc = captured["cfg"].magicquant
    assert mc.use_imatrix is True
    assert mc.imatrix_corpus == "corpus.txt"
    assert mc.enable_kl is True
    assert mc.kl_weight == 0.42
    assert mc.enable_speed_bench is True
    assert mc.stream_aware is True
    assert mc.head_aggressive is True


def test_magicquant_chunks_cli_flag_wires_into_config(monkeypatch):
    """--magicquant-chunks is unconditional (like --magicquant-use-imatrix) --
    it must land in the config whether or not --magicquant-measured is set,
    since it also caps run_full_search's baseline pass."""
    pl = _pipeline()
    captured = {}

    def _fake_run_pipeline(cfg, **kwargs):
        captured["cfg"] = cfg
        return {"magicquant": True}

    monkeypatch.setattr(pl, "run_pipeline", _fake_run_pipeline)

    pl.main(["--model", "org/m", "--no-export", "--no-heretic", "--no-reap"])
    assert captured["cfg"].magicquant.measurement_chunks is None

    pl.main([
        "--model", "org/m", "--no-export", "--no-heretic", "--no-reap",
        "--magicquant-chunks", "8",
    ])
    assert captured["cfg"].magicquant.measurement_chunks == 8

    pl.main([
        "--model", "org/m", "--no-export", "--no-heretic", "--no-reap",
        "--magicquant-measured", "--magicquant-chunks", "8",
    ])
    assert captured["cfg"].magicquant.measurement_chunks == 8


def test_magicquant_stream_aware_and_head_aggressive_cli_flags_unconditional(monkeypatch):
    """--magicquant-stream-aware / --magicquant-head-aggressive are search-bias
    knobs consumed by both run_measured_search and run_full_search (like
    --magicquant-use-imatrix) -- they must land in the config regardless of
    --magicquant-measured."""
    pl = _pipeline()
    captured = {}

    def _fake_run_pipeline(cfg, **kwargs):
        captured["cfg"] = cfg
        return {"magicquant": True}

    monkeypatch.setattr(pl, "run_pipeline", _fake_run_pipeline)

    pl.main(["--model", "org/m", "--no-export", "--no-heretic", "--no-reap"])
    mc = captured["cfg"].magicquant
    assert mc.stream_aware is False
    assert mc.head_aggressive is False

    pl.main([
        "--model", "org/m", "--no-export", "--no-heretic", "--no-reap",
        "--magicquant-stream-aware", "--magicquant-head-aggressive",
    ])
    mc = captured["cfg"].magicquant
    assert mc.stream_aware is True
    assert mc.head_aggressive is True

    pl.main([
        "--model", "org/m", "--no-export", "--no-heretic", "--no-reap",
        "--magicquant-measured",
        "--magicquant-stream-aware", "--magicquant-head-aggressive",
    ])
    mc = captured["cfg"].magicquant
    assert mc.stream_aware is True
    assert mc.head_aggressive is True


def test_magicquant_kl_weight_only_applied_when_kl_flag_set(monkeypatch):
    """--magicquant-kl-weight alone (without --magicquant-kl) must not silently
    enable KL scoring -- mirrors --magicquant-rounds only applying under
    --magicquant-measured."""
    pl = _pipeline()
    captured = {}

    def _fake_run_pipeline(cfg, **kwargs):
        captured["cfg"] = cfg
        return {"magicquant": True}

    monkeypatch.setattr(pl, "run_pipeline", _fake_run_pipeline)
    pl.main([
        "--model", "org/m", "--no-export", "--no-heretic", "--no-reap",
        "--magicquant-kl-weight", "0.9",
    ])
    mc = captured["cfg"].magicquant
    assert mc.enable_kl is False
    assert mc.kl_weight == 0.1  # untouched dataclass default


def test_magicquant_speed_cli_flags_wire_into_config(monkeypatch):
    """Individual speed-knob flags wire into MagicQuantConfig, unconditional
    of --magicquant-measured (like --magicquant-use-imatrix) for
    speed_weight/use_bytes_tps/calibration_source; --magicquant-speed-metric
    is only applied together with --magicquant-speed-aware, mirroring
    --magicquant-kl-weight's gate on --magicquant-kl."""
    pl = _pipeline()
    captured = {}

    def _fake_run_pipeline(cfg, **kwargs):
        captured["cfg"] = cfg
        return {"magicquant": True}

    monkeypatch.setattr(pl, "run_pipeline", _fake_run_pipeline)

    pl.main(["--model", "org/m", "--no-export", "--no-heretic", "--no-reap"])
    mc = captured["cfg"].magicquant
    assert mc.speed_aware is None  # no explicit choice -> library default applies
    assert mc.speed_metric == "bytes"
    assert mc.speed_weight is None
    assert mc.use_bytes_tps is False
    assert mc.calibration_source == ""
    assert mc.write_calibration is False

    pl.main([
        "--model", "org/m", "--no-export", "--no-heretic", "--no-reap",
        "--magicquant-speed-aware", "--magicquant-speed-metric", "bench",
        "--magicquant-speed-weight", "0.4", "--magicquant-use-bytes-tps",
        "--magicquant-calibration-source", "calib.json",
        "--magicquant-write-calibration",
    ])
    mc = captured["cfg"].magicquant
    assert mc.speed_aware is True
    assert mc.speed_metric == "bench"
    assert mc.speed_weight == 0.4
    assert mc.use_bytes_tps is True
    assert mc.calibration_source == "calib.json"
    assert mc.write_calibration is True


def test_magicquant_speed_metric_applies_independently_of_speed_aware_flag(monkeypatch):
    """--magicquant-speed-metric applies whenever explicitly set (like
    --magicquant-imatrix-corpus is independent of --magicquant-use-imatrix),
    NOT gated behind --magicquant-speed-aware -- otherwise combining
    --magicquant-optimize-for-speed (which already turns speed_aware on) with
    an explicit --magicquant-speed-metric override would silently drop the
    override unless --magicquant-speed-aware were also redundantly repeated."""
    pl = _pipeline()
    captured = {}

    def _fake_run_pipeline(cfg, **kwargs):
        captured["cfg"] = cfg
        return {"magicquant": True}

    monkeypatch.setattr(pl, "run_pipeline", _fake_run_pipeline)

    # Passed alone: speed_aware stays unset (no explicit choice), but the
    # metric is still recorded (dormant, like imatrix_corpus without
    # use_imatrix).
    pl.main([
        "--model", "org/m", "--no-export", "--no-heretic", "--no-reap",
        "--magicquant-speed-metric", "bench",
    ])
    mc = captured["cfg"].magicquant
    assert mc.speed_aware is None
    assert mc.speed_metric == "bench"

    # Composes with --magicquant-optimize-for-speed without needing a
    # redundant --magicquant-speed-aware.
    pl.main([
        "--model", "org/m", "--no-export", "--no-heretic", "--no-reap",
        "--magicquant-optimize-for-speed", "--magicquant-speed-metric", "bench",
    ])
    mc = captured["cfg"].magicquant
    assert mc.speed_aware is True
    assert mc.speed_metric == "bench"


def test_magicquant_optimize_for_speed_cli_flag_expands_to_group(monkeypatch):
    """--magicquant-optimize-for-speed is a convenience flag that expands into
    the sensible-defaults bundle (speed_aware=True, speed_metric=bytes,
    speed_weight=0.35, use_bytes_tps=True); an individually-passed flag
    afterward still overrides it for power users."""
    pl = _pipeline()
    captured = {}

    def _fake_run_pipeline(cfg, **kwargs):
        captured["cfg"] = cfg
        return {"magicquant": True}

    monkeypatch.setattr(pl, "run_pipeline", _fake_run_pipeline)

    pl.main([
        "--model", "org/m", "--no-export", "--no-heretic", "--no-reap",
        "--magicquant-optimize-for-speed",
    ])
    mc = captured["cfg"].magicquant
    assert mc.speed_aware is True
    assert mc.speed_metric == "bytes"
    assert mc.speed_weight == 0.35
    assert mc.use_bytes_tps is True

    # An explicit individual override (a different speed weight) still wins.
    pl.main([
        "--model", "org/m", "--no-export", "--no-heretic", "--no-reap",
        "--magicquant-optimize-for-speed", "--magicquant-speed-weight", "0.6",
    ])
    mc = captured["cfg"].magicquant
    assert mc.speed_weight == 0.6
    assert mc.speed_aware is True  # rest of the bundle still applied


# ── core/pipeline.py: cfg_hash includes the new knobs (cache invalidation) ────

def test_stage_magicquant_hash_source_includes_new_knobs():
    """Source-level check that stage_magicquant's cfg_hash dict was extended
    (a functional test would need a full Artifacts/subprocess harness; the
    existing rocmfpx_schemes/iq_schemes wiring is checked the same way in
    tests/test_stage_cleanup.py's source-text assertions)."""
    src = (ROOT / "core" / "pipeline.py").read_text()
    hash_block = src[src.index('def stage_magicquant'):src.index('existing = sorted(artifacts.magicquant_dir')]
    for key in ("use_imatrix", "imatrix_corpus", "enable_kl", "kl_weight",
                "enable_speed_bench", "measurement_chunks",
                "stream_aware", "head_aggressive"):
        assert f'"{key}": mc.{key}' in hash_block, key


def test_stage_magicquant_hash_source_includes_speed_knobs():
    src = (ROOT / "core" / "pipeline.py").read_text()
    hash_block = src[src.index('def stage_magicquant'):src.index('existing = sorted(artifacts.magicquant_dir')]
    for key in ("speed_aware", "speed_metric", "speed_weight", "use_bytes_tps",
                "calibration_source", "write_calibration"):
        assert f'"{key}": mc.{key}' in hash_block, key


# ── core/services.py: MagicQuantService.build_config carries the new keys ────

def test_magicquant_build_config_carries_new_knobs():
    svc = MagicQuantService(ROOT, "python")
    cfg = svc.build_config(
        llamacpp_hint="", pipeline_root_str="/repo", mq_source_override="/src",
        out_abs_str="/o", generations=5, population_size=10,
        target_base_quant="IQ4_NL", tiers_json="{}", model_name="m", verify=False,
        use_imatrix=True, imatrix_corpus="calib.txt",
        enable_kl=True, kl_weight=0.42, enable_speed_bench=True,
        measurement_chunks=8,
        stream_aware=True, head_aggressive=True,
    )
    assert cfg["use_imatrix"] is True
    assert cfg["imatrix_corpus"] == "calib.txt"
    assert cfg["enable_kl"] is True
    assert cfg["kl_weight"] == 0.42
    assert cfg["enable_speed_bench"] is True
    assert cfg["measurement_chunks"] == 8
    assert cfg["stream_aware"] is True
    assert cfg["head_aggressive"] is True
    assert json.loads(json.dumps(cfg)) == cfg


def test_magicquant_build_config_new_knobs_default():
    svc = MagicQuantService(ROOT, "python")
    cfg = svc.build_config(
        llamacpp_hint="", pipeline_root_str="/repo", mq_source_override="/src",
        out_abs_str="/o", generations=5, population_size=10,
        target_base_quant="IQ4_NL", tiers_json="{}", model_name="m",
    )
    # Defaults ON since 4b14e6c: the bundled calibration corpus went from
    # 13 KB English-only (~5 chunks) to ~1 MB across 18 languages plus code
    # and math, verified disjoint from the perplexity eval corpus, with
    # capture bounded to 200 chunks. Weighted quantization beats unweighted
    # once the calibration is sound. imatrix_corpus stays None = bundled.
    assert cfg["use_imatrix"] is True
    assert cfg["imatrix_corpus"] is None
    assert cfg["enable_kl"] is False
    assert cfg["kl_weight"] == 0.1
    assert cfg["enable_speed_bench"] is False
    assert cfg["measurement_chunks"] is None
    assert cfg["stream_aware"] is False
    assert cfg["head_aggressive"] is False


def test_magicquant_build_config_carries_speed_knobs():
    svc = MagicQuantService(ROOT, "python")
    cfg = svc.build_config(
        llamacpp_hint="", pipeline_root_str="/repo", mq_source_override="/src",
        out_abs_str="/o", generations=5, population_size=10,
        target_base_quant="IQ4_NL", tiers_json="{}", model_name="m", verify=False,
        speed_aware=True, speed_metric="bench", speed_weight=0.4,
        use_bytes_tps=True, calibration_source="calib.json",
        write_calibration=True,
    )
    assert cfg["speed_aware"] is True
    assert cfg["speed_metric"] == "bench"
    assert cfg["speed_weight"] == 0.4
    assert cfg["use_bytes_tps"] is True
    assert cfg["calibration_source"] == "calib.json"
    assert cfg["write_calibration"] is True
    assert json.loads(json.dumps(cfg)) == cfg


def test_magicquant_build_config_speed_knobs_default():
    svc = MagicQuantService(ROOT, "python")
    cfg = svc.build_config(
        llamacpp_hint="", pipeline_root_str="/repo", mq_source_override="/src",
        out_abs_str="/o", generations=5, population_size=10,
        target_base_quant="IQ4_NL", tiers_json="{}", model_name="m",
    )
    # None (not False): "no explicit Foundry choice" -- the JSON emitted
    # for _magicquant_entry.py carries speed_aware=null, and entry.run()
    # omits the kwarg entirely so MagicQuantOrchestrator.run_measured_
    # search's own default applies instead of a stale hardcoded False (see
    # test_entry_speed_aware_omitted_lets_library_default_apply).
    assert cfg["speed_aware"] is None
    assert cfg["speed_metric"] == "bytes"
    assert cfg["speed_weight"] is None
    assert cfg["use_bytes_tps"] is False
    assert cfg["calibration_source"] == ""
    assert cfg["write_calibration"] is False


# ── core/_magicquant_entry.py: run() wiring to the orchestrator ──────────────

class _FakeOrchestrator:
    """Signature-faithful stand-in for MagicQuantOrchestrator: passing a
    kwarg run_full_search doesn't accept (e.g. enable_kl) raises TypeError,
    same as the real one -- this is what actually catches a wiring mistake."""

    instances = []

    def __init__(self, **kwargs):
        self.init_kwargs = kwargs
        _FakeOrchestrator.instances.append(self)
        self.run_full_search_kwargs = None
        self.run_measured_search_kwargs = None

    def run_full_search(
        self, target_base_quant="MXFP4_MOE", max_generations=50, population_size=100,
        verbose=True, patience=None, enable_rocmfpx=False, enable_iq=False,
        seed=None, use_imatrix=False, imatrix_corpus=None, measurement_chunks=None,
        stream_aware=False, head_aggressive=False,
        speed_weight=None, use_bytes_tps=False, calibration_source="",
    ):
        self.run_full_search_kwargs = dict(
            target_base_quant=target_base_quant, max_generations=max_generations,
            population_size=population_size, verbose=verbose, patience=patience,
            enable_rocmfpx=enable_rocmfpx, enable_iq=enable_iq, seed=seed,
            use_imatrix=use_imatrix, imatrix_corpus=imatrix_corpus,
            measurement_chunks=measurement_chunks,
            stream_aware=stream_aware, head_aggressive=head_aggressive,
            speed_weight=speed_weight, use_bytes_tps=use_bytes_tps,
            calibration_source=calibration_source,
        )
        return [], {"Q4": {"config": {}}}

    def run_measured_search(
        self, target_base_quant="MXFP4_MOE", search_generations=30, population_size=80,
        measurement_rounds=3, candidates_per_round=4, verbose=True, patience=None,
        enable_rocmfpx=False, enable_iq=False, seed=None, use_imatrix=False,
        imatrix_corpus=None, enable_kl=False, kl_weight=0.1, enable_speed_bench=False,
        measurement_chunks=None, stream_aware=False, head_aggressive=False,
        # speed_aware=True mirrors the REAL MagicQuantOrchestrator.
        # run_measured_search's own default (the 2026-07 fix) -- kept
        # signature-faithful on purpose, see
        # test_fake_orchestrator_speed_aware_default_matches_real_library,
        # which cross-checks this literal against the installed library so
        # this fake can't silently drift out of sync again.
        speed_aware=True, speed_metric="bytes", speed_weight=None,
        use_bytes_tps=False, write_calibration=False, calibration_source="",
    ):
        self.run_measured_search_kwargs = dict(
            target_base_quant=target_base_quant, search_generations=search_generations,
            population_size=population_size, measurement_rounds=measurement_rounds,
            candidates_per_round=candidates_per_round, verbose=verbose, patience=patience,
            enable_rocmfpx=enable_rocmfpx, enable_iq=enable_iq, seed=seed,
            use_imatrix=use_imatrix, imatrix_corpus=imatrix_corpus, enable_kl=enable_kl,
            kl_weight=kl_weight, enable_speed_bench=enable_speed_bench,
            measurement_chunks=measurement_chunks,
            stream_aware=stream_aware, head_aggressive=head_aggressive,
            speed_aware=speed_aware, speed_metric=speed_metric,
            speed_weight=speed_weight, use_bytes_tps=use_bytes_tps,
            write_calibration=write_calibration, calibration_source=calibration_source,
        )
        return [], {"Q4": {"config": {}}}

    def generate_tiered_models(self, tiered, model_name_prefix, tiers, verify):
        return [self._out_path]


@pytest.fixture
def fake_orchestrator(monkeypatch, tmp_path):
    """Install a fake magicquant.orchestrator module and a fake find_llamacpp
    so _magicquant_entry.run() can execute fully offline (no git clone, no
    real quantization)."""
    import _magicquant_entry as entry

    _FakeOrchestrator.instances = []
    out_gguf = tmp_path / "model-Q4.gguf"
    out_gguf.write_bytes(b"fake gguf")
    _FakeOrchestrator._out_path = str(out_gguf)

    fake_pkg = types.ModuleType("magicquant")
    fake_orch_mod = types.ModuleType("magicquant.orchestrator")
    fake_orch_mod.MagicQuantOrchestrator = _FakeOrchestrator
    monkeypatch.setitem(sys.modules, "magicquant", fake_pkg)
    monkeypatch.setitem(sys.modules, "magicquant.orchestrator", fake_orch_mod)
    monkeypatch.setattr(entry, "find_llamacpp", lambda hint="": "/fake/llamacpp")
    # Measured runs auto-convert a safetensors source to BF16 GGUF via
    # convert_hf_to_gguf.py, which the fake llamacpp dir doesn't have.
    monkeypatch.setattr(
        entry, "_ensure_bf16_gguf",
        lambda llamacpp_dir, source, out_dir, model_name=None: source
    )

    return entry


def _write_cfg(tmp_path, src_file, **overrides):
    cfg = {
        "pipeline_root": str(ROOT),
        "pipeline_root_str": str(ROOT),
        "llamacpp_hint": "",
        "mq_source_override": str(src_file),
        "out_abs_str": str(tmp_path / "out"),
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
        # None (absent choice) here matches what a genuinely default Foundry
        # config now emits (services.py's speed_aware default changed from a
        # hardcoded False to None -- see test_entry_speed_aware_omitted_lets_
        # library_default_apply). Tests that want an explicit True/False
        # still override it below.
        "speed_aware": None,
        "speed_metric": "bytes",
        "speed_weight": None,
        "use_bytes_tps": False,
        "calibration_source": "",
        "write_calibration": False,
    }
    cfg.update(overrides)
    cfg_path = tmp_path / "cfg.json"
    cfg_path.write_text(json.dumps(cfg))
    return cfg_path


def test_entry_run_full_search_gets_imatrix_but_not_measured_only_knobs(
    fake_orchestrator, tmp_path,
):
    """Prediction-only search (measured=False) must receive use_imatrix/
    imatrix_corpus (run_full_search accepts them) and must NOT be asked to
    pass enable_kl/kl_weight/enable_speed_bench (run_full_search has no such
    params -- a TypeError here means the wiring leaked measured-only knobs
    into the wrong call)."""
    src_file = tmp_path / "src.safetensors"
    src_file.write_bytes(b"x")
    cfg_path = _write_cfg(
        tmp_path, src_file,
        measured=False, use_imatrix=True, imatrix_corpus="calib.txt",
        enable_kl=True, kl_weight=0.42, enable_speed_bench=True,
        measurement_chunks=6, stream_aware=True, head_aggressive=True,
    )

    fake_orchestrator.run(str(cfg_path))

    inst = _FakeOrchestrator.instances[-1]
    assert inst.run_measured_search_kwargs is None
    kw = inst.run_full_search_kwargs
    assert kw is not None
    assert kw["use_imatrix"] is True
    assert kw["imatrix_corpus"] == "calib.txt"
    # measurement_chunks reaches run_full_search too -- it caps the (single)
    # baseline perplexity pass, symmetric with the measured path.
    assert kw["measurement_chunks"] == 6
    # stream_aware/head_aggressive are search-bias knobs, not measured-only --
    # run_full_search accepts (and must receive) both, same as use_imatrix.
    assert kw["stream_aware"] is True
    assert kw["head_aggressive"] is True


def test_entry_run_measured_search_gets_all_seven_knobs(fake_orchestrator, tmp_path):
    src_file = tmp_path / "src.safetensors"
    src_file.write_bytes(b"x")
    cfg_path = _write_cfg(
        tmp_path, src_file,
        measured=True, use_imatrix=True, imatrix_corpus="calib.txt",
        enable_kl=True, kl_weight=0.42, enable_speed_bench=True,
        measurement_chunks=6, stream_aware=True, head_aggressive=True,
    )

    fake_orchestrator.run(str(cfg_path))

    inst = _FakeOrchestrator.instances[-1]
    assert inst.run_full_search_kwargs is None
    kw = inst.run_measured_search_kwargs
    assert kw is not None
    assert kw["use_imatrix"] is True
    assert kw["imatrix_corpus"] == "calib.txt"
    assert kw["enable_kl"] is True
    assert kw["kl_weight"] == 0.42
    assert kw["enable_speed_bench"] is True
    assert kw["measurement_chunks"] == 6
    assert kw["stream_aware"] is True
    assert kw["head_aggressive"] is True


def test_entry_stream_aware_and_head_aggressive_default_off(fake_orchestrator, tmp_path):
    src_file = tmp_path / "src.safetensors"
    src_file.write_bytes(b"x")
    cfg_path = _write_cfg(tmp_path, src_file, measured=True)

    fake_orchestrator.run(str(cfg_path))

    inst = _FakeOrchestrator.instances[-1]
    assert inst.run_measured_search_kwargs["stream_aware"] is False
    assert inst.run_measured_search_kwargs["head_aggressive"] is False


def test_entry_measurement_chunks_defaults_to_none(fake_orchestrator, tmp_path):
    src_file = tmp_path / "src.safetensors"
    src_file.write_bytes(b"x")
    cfg_path = _write_cfg(tmp_path, src_file, measured=True)

    fake_orchestrator.run(str(cfg_path))

    inst = _FakeOrchestrator.instances[-1]
    assert inst.run_measured_search_kwargs["measurement_chunks"] is None


def test_entry_empty_imatrix_corpus_normalized_to_none(fake_orchestrator, tmp_path):
    """An empty-string imatrix_corpus (e.g. from a UI text field left blank)
    must resolve to None, not '' -- ensure_imatrix's default-corpus branch is
    keyed on `is not None`, so '' would wrongly resolve to Path('')."""
    src_file = tmp_path / "src.safetensors"
    src_file.write_bytes(b"x")
    cfg_path = _write_cfg(tmp_path, src_file, use_imatrix=True, imatrix_corpus="")

    fake_orchestrator.run(str(cfg_path))

    inst = _FakeOrchestrator.instances[-1]
    assert inst.run_full_search_kwargs["imatrix_corpus"] is None


def test_entry_run_full_search_gets_speed_weight_but_not_measured_only_speed_knobs(
    fake_orchestrator, tmp_path,
):
    """Prediction-only search (measured=False) must receive speed_weight/
    use_bytes_tps/calibration_source (run_full_search accepts them) and must
    NOT be asked to pass speed_aware/speed_metric/write_calibration
    (run_full_search has no such params -- a TypeError here means the wiring
    leaked measured-only speed knobs into the wrong call)."""
    src_file = tmp_path / "src.safetensors"
    src_file.write_bytes(b"x")
    cfg_path = _write_cfg(
        tmp_path, src_file,
        measured=False, speed_aware=True, speed_metric="bench",
        speed_weight=0.4, use_bytes_tps=True,
        calibration_source="calib.json", write_calibration=True,
    )

    fake_orchestrator.run(str(cfg_path))

    inst = _FakeOrchestrator.instances[-1]
    assert inst.run_measured_search_kwargs is None
    kw = inst.run_full_search_kwargs
    assert kw is not None
    assert kw["speed_weight"] == 0.4
    assert kw["use_bytes_tps"] is True
    assert kw["calibration_source"] == "calib.json"


def test_entry_run_measured_search_gets_all_speed_knobs(fake_orchestrator, tmp_path):
    src_file = tmp_path / "src.safetensors"
    src_file.write_bytes(b"x")
    cfg_path = _write_cfg(
        tmp_path, src_file,
        measured=True, speed_aware=True, speed_metric="bench",
        speed_weight=0.4, use_bytes_tps=True,
        calibration_source="calib.json", write_calibration=True,
    )

    fake_orchestrator.run(str(cfg_path))

    inst = _FakeOrchestrator.instances[-1]
    assert inst.run_full_search_kwargs is None
    kw = inst.run_measured_search_kwargs
    assert kw is not None
    assert kw["speed_aware"] is True
    assert kw["speed_metric"] == "bench"
    assert kw["speed_weight"] == 0.4
    assert kw["use_bytes_tps"] is True
    assert kw["write_calibration"] is True
    assert kw["calibration_source"] == "calib.json"


def test_entry_speed_knobs_default_off_except_speed_aware(fake_orchestrator, tmp_path):
    """All the OTHER speed knobs stay off by default. speed_aware is the one
    exception: a default Foundry config carries speed_aware=None (no
    explicit choice), entry.run() omits the kwarg entirely for that case
    (see apply_dequant_env-style handling in run()), and the resolved value
    is therefore whatever MagicQuantOrchestrator.run_measured_search's OWN
    default is -- True, per the 2026-07 fix -- not a hardcoded False. See
    test_entry_speed_aware_omitted_lets_library_default_apply for the
    focused regression."""
    src_file = tmp_path / "src.safetensors"
    src_file.write_bytes(b"x")
    cfg_path = _write_cfg(tmp_path, src_file, measured=True)

    fake_orchestrator.run(str(cfg_path))

    kw = _FakeOrchestrator.instances[-1].run_measured_search_kwargs
    assert kw["speed_aware"] is True
    assert kw["speed_metric"] == "bytes"
    assert kw["speed_weight"] is None
    assert kw["use_bytes_tps"] is False
    assert kw["write_calibration"] is False
    assert kw["calibration_source"] == ""


def test_entry_speed_aware_omitted_lets_library_default_apply(fake_orchestrator, tmp_path):
    """BLOCKER regression (2026-07): a default Foundry config must result in
    speed-aware (size-aware) selection being ACTIVE, not merely "whatever
    value happens to get passed through".

    Before this fix: MagicQuantService.build_config declared
    ``speed_aware: bool = False`` and ALWAYS injected the key into the
    emitted config JSON; core/_magicquant_entry.py then did
    ``speed_aware=cfg.get("speed_aware", False)`` -- since the key was
    always present, the ``, False`` fallback never applied, and a default
    Foundry config permanently pinned every real run to
    ``speed_aware=False`` no matter what MagicQuantOrchestrator's own
    default became. Proven to fail pre-fix: reverting either
    MagicQuantService.build_config's default back to a bare ``False``
    (always-inject) OR core/_magicquant_entry.py's kwarg construction back
    to ``speed_aware=cfg.get("speed_aware", False)`` makes this test's
    ``kw["speed_aware"] is True`` assertion fail (it becomes False),
    exactly reproducing the blocker.

    This exercises the real MagicQuantService.build_config -> JSON ->
    entry.run() path (not a hand-built cfg dict), so it also catches a
    regression in build_config's own default, not just entry.py's read
    side.
    """
    from services import MagicQuantService

    src_file = tmp_path / "src.safetensors"
    src_file.write_bytes(b"x")

    svc = MagicQuantService(ROOT, "python")
    cfg = svc.build_config(
        llamacpp_hint="", pipeline_root_str=str(ROOT),
        mq_source_override=str(src_file), out_abs_str=str(tmp_path / "out"),
        generations=5, population_size=10, target_base_quant="MXFP4_MOE",
        tiers_json=json.dumps(["Q4"]), model_name="m",
        measured=True,
        # speed_aware deliberately NOT passed -- this is what a genuinely
        # default Foundry config (CLI with no --magicquant-speed-aware flag,
        # or a UI submission where the toggle was never touched) looks like.
    )
    assert cfg["speed_aware"] is None  # sanity: the JSON really is unset

    cfg_path = tmp_path / "cfg.json"
    cfg_path.write_text(json.dumps(cfg))

    fake_orchestrator.run(str(cfg_path))

    kw = _FakeOrchestrator.instances[-1].run_measured_search_kwargs
    assert kw["speed_aware"] is True, (
        "a DEFAULT Foundry config must result in speed-aware (size-aware) "
        f"selection being ACTIVE -- got speed_aware={kw['speed_aware']!r}"
    )


def test_fake_orchestrator_speed_aware_default_matches_real_library():
    """Guards _FakeOrchestrator.run_measured_search's speed_aware=True
    default (used by the tests above) against silently drifting out of sync
    with the REAL, installed magicquant package's own default -- otherwise
    the tests above could pass for the wrong reason (a stale fake) even
    after a future MagicQuant change flips the real default again."""
    import inspect

    from magicquant.orchestrator import MagicQuantOrchestrator

    real_default = inspect.signature(
        MagicQuantOrchestrator.run_measured_search
    ).parameters["speed_aware"].default
    fake_default = inspect.signature(
        _FakeOrchestrator.run_measured_search
    ).parameters["speed_aware"].default
    assert real_default is True, (
        "MagicQuantOrchestrator.run_measured_search's own speed_aware "
        f"default changed to {real_default!r} -- update "
        "_FakeOrchestrator's matching default (and this assertion) "
        "to match"
    )
    assert fake_default == real_default


# ── ui/app.py: MagicQuantCfg pydantic model ───────────────────────────────────

def test_magicquantcfg_new_knob_defaults():
    import app as app_module

    c = app_module.MagicQuantCfg()
    # See the note in test_magicquant_build_config_new_knobs_default.
    assert c.use_imatrix is True
    assert c.imatrix_corpus is None
    assert c.enable_kl is False
    assert c.kl_weight == 0.1
    assert c.enable_speed_bench is False
    assert c.measurement_chunks is None
    assert c.stream_aware is False
    assert c.head_aggressive is False


def test_magicquantcfg_accepts_new_knobs():
    import app as app_module

    c = app_module.MagicQuantCfg(
        use_imatrix=True, imatrix_corpus="calib.txt",
        enable_kl=True, kl_weight=0.3, enable_speed_bench=True,
        measurement_chunks=8, stream_aware=True, head_aggressive=True,
    )
    assert c.use_imatrix is True
    assert c.imatrix_corpus == "calib.txt"
    assert c.enable_kl is True
    assert c.kl_weight == 0.3
    assert c.enable_speed_bench is True
    assert c.measurement_chunks == 8
    assert c.stream_aware is True
    assert c.head_aggressive is True


def test_magicquantcfg_new_speed_knob_defaults():
    import app as app_module

    c = app_module.MagicQuantCfg()
    assert c.speed_aware is None  # no explicit UI choice -> library default applies
    assert c.speed_metric == "bytes"
    assert c.speed_weight is None
    assert c.use_bytes_tps is False
    assert c.calibration_source == ""
    assert c.write_calibration is False


def test_magicquantcfg_accepts_speed_knobs():
    import app as app_module

    c = app_module.MagicQuantCfg(
        speed_aware=True, speed_metric="bench", speed_weight=0.4,
        use_bytes_tps=True, calibration_source="calib.json",
        write_calibration=True,
    )
    assert c.speed_aware is True
    assert c.speed_metric == "bench"
    assert c.speed_weight == 0.4
    assert c.use_bytes_tps is True
    assert c.calibration_source == "calib.json"
    assert c.write_calibration is True


def test_do_magicquant_hash_source_includes_new_knobs():
    src = (ROOT / "ui" / "app.py").read_text()
    hash_block = src[src.index("async def do_magicquant"):src.index("existing_ggufs = sorted(mq_dir.glob")]
    for key in ("use_imatrix", "imatrix_corpus", "enable_kl", "kl_weight",
                "enable_speed_bench", "measurement_chunks",
                "stream_aware", "head_aggressive"):
        assert f'"{key}": mc.{key}' in hash_block, key
    assert "use_imatrix=mc.use_imatrix" in src
    assert "imatrix_corpus=mc.imatrix_corpus" in src
    assert "enable_kl=mc.enable_kl" in src
    assert "kl_weight=mc.kl_weight" in src
    assert "enable_speed_bench=mc.enable_speed_bench" in src
    assert "measurement_chunks=mc.measurement_chunks" in src
    assert "stream_aware=mc.stream_aware" in src
    assert "head_aggressive=mc.head_aggressive" in src


def test_do_magicquant_hash_source_includes_speed_knobs():
    src = (ROOT / "ui" / "app.py").read_text()
    hash_block = src[src.index("async def do_magicquant"):src.index("existing_ggufs = sorted(mq_dir.glob")]
    for key in ("speed_aware", "speed_metric", "speed_weight", "use_bytes_tps",
                "calibration_source", "write_calibration"):
        assert f'"{key}": mc.{key}' in hash_block, key
    assert "speed_aware=mc.speed_aware" in src
    assert "speed_metric=mc.speed_metric" in src
    assert "speed_weight=mc.speed_weight" in src
    assert "use_bytes_tps=mc.use_bytes_tps" in src
    assert "calibration_source=mc.calibration_source" in src
    assert "write_calibration=mc.write_calibration" in src


# ── ui/index.html: MagicQuant card exposes + gates the new controls ──────────

def test_index_html_serves_magicquant_new_knob_controls():
    from fastapi.testclient import TestClient

    import app as app_module

    client = TestClient(app_module.app)
    r = client.get("/")
    assert r.status_code == 200
    body = r.text
    assert "magicquant.use_imatrix" in body
    assert "magicquant.imatrix_corpus" in body
    assert "magicquant.enable_kl" in body
    assert "magicquant.kl_weight" in body
    assert "magicquant.enable_speed_bench" in body
    assert "magicquant.measurement_chunks" in body
    assert "magicquant.stream_aware" in body
    assert "magicquant.head_aggressive" in body


def test_index_html_gates_kl_and_speed_bench_behind_measured():
    """KL and speed-bench controls must only render when c.measured is true;
    use_imatrix/imatrix_corpus are NOT measured-gated (run_full_search also
    accepts them, per the entry.py wiring). measurement_chunks is gated the
    same as KL/speed-bench, even though it too reaches run_full_search --
    it's shown alongside the other measured-search-only controls to avoid
    cluttering the default (prediction-only) view. stream_aware/head_aggressive
    are search-bias knobs (not measured-only, like use_imatrix) so they must
    NOT be gated either."""
    src = (ROOT / "ui" / "index.html").read_text()
    fn_start = src.index("function renderMagicQuant()")
    fn_end = src.index("function renderROCmFPX()")
    body = src[fn_start:fn_end]
    gated = body[body.index("c.measured ?"):]
    assert "magicquant.enable_kl" in gated
    assert "magicquant.kl_weight" in gated
    assert "magicquant.enable_speed_bench" in gated
    assert "magicquant.measurement_chunks" in gated
    ungated = body[:body.index("c.measured ?")]
    assert "magicquant.use_imatrix" in ungated
    assert "magicquant.imatrix_corpus" in ungated
    assert "magicquant.stream_aware" in ungated
    assert "magicquant.head_aggressive" in ungated


def test_index_html_head_aggressive_labeled_superseded():
    """The task spec requires head_aggressive to be clearly labeled as
    superseded by stream_aware in the UI copy."""
    src = (ROOT / "ui" / "index.html").read_text()
    fn_start = src.index("function renderMagicQuant()")
    fn_end = src.index("function renderROCmFPX()")
    body = src[fn_start:fn_end]
    head_line = next(
        line for line in body.splitlines() if "magicquant.head_aggressive" in line
    )
    assert "superseded" in head_line.lower()
    assert "stream-aware" in head_line.lower()


def test_index_html_serves_magicquant_speed_knob_controls():
    from fastapi.testclient import TestClient

    import app as app_module

    client = TestClient(app_module.app)
    r = client.get("/")
    assert r.status_code == 200
    body = r.text
    assert "magicquant.speed_aware" in body
    assert "magicquant.speed_metric" in body
    assert "magicquant.speed_weight" in body
    assert "magicquant.use_bytes_tps" in body
    assert "magicquant.calibration_source" in body
    assert "magicquant.write_calibration" in body
    assert "toggleOptimizeForSpeed" in body


def test_index_html_gates_speed_aware_and_write_calibration_behind_measured():
    """speed_aware/speed_metric/write_calibration are measured-search-only
    (like enable_kl/kl_weight/enable_speed_bench) so must only render when
    c.measured is true; speed_weight/use_bytes_tps/calibration_source reach
    both search paths (like use_imatrix/imatrix_corpus) so must NOT be
    gated."""
    src = (ROOT / "ui" / "index.html").read_text()
    fn_start = src.index("function renderMagicQuant()")
    fn_end = src.index("function renderROCmFPX()")
    body = src[fn_start:fn_end]
    speed_section_start = body.index("speed-optimize-box")
    gated_marker = body.index("c.measured ?", speed_section_start)
    gated = body[gated_marker:]
    assert "magicquant.speed_aware" in gated
    assert "magicquant.speed_metric" in gated
    assert "magicquant.write_calibration" in gated
    ungated = body[speed_section_start:gated_marker]
    assert "magicquant.speed_weight" in ungated
    assert "magicquant.use_bytes_tps" in ungated
    assert "magicquant.calibration_source" in ungated


def test_index_html_optimize_for_speed_checkbox_present():
    """The single convenience checkbox must exist and call
    toggleOptimizeForSpeed, distinct from the individual advanced controls."""
    src = (ROOT / "ui" / "index.html").read_text()
    fn_start = src.index("function renderMagicQuant()")
    fn_end = src.index("function renderROCmFPX()")
    body = src[fn_start:fn_end]
    assert "onchange=\"toggleOptimizeForSpeed(this.checked)\"" in body
    assert "Optimize for generation speed" in body
    # The advanced individual controls live behind a <details> reveal.
    assert "<details" in body
    assert "Advanced speed controls" in body


def test_toggle_optimize_for_speed_js_sets_the_full_bundle():
    """toggleOptimizeForSpeed must set/clear all four knobs together (the
    coherence pairing: the search-objective half alone doesn't change outputs
    without speed_aware selection also on)."""
    src = (ROOT / "ui" / "index.html").read_text()
    fn_start = src.index("function toggleOptimizeForSpeed(")
    fn_end = src.index("\n}", fn_start)
    body = src[fn_start:fn_end]
    assert "mc.speed_aware = on" in body
    assert "mc.speed_metric = 'bytes'" in body
    assert "mc.use_bytes_tps = on" in body
    assert "mc.speed_weight" in body


# ── find_llamacpp hint layouts ────────────────────────────────────────────────

def test_find_llamacpp_accepts_bare_build_dir_hint(tmp_path):
    """A ROCmFPX-style build dir (binaries in bin/, no convert script, no
    build/ subdir) must be accepted -- it used to fail validation and fall
    back silently to an auto-detected, possibly arch-incompatible build."""
    import _train_entry  # noqa: F401  (sys.path side effect via conftest)
    import _magicquant_entry as entry

    fork = tmp_path / "build-strix-rocmfp4"
    (fork / "bin").mkdir(parents=True)
    (fork / "bin" / "llama-quantize").write_text("")
    assert entry.find_llamacpp(str(fork)) == str(fork)


def test_find_llamacpp_bad_hint_warns_and_falls_back(tmp_path, capsys):
    import _magicquant_entry as entry

    entry.find_llamacpp(str(tmp_path / "nope"))
    assert "falling back to auto-detection" in capsys.readouterr().out


def test_find_llamacpp_prefers_rocmfpx_fork_over_stock(tmp_path, monkeypatch):
    """With no hint/env, an installed ROCmFPX fork build wins over ~/llama.cpp:
    it measures everything stock can, GPU-offloads by default, and is the only
    build that handles the rocmfp* types the pipeline produces."""
    import _magicquant_entry as entry

    home = tmp_path
    fork_bin = home / "ROCmFPX" / "build-strix-rocmfp4" / "bin"
    fork_bin.mkdir(parents=True)
    (fork_bin / "llama-quantize").write_text("")
    stock = home / "llama.cpp"
    stock.mkdir()
    (stock / "convert_hf_to_gguf.py").write_text("")

    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    monkeypatch.delenv("LLAMACPP_PATH", raising=False)
    assert entry.find_llamacpp("") == str(fork_bin.parent)
    # explicit hint still beats the fork preference
    assert entry.find_llamacpp(str(stock)) == str(stock)


# ── allow_dequant_source: opt-in dequant of already-quantized GGUF sources ────

def test_magicquant_config_allow_dequant_source_default():
    pl = _pipeline()
    mc = pl.MagicQuantConfig()
    assert mc.allow_dequant_source is False


def test_magicquant_dequant_source_cli_flag_wires_into_config(monkeypatch):
    pl = _pipeline()
    captured = {}

    def _fake_run_pipeline(cfg, **kwargs):
        captured["cfg"] = cfg
        return {"magicquant": True}

    monkeypatch.setattr(pl, "run_pipeline", _fake_run_pipeline)

    pl.main(["--model", "org/m", "--no-export", "--no-heretic", "--no-reap"])
    assert captured["cfg"].magicquant.allow_dequant_source is False

    pl.main([
        "--model", "org/m", "--no-export", "--no-heretic", "--no-reap",
        "--magicquant-dequant-source",
    ])
    assert captured["cfg"].magicquant.allow_dequant_source is True


def test_stage_magicquant_hash_source_includes_allow_dequant_source():
    src = (ROOT / "core" / "pipeline.py").read_text()
    hash_block = src[src.index('def stage_magicquant'):src.index('existing = sorted(artifacts.magicquant_dir')]
    assert '"allow_dequant_source": mc.allow_dequant_source' in hash_block


def test_magicquant_build_config_allow_dequant_source_default_false():
    svc = MagicQuantService(ROOT, "python")
    cfg = svc.build_config(
        llamacpp_hint="", pipeline_root_str="/repo", mq_source_override="/src",
        out_abs_str="/o", generations=5, population_size=10,
        target_base_quant="IQ4_NL", tiers_json="{}", model_name="m",
    )
    assert cfg["allow_dequant_source"] is False


def test_magicquant_build_config_allow_dequant_source_explicit_true():
    svc = MagicQuantService(ROOT, "python")
    cfg = svc.build_config(
        llamacpp_hint="", pipeline_root_str="/repo", mq_source_override="/src",
        out_abs_str="/o", generations=5, population_size=10,
        target_base_quant="IQ4_NL", tiers_json="{}", model_name="m",
        allow_dequant_source=True,
    )
    assert cfg["allow_dequant_source"] is True
    assert json.loads(json.dumps(cfg)) == cfg


# ── core/_magicquant_entry.py: apply_dequant_env ──────────────────────────────

def test_apply_dequant_env_noop_when_key_missing():
    import _magicquant_entry as entry

    environ = {}
    assert entry.apply_dequant_env({}, environ) is False
    assert environ == {}


def test_apply_dequant_env_noop_when_key_false():
    import _magicquant_entry as entry

    environ = {}
    assert entry.apply_dequant_env({"allow_dequant_source": False}, environ) is False
    assert environ == {}


def test_apply_dequant_env_sets_env_when_key_true():
    import _magicquant_entry as entry

    environ = {}
    assert entry.apply_dequant_env({"allow_dequant_source": True}, environ) is True
    assert environ == {"MAGICQUANT_ALLOW_DEQUANT_SOURCE": "1"}


def test_pipeline_help_does_not_crash():
    """argparse expands help via '<help> % params', so a literal '%' in a help
    string (e.g. '+18% gen speed') raises TypeError and breaks --help entirely.
    Regression guard for the --magicquant-stream-aware help string."""
    pl = _pipeline()
    with pytest.raises(SystemExit) as exc:  # --help exits 0, never TypeError
        pl.main(["--help"])
    assert exc.value.code == 0


# ── source_model: explicit MagicQuant source override ────────────────────────

def test_magicquant_config_source_model_default():
    pl = _pipeline()
    assert pl.MagicQuantConfig().source_model == ""


def test_magicquant_source_model_cli_flag_wires_into_config(monkeypatch):
    pl = _pipeline()
    captured = {}

    def _fake_run_pipeline(cfg, **kwargs):
        captured["cfg"] = cfg
        return {"magicquant": True}

    monkeypatch.setattr(pl, "run_pipeline", _fake_run_pipeline)

    pl.main(["--model", "org/m", "--no-export", "--no-heretic", "--no-reap"])
    assert captured["cfg"].magicquant.source_model == ""

    pl.main([
        "--model", "org/m", "--no-export", "--no-heretic", "--no-reap",
        "--magicquant-source-model", "/models/release-q8_0.gguf",
    ])
    assert captured["cfg"].magicquant.source_model == "/models/release-q8_0.gguf"


def test_stage_magicquant_honors_source_model_override():
    """The stage must prefer mc.source_model over artifact resolution --
    without this, --magicquant-dequant-source's sole use case (a GGUF-only
    release, which the artifact resolver can never find) is unreachable
    from the CLI. Static source check, matching the hash-block tests above."""
    src = (ROOT / "core" / "pipeline.py").read_text()
    stage = src[src.index("def stage_magicquant"):src.index("def stage_rocmfpx")]
    assert "if mc.source_model:" in stage
    assert "source = mc.source_model" in stage
    # The override must be decided BEFORE falling back to the resolver.
    assert stage.index("if mc.source_model:") < stage.index("resolve_artifact_source(")


# ── Change 1: always convert safetensors sources to BF16 GGUF ────────────────
#
# should_convert_source_to_gguf is the pure predicate _magicquant_entry.run()
# refactored the conversion decision into, so it's testable without spawning
# a real conversion. The task requirement: a directory (safetensors) source
# converts regardless of `measured`; only llamacpp-unavailable (None) and an
# already-GGUF source stay unconverted.

def test_should_convert_source_to_gguf_dir_with_llamacpp():
    import _magicquant_entry as entry

    assert entry.should_convert_source_to_gguf(True, "/fake/llamacpp") is True


def test_should_convert_source_to_gguf_dir_without_llamacpp_falls_back():
    import _magicquant_entry as entry

    assert entry.should_convert_source_to_gguf(True, None) is False


def test_should_convert_source_to_gguf_non_dir_source_never_converts():
    """A non-directory resolved source (already .gguf, or a bare single-file
    override) is left alone regardless of llamacpp availability -- there is
    nothing for this predicate to convert."""
    import _magicquant_entry as entry

    assert entry.should_convert_source_to_gguf(False, "/fake/llamacpp") is False
    assert entry.should_convert_source_to_gguf(False, None) is False


def test_entry_converts_directory_source_even_when_not_measured(
    fake_orchestrator, tmp_path, monkeypatch,
):
    """Change 1's core behavioral requirement: a safetensors DIRECTORY source
    must be converted to BF16 GGUF even under prediction-only search
    (measured=False) -- previously conversion was gated on measured=True."""
    src_dir = tmp_path / "merged_model"
    src_dir.mkdir()
    (src_dir / "model.safetensors").write_bytes(b"x")

    calls = []

    def _fake_ensure(llamacpp_dir, source, out_dir, model_name=None):
        calls.append((llamacpp_dir, source, out_dir))
        return str(tmp_path / "model-bf16.gguf")

    monkeypatch.setattr(fake_orchestrator, "_ensure_bf16_gguf", _fake_ensure)
    cfg_path = _write_cfg(tmp_path, src_dir, measured=False)

    fake_orchestrator.run(str(cfg_path))

    assert len(calls) == 1
    assert calls[0][1] == str(src_dir)


def test_entry_converts_directory_source_when_measured_too(
    fake_orchestrator, tmp_path, monkeypatch,
):
    """Same as above with measured=True -- conversion must still happen (this
    was already true pre-change; guards against a refactor accidentally
    dropping the measured=True case)."""
    src_dir = tmp_path / "merged_model"
    src_dir.mkdir()
    (src_dir / "model.safetensors").write_bytes(b"x")

    calls = []

    def _fake_ensure(llamacpp_dir, source, out_dir, model_name=None):
        calls.append((llamacpp_dir, source, out_dir))
        return str(tmp_path / "model-bf16.gguf")

    monkeypatch.setattr(fake_orchestrator, "_ensure_bf16_gguf", _fake_ensure)
    cfg_path = _write_cfg(tmp_path, src_dir, measured=True)

    fake_orchestrator.run(str(cfg_path))

    assert len(calls) == 1


def test_entry_dir_source_llamacpp_unavailable_warns_and_falls_back(
    fake_orchestrator, tmp_path, monkeypatch, capsys,
):
    """When llama.cpp discovery/install fails (ensure_llamacpp -> None), a
    directory source must be passed straight through (old pre-Change-1
    behavior) with a loud warning naming the SafetensorsSource-drift risk --
    not a hard failure (MagicQuant's own arch gate is the backstop)."""
    monkeypatch.setattr(fake_orchestrator, "ensure_llamacpp", lambda hint="": None)
    src_dir = tmp_path / "merged_model"
    src_dir.mkdir()
    (src_dir / "model.safetensors").write_bytes(b"x")
    cfg_path = _write_cfg(tmp_path, src_dir, measured=False)

    fake_orchestrator.run(str(cfg_path))

    out = capsys.readouterr().out
    assert "WARNING: llama.cpp unavailable" in out
    assert "SafetensorsSource" in out
    inst = _FakeOrchestrator.instances[-1]
    assert inst.init_kwargs["source_model_path"] == str(src_dir)


def test_magicquant_entry_run_body_converts_before_measured_only_gate_removed():
    """Static guard: the conversion call in run() must no longer be gated
    behind `if measured and ...` -- regression guard for Change 1 in case a
    future edit reintroduces the old gate."""
    src = (ROOT / "core" / "_magicquant_entry.py").read_text()
    run_body = src[src.index("def run("):src.index('if __name__ == "__main__"')]
    assert "if measured and llamacpp" not in run_body
    assert "should_convert_source_to_gguf(is_dir_source, llamacpp)" in run_body


# ── Change 2: post-generation PPL smoke gate ─────────────────────────────────

def test_entry_aborts_when_ppl_smoke_fails(fake_orchestrator, tmp_path, monkeypatch):
    src_file = tmp_path / "src.safetensors"
    src_file.write_bytes(b"x")
    cfg_path = _write_cfg(tmp_path, src_file, measured=False)
    monkeypatch.setattr(ppl_smoke, "smoke_test_gguf", lambda *a, **k: False)

    with pytest.raises(SystemExit) as exc:
        fake_orchestrator.run(str(cfg_path))
    assert exc.value.code == 1


def test_entry_completes_when_ppl_smoke_passes(fake_orchestrator, tmp_path, monkeypatch):
    src_file = tmp_path / "src.safetensors"
    src_file.write_bytes(b"x")
    cfg_path = _write_cfg(tmp_path, src_file, measured=False)
    monkeypatch.setattr(ppl_smoke, "smoke_test_gguf", lambda *a, **k: True)

    fake_orchestrator.run(str(cfg_path))  # must not raise


def test_entry_ppl_smoke_gate_skips_when_no_binary_found(
    fake_orchestrator, tmp_path, capsys,
):
    """Default fixture state: llamacpp="/fake/llamacpp" doesn't exist on disk,
    so find_perplexity_bin resolves nothing -- the gate must skip (advisory),
    not fail the stage, when nothing is monkeypatched."""
    src_file = tmp_path / "src.safetensors"
    src_file.write_bytes(b"x")
    cfg_path = _write_cfg(tmp_path, src_file, measured=False)

    fake_orchestrator.run(str(cfg_path))  # must not raise

    out = capsys.readouterr().out
    assert "PPL smoke test SKIPPED" in out
    assert "PIPELINE_STAGE_COMPLETE=magicquant" in out


def test_magicquant_entry_run_body_smoke_gate_before_stage_complete():
    """Static guard: the smoke gate must run (and be able to sys.exit(1))
    strictly before the PIPELINE_STAGE_COMPLETE marker prints."""
    src = (ROOT / "core" / "_magicquant_entry.py").read_text()
    run_body = src[src.index("def run("):src.index('if __name__ == "__main__"')]
    assert "ppl_smoke.smoke_test_gguf" in run_body
    gate_idx = run_body.index("ppl_smoke.smoke_test_gguf")
    assert "sys.exit(1)" in run_body[gate_idx:]
    assert gate_idx < run_body.index('print("PIPELINE_STAGE_COMPLETE=magicquant"')


# ── resolve_source: LoRA-adapter-dir disclosure (rsLoRA audit, 2026-07-28) ──
#
# resolve_source()'s plain-safetensors-dir fallback
# (``any(c.glob("*.safetensors"))``) also matches a LoRA ADAPTER directory
# (adapter_model.safetensors + adapter_config.json) -- adapter_model.safetensors
# IS a *.safetensors file. Behavior must stay the same (still return the dir,
# not refuse it -- MagicQuant's own open_model_source() legitimately supports
# LoRA-merge-on-read against the adapter), but silently treating an unmerged
# adapter as a full model is a footgun: a user who forgot the export stage
# gets no signal. resolve_source() now calls _disclose_if_lora_adapter_dir()
# to print a loud NOTE naming the base model (from adapter_config.json's
# base_model_name_or_path) it will merge against.

def test_resolve_source_discloses_lora_adapter_dir(tmp_path, capsys):
    out = tmp_path / "output"
    out.mkdir()
    (out / "adapter_model.safetensors").write_bytes(b"x")
    (out / "adapter_config.json").write_text(json.dumps({
        "r": 16, "lora_alpha": 32, "base_model_name_or_path": "/models/base-model",
    }))

    import _magicquant_entry as entry

    result = entry.resolve_source("", out, str(tmp_path))

    # Behavior-preserving: the adapter dir is still returned, not refused.
    assert result == str(out)
    err_out = capsys.readouterr().out
    assert "LoRA ADAPTER" in err_out
    assert "/models/base-model" in err_out
    assert str(out) in err_out


def test_resolve_source_discloses_lora_adapter_dir_with_unresolvable_base_model(
    tmp_path, capsys,
):
    """adapter_config.json missing/blank base_model_name_or_path still gets a
    loud NOTE -- just naming the base model as unresolved instead of silently
    omitting it or crashing."""
    out = tmp_path / "output"
    out.mkdir()
    (out / "adapter_model.safetensors").write_bytes(b"x")
    (out / "adapter_config.json").write_text(json.dumps({"r": 8, "lora_alpha": 16}))

    import _magicquant_entry as entry

    result = entry.resolve_source("", out, str(tmp_path))

    assert result == str(out)
    err_out = capsys.readouterr().out
    assert "LoRA ADAPTER" in err_out
    assert "unknown" in err_out.lower()


def test_resolve_source_plain_safetensors_dir_has_no_disclosure(tmp_path, capsys):
    """The common case (a real merged-model dir, no adapter_config.json)
    must NOT print the adapter disclosure."""
    out = tmp_path / "output"
    out.mkdir()
    (out / "model.safetensors").write_bytes(b"x")

    import _magicquant_entry as entry

    result = entry.resolve_source("", out, str(tmp_path))

    assert result == str(out)
    err_out = capsys.readouterr().out
    assert "LoRA ADAPTER" not in err_out
