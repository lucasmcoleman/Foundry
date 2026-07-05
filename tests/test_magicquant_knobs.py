"""MagicQuant orchestrator knobs (use_imatrix/imatrix_corpus/enable_kl/kl_weight/
enable_speed_bench) wired end-to-end: dataclass -> CLI -> service -> entry ->
UI config model -> UI card.

Mirrors the existing rocmfpx_schemes/iq_schemes/seed wiring and its test
coverage style (tests/test_qat_service.py's CLI/UIConfig checks,
tests/test_stage_entries.py's build_config checks).
"""

import json
import sys
import types
from pathlib import Path

import pytest

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

    # All five flags flipped on.
    pl.main([
        "--model", "org/m", "--no-export", "--no-heretic", "--no-reap",
        "--magicquant-use-imatrix", "--magicquant-imatrix-corpus", "corpus.txt",
        "--magicquant-kl", "--magicquant-kl-weight", "0.42",
        "--magicquant-speed-bench",
    ])
    mc = captured["cfg"].magicquant
    assert mc.use_imatrix is True
    assert mc.imatrix_corpus == "corpus.txt"
    assert mc.enable_kl is True
    assert mc.kl_weight == 0.42
    assert mc.enable_speed_bench is True


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


# ── core/pipeline.py: cfg_hash includes the new knobs (cache invalidation) ────

def test_stage_magicquant_hash_source_includes_new_knobs():
    """Source-level check that stage_magicquant's cfg_hash dict was extended
    (a functional test would need a full Artifacts/subprocess harness; the
    existing rocmfpx_schemes/iq_schemes wiring is checked the same way in
    tests/test_stage_cleanup.py's source-text assertions)."""
    src = (ROOT / "core" / "pipeline.py").read_text()
    hash_block = src[src.index('def stage_magicquant'):src.index('existing = sorted(artifacts.magicquant_dir')]
    for key in ("use_imatrix", "imatrix_corpus", "enable_kl", "kl_weight", "enable_speed_bench"):
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
    )
    assert cfg["use_imatrix"] is True
    assert cfg["imatrix_corpus"] == "calib.txt"
    assert cfg["enable_kl"] is True
    assert cfg["kl_weight"] == 0.42
    assert cfg["enable_speed_bench"] is True
    assert json.loads(json.dumps(cfg)) == cfg


def test_magicquant_build_config_new_knobs_default():
    svc = MagicQuantService(ROOT, "python")
    cfg = svc.build_config(
        llamacpp_hint="", pipeline_root_str="/repo", mq_source_override="/src",
        out_abs_str="/o", generations=5, population_size=10,
        target_base_quant="IQ4_NL", tiers_json="{}", model_name="m",
    )
    assert cfg["use_imatrix"] is False
    assert cfg["imatrix_corpus"] is None
    assert cfg["enable_kl"] is False
    assert cfg["kl_weight"] == 0.1
    assert cfg["enable_speed_bench"] is False


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
        seed=None, use_imatrix=False, imatrix_corpus=None,
    ):
        self.run_full_search_kwargs = dict(
            target_base_quant=target_base_quant, max_generations=max_generations,
            population_size=population_size, verbose=verbose, patience=patience,
            enable_rocmfpx=enable_rocmfpx, enable_iq=enable_iq, seed=seed,
            use_imatrix=use_imatrix, imatrix_corpus=imatrix_corpus,
        )
        return [], {"Q4": {"config": {}}}

    def run_measured_search(
        self, target_base_quant="MXFP4_MOE", search_generations=30, population_size=80,
        measurement_rounds=3, candidates_per_round=4, verbose=True, patience=None,
        enable_rocmfpx=False, enable_iq=False, seed=None, use_imatrix=False,
        imatrix_corpus=None, enable_kl=False, kl_weight=0.1, enable_speed_bench=False,
    ):
        self.run_measured_search_kwargs = dict(
            target_base_quant=target_base_quant, search_generations=search_generations,
            population_size=population_size, measurement_rounds=measurement_rounds,
            candidates_per_round=candidates_per_round, verbose=verbose, patience=patience,
            enable_rocmfpx=enable_rocmfpx, enable_iq=enable_iq, seed=seed,
            use_imatrix=use_imatrix, imatrix_corpus=imatrix_corpus, enable_kl=enable_kl,
            kl_weight=kl_weight, enable_speed_bench=enable_speed_bench,
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
        entry, "_ensure_bf16_gguf", lambda llamacpp_dir, source, out_dir: source
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
    )

    fake_orchestrator.run(str(cfg_path))

    inst = _FakeOrchestrator.instances[-1]
    assert inst.run_measured_search_kwargs is None
    kw = inst.run_full_search_kwargs
    assert kw is not None
    assert kw["use_imatrix"] is True
    assert kw["imatrix_corpus"] == "calib.txt"


def test_entry_run_measured_search_gets_all_five_knobs(fake_orchestrator, tmp_path):
    src_file = tmp_path / "src.safetensors"
    src_file.write_bytes(b"x")
    cfg_path = _write_cfg(
        tmp_path, src_file,
        measured=True, use_imatrix=True, imatrix_corpus="calib.txt",
        enable_kl=True, kl_weight=0.42, enable_speed_bench=True,
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


# ── ui/app.py: MagicQuantCfg pydantic model ───────────────────────────────────

def test_magicquantcfg_new_knob_defaults():
    import app as app_module

    c = app_module.MagicQuantCfg()
    assert c.use_imatrix is False
    assert c.imatrix_corpus is None
    assert c.enable_kl is False
    assert c.kl_weight == 0.1
    assert c.enable_speed_bench is False


def test_magicquantcfg_accepts_new_knobs():
    import app as app_module

    c = app_module.MagicQuantCfg(
        use_imatrix=True, imatrix_corpus="calib.txt",
        enable_kl=True, kl_weight=0.3, enable_speed_bench=True,
    )
    assert c.use_imatrix is True
    assert c.imatrix_corpus == "calib.txt"
    assert c.enable_kl is True
    assert c.kl_weight == 0.3
    assert c.enable_speed_bench is True


def test_do_magicquant_hash_source_includes_new_knobs():
    src = (ROOT / "ui" / "app.py").read_text()
    hash_block = src[src.index("async def do_magicquant"):src.index("existing_ggufs = sorted(mq_dir.glob")]
    for key in ("use_imatrix", "imatrix_corpus", "enable_kl", "kl_weight", "enable_speed_bench"):
        assert f'"{key}": mc.{key}' in hash_block, key
    assert "use_imatrix=mc.use_imatrix" in src
    assert "imatrix_corpus=mc.imatrix_corpus" in src
    assert "enable_kl=mc.enable_kl" in src
    assert "kl_weight=mc.kl_weight" in src
    assert "enable_speed_bench=mc.enable_speed_bench" in src


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


def test_index_html_gates_kl_and_speed_bench_behind_measured():
    """KL and speed-bench controls must only render when c.measured is true;
    use_imatrix/imatrix_corpus are NOT measured-gated (run_full_search also
    accepts them, per the entry.py wiring)."""
    src = (ROOT / "ui" / "index.html").read_text()
    fn_start = src.index("function renderMagicQuant()")
    fn_end = src.index("function renderROCmFPX()")
    body = src[fn_start:fn_end]
    gated = body[body.index("c.measured ?"):]
    assert "magicquant.enable_kl" in gated
    assert "magicquant.kl_weight" in gated
    assert "magicquant.enable_speed_bench" in gated
    ungated = body[:body.index("c.measured ?")]
    assert "magicquant.use_imatrix" in ungated
    assert "magicquant.imatrix_corpus" in ungated


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
