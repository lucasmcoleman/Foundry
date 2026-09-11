"""Field report Issue B, defect 1: silent marker writes.

Every do_* stage runner used to write its completion marker inside a bare
``try: markers.write_marker(...) except OSError: pass`` -- non-fatal (a
marker failure must never fail an otherwise-successful stage) but SILENT: a
successful Aug 9 export produced NO marker with nothing in the logs to
explain why, forcing a spurious re-export that was undiagnosable after the
fact. The same pattern repeated at all 7 marker-write sites (training,
export, heretic, reap, qat, magicquant, rocmfpx).

Fixed by extracting ONE tiny async helper, ``_write_stage_marker``, used by
all 7 sites -- extracted rather than fixing the 7 call sites identically so
this logging can never drift out of sync between stages again, and so it
only needs testing once (parametrized over the 7 stage names) plus a source
guard proving every site actually calls it.
"""
import asyncio
from pathlib import Path

import pytest

import app as app_module  # ui/app.py (ui is on sys.path via conftest)

ROOT = Path(__file__).resolve().parent.parent

STAGE_NAMES = ["training", "export", "heretic", "reap", "qat", "magicquant", "rocmfpx"]


@pytest.fixture
def captured_logs(monkeypatch):
    calls = []

    async def fake_log(text, level="info"):
        calls.append((text, level))

    monkeypatch.setattr(app_module.state, "log", fake_log)
    return calls


def _write(stage_dir, stage, key_file, cfg_hash):
    asyncio.run(app_module._write_stage_marker(stage_dir, stage, key_file, cfg_hash))


@pytest.mark.parametrize("stage", STAGE_NAMES)
def test_write_stage_marker_logs_info_on_success(stage, tmp_path, captured_logs):
    stage_dir = tmp_path / stage
    stage_dir.mkdir()
    key_file = stage_dir / "artifact.bin"
    key_file.write_bytes(b"x")

    _write(stage_dir, stage, key_file, "cfghash")

    # The marker was actually written -- the non-fatal/logged fix must not
    # have turned into a silent no-op in the other direction.
    assert app_module.markers.read_marker(stage_dir) is not None
    assert len(captured_logs) == 1
    text, level = captured_logs[0]
    assert level == "info"
    assert stage in text
    assert str(stage_dir) in text


@pytest.mark.parametrize("stage", STAGE_NAMES)
def test_write_stage_marker_logs_warning_on_oserror_and_stays_non_fatal(
    stage, tmp_path, captured_logs, monkeypatch,
):
    stage_dir = tmp_path / stage  # deliberately never created

    def _boom(*a, **k):
        raise OSError("disk full (simulated)")

    monkeypatch.setattr(app_module.markers, "write_marker", _boom)

    # Must not raise: a marker failure must never fail an otherwise-
    # successful stage (this is the whole point of keeping it non-fatal).
    _write(stage_dir, stage, stage_dir / "artifact.bin", "cfghash")

    assert len(captured_logs) == 1
    text, level = captured_logs[0]
    assert level == "warn"
    assert stage in text
    assert str(stage_dir) in text
    assert "disk full" in text


def test_all_seven_call_sites_route_through_the_shared_helper():
    """Source-inspection guard: extracting one helper only actually closes
    the silent-failure hole if every one of the 7 stage runners actually
    reaches it through artifact validation. Guards against a future edit that reintroduces a
    bare try/except at even one site -- or a direct markers.write_marker()
    call bypassing the helper -- is caught.

    The bare-except check is scoped to each do_*'s own function body -- the
    actual marker-site neighborhood -- rather than the whole file: a
    whole-file scan would also flag any FUTURE, unrelated
    `except OSError:` elsewhere in app.py that has nothing to do with
    marker writes, failing this test for a reason outside what it claims
    to guard (and, before this scoping, it depended on this module's own
    docstrings never literally spelling out the old pattern with a
    trailing colon at end-of-line).
    """
    src = (ROOT / "ui" / "app.py").read_text(encoding="utf-8")

    def _body(fn_name: str) -> str:
        start = src.index(f"async def {fn_name}")
        end = src.index("\nasync def ", start + 1)
        return src[start:end]

    for stage in STAGE_NAMES:
        body = _body(f"do_{stage}")
        assert "await _finish_artifact_stage(" in body, stage
        # Each site hands off to the shared helper -- it must not also call
        # markers.write_marker() directly (that would bypass the logging).
        assert "markers.write_marker(" not in body, stage
        bare_except_lines = [ln for ln in body.splitlines() if ln.strip() == "except OSError:"]
        assert bare_except_lines == [], stage

    assert _body("_finish_artifact_stage").count("await _write_stage_marker(") == 1

    # Exactly one direct call to markers.write_marker in the whole file --
    # inside the shared helper itself, outside all 7 bodies checked above.
    assert src.count("markers.write_marker(") == 1
