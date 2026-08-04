"""The UI model must accept budget_gib and hand it to the service layer —
the measured+budget refusal then fires in build_config for UI runs too
(single choke point, tested in test_magicquant_budget_config.py)."""
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi")

from ui.app import MagicQuantCfg

ROOT = Path(__file__).resolve().parent.parent


def test_cfg_accepts_budget_gib():
    assert MagicQuantCfg().budget_gib is None
    assert MagicQuantCfg(budget_gib=12.5).budget_gib == 12.5


def test_index_html_has_the_input_and_default():
    html = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
    assert 'data-key="magicquant.budget_gib"' in html
    assert "budget_gib: null" in html
    # reviewer-caught: without this, the mq-budget preset ships CLI-only —
    # the ROCmFPX card's format tag list has to know about it too.
    assert "'mq-budget'" in html


def test_do_magicquant_passes_budget_gib_to_build_script():
    """do_magicquant is async and talks to a live WebSocket/state singleton,
    so (matching the existing no-subprocess pattern in
    test_magicquant_knobs.py's test_do_magicquant_hash_source_includes_*)
    this source-inspects the function body rather than invoking it.

    Two things must both hold, and each is independently falsifiable by a
    plausible mutation:
    - budget_gib is threaded into the resume/skip config_hash dict (mc.budget_gib
      omitted there would make a stage with a changed budget_gib wrongly report
      "already complete" against a marker written under a different budget).
    - budget_gib is forwarded into the svc.build_script(...) call (omitted
      there, the field would sit on the pydantic model and do nothing).
    """
    src = (ROOT / "ui" / "app.py").read_text(encoding="utf-8")
    do_magicquant_start = src.index("async def do_magicquant")
    do_magicquant_body = src[do_magicquant_start:src.index("\nasync def ", do_magicquant_start + 1)]

    hash_block = do_magicquant_body[:do_magicquant_body.index("existing_ggufs = sorted(mq_dir.glob")]
    assert '"budget_gib": mc.budget_gib' in hash_block

    build_script_call = do_magicquant_body[do_magicquant_body.index("svc.build_script("):do_magicquant_body.index("rc = await run_script(script, out)")]
    assert "budget_gib=mc.budget_gib" in build_script_call
