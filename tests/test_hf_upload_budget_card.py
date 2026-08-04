"""Model-card disclosure for size-target ("budget") builds.

A budget file (matched by ``core.publish_criteria.BUDGET_FILE_RE``, Task 4)
claims a requested GiB SIZE via its filename, not a named Q4/Q5/Q6 band.
This card section must show: the file itself, the requested budget, the
achieved size, and -- only when Task 1's interchange block is present in
``<out>/magicquant/search_results.json`` -- the measured perplexity and its
baseline. PPL must come ONLY from that block (``tiered[<BUDGET-NGiB
key>]["ppl"]`` / ``["baseline_ppl"]``), never from ``v2_results.json`` or
``frontier.json``, both of which the cleanup stage deletes; when the block
is absent the PPL line is simply omitted, never fabricated.

Separately, a build-time size-guard refusal (``rule == "budget"``, carrying
``requested_budget_gib`` + ``predicted_gib`` with the band fields empty --
the ``_refusals.json`` record shape a future size-prediction guard would
write) must render as SIZE OVERSHOOT prose, never the "would land in band X"
prose used for an actual tier (band) refusal. That phrasing decision must
live in core/hf_upload.py (tracked), not in the gitignored
output/_publish_tiers.py -- publishing logic living there untested is
exactly what let four model-card bugs reach public pages in 2026-08.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core"))

from hf_upload import HFUploadConfig, audit_card_against_repo, generate_model_card

_96_GIB = 96 * 1024 ** 3


def _fake_gguf(path: Path, size_bytes: int = _96_GIB) -> Path:
    """A sparse file of the given size -- stat() reports it accurately
    without actually allocating/writing gigabytes of real bytes (which
    hung the test run the first time this was tried with b"x" * N)."""
    with open(path, "wb") as f:
        f.truncate(size_bytes)
    return path


def _cfg(**overrides):
    base = dict(
        repo_id="user/model-MagicQuant-GGUF",
        base_model="org/base",
        dataset_name="mydata",
        did_training=True,
        did_magicquant=True,
    )
    base.update(overrides)
    return HFUploadConfig(**base)


def _bullet_lines(section: str) -> list:
    """The "- **tier** -- reason" bullet lines of a refusal/drop section,
    excluding the closing paragraph -- which legitimately uses words like
    "band" to explain what a budget refusal ISN'T, so checks that must never
    see "band" belong on the bullet, not the whole section."""
    return [l for l in section.strip().splitlines() if l.startswith("- **")]


def _write_interchange_block(out_dir: Path, key: str, ppl: float, baseline_ppl: float):
    """Hand-built fixture matching Task 1's write_interchange_block shape
    (magicquant.v2.interchange) -- built locally rather than imported, since
    this task must not cross-import Task 1's module for its fixtures."""
    results_path = out_dir / "magicquant" / "search_results.json"
    results_path.parent.mkdir(parents=True, exist_ok=True)
    results_path.write_text(json.dumps({
        "tier_scheme_version": 2,
        "tiered": {
            key: {
                "config": {"E": "Q8_0"},
                "tensor_config": {"blk.0.attn_q.weight": "Q4_K"},
                "tensor_actual_types": {"blk.0.attn_q.weight": "Q4_K"},
                "algo": "v2-budget",
                "budget_bytes": 107374182400,
                "predicted_bytes": 103079215104,
                "actual_bytes": 103079215104,
                "ppl": ppl,
                "baseline_ppl": baseline_ppl,
            }
        },
    }))
    return results_path


# ── Size-target section: filename, requested budget, achieved size ─────────

def test_budget_file_gets_size_target_section(tmp_path):
    mq_dir = tmp_path / "magicquant"
    mq_dir.mkdir()
    p = _fake_gguf(mq_dir / "Model-BUDGET-100GiB.gguf")  # 96 GiB sparse, exercises real bytes math
    card = generate_model_card(_cfg(), [(p, "Model-BUDGET-100GiB.gguf")])

    assert "## Size-Target Build" in card
    # "100 GiB"/"96.00 GiB" also appear in the GGUF table's quant-hint cell
    # ("Size-target build (~100 GiB requested)"), so checking the whole card
    # would pass even with the section's own bullets deleted. Isolate the
    # section itself, the way the refusal tests below correctly do.
    section = card.split("## Size-Target Build", 1)[1].split("\n## ", 1)[0]
    assert "Model-BUDGET-100GiB.gguf" in section
    assert "100 GiB" in section             # requested figure, from the filename
    assert "96.00 GiB" in section           # achieved size, from real file bytes


# ── PPL sourcing: interchange block ONLY, never fabricated ─────────────────

def test_budget_ppl_row_from_interchange_block(tmp_path):
    mq_dir = tmp_path / "magicquant"
    mq_dir.mkdir()
    p = _fake_gguf(mq_dir / "Model-BUDGET-100GiB.gguf")
    _write_interchange_block(tmp_path, "BUDGET-100GiB", ppl=7.1234, baseline_ppl=6.9001)

    card = generate_model_card(_cfg(), [(p, "Model-BUDGET-100GiB.gguf")])

    assert "Measured perplexity" in card
    assert "7.1234" in card
    assert "6.9001" in card
    delta = (7.1234 / 6.9001 - 1) * 100
    assert f"{delta:+.2f}%" in card         # exact delta, computed the same way the card does


def test_budget_ppl_row_ignores_v2_results_and_frontier_json(tmp_path):
    """Even if v2_results.json / frontier.json are sitting right there with a
    PPL number, they must never be read -- cleanup deletes them, and the
    interchange block is the only trustworthy source."""
    mq_dir = tmp_path / "magicquant"
    mq_dir.mkdir()
    p = _fake_gguf(mq_dir / "Model-BUDGET-100GiB.gguf")
    (mq_dir / "v2_results.json").write_text(json.dumps({"ppl": 1.2345}))
    (mq_dir / "frontier.json").write_text(json.dumps({"ppl": 9.8765}))
    # No search_results.json at all -- the ONLY legitimate source is absent.

    card = generate_model_card(_cfg(), [(p, "Model-BUDGET-100GiB.gguf")])

    assert "1.2345" not in card
    assert "9.8765" not in card
    assert "Measured perplexity" not in card


def test_budget_ppl_row_omitted_when_no_records(tmp_path):
    mq_dir = tmp_path / "magicquant"
    mq_dir.mkdir()
    p = _fake_gguf(mq_dir / "Model-BUDGET-100GiB.gguf")
    # No search_results.json anywhere.

    card = generate_model_card(_cfg(), [(p, "Model-BUDGET-100GiB.gguf")])

    assert "## Size-Target Build" in card    # section still renders...
    section = card.split("## Size-Target Build", 1)[1].split("\n## ", 1)[0]
    assert "100 GiB" in section
    assert "96.00 GiB" in section
    assert "Measured perplexity" not in section  # ...just without a PPL line


# ── audit_card_against_repo: existing filename-mention rule ────────────────

def test_audit_flags_undisclosed_budget_file():
    card = generate_model_card(_cfg(), [])   # no files -- nothing mentioned
    repo_files = ["Model-BUDGET-100GiB.gguf", "README.md"]
    warnings = audit_card_against_repo(card, repo_files, log=lambda *a, **k: None)
    assert any("Model-BUDGET-100GiB.gguf" in w and "never mentions it" in w
               for w in warnings), warnings


def test_audit_passes_when_disclosed(tmp_path):
    mq_dir = tmp_path / "magicquant"
    mq_dir.mkdir()
    p = _fake_gguf(mq_dir / "Model-BUDGET-100GiB.gguf")
    card = generate_model_card(_cfg(), [(p, "Model-BUDGET-100GiB.gguf")])

    repo_files = ["Model-BUDGET-100GiB.gguf", "README.md"]
    warnings = audit_card_against_repo(card, repo_files, log=lambda *a, **k: None)
    assert not any("Model-BUDGET-100GiB.gguf" in w for w in warnings), warnings


# ── Budget refusal: size overshoot prose, never band prose ─────────────────

def test_budget_refusal_rendered_as_size_overshoot():
    cfg = _cfg(refused_tiers=[
        # An ordinary tier (band) refusal -- the phrasing this must NOT reuse.
        {"tier": "Q5", "family": "rocmfpx", "rule": "band",
         "reason": "rendering MagicQuant's Q5 config into ROCmFPX types "
                    "predicts 14.10 GiB against an 11.50 GiB BF16 baseline, "
                    "which is the Q6 band, not Q5."},
        # Task 6's budget refusal shape: requested_budget_gib + predicted_gib
        # present, band fields empty. "reason" is deliberately band-flavored
        # text that a correct implementation must NOT trust verbatim -- the
        # phrasing decision belongs to hf_upload.py, computed from the GiB
        # numbers, not passed through from whatever produced the record.
        {"tier": None, "family": "magicquant", "rule": "budget",
         "requested_budget_gib": 100.0, "predicted_gib": 118.4,
         "predicted_band": None, "claimed_band": None,
         "reason": "would land in the wrong band"},
    ])
    card = generate_model_card(cfg, [], rocmfpx=True)

    section = card.split("## Tiers this build does not produce", 1)[1]
    section = section.split("\n## ", 1)[0]
    lines = [l for l in section.strip().splitlines() if l.startswith("- **")]
    band_line = next(l for l in lines if l.startswith("- **Q5**"))
    budget_line = next(l for l in lines if l is not band_line)

    # The band refusal keeps its band-shaped explanation.
    assert "band" in band_line.lower()

    # The budget refusal renders the GiB numbers as a SIZE OVERSHOOT, and
    # never mentions a band, and never reuses the record's own (band-flavored)
    # "reason" text verbatim.
    assert "100" in budget_line
    assert "118.4" in budget_line
    assert "overshoot" in budget_line.lower()
    assert "band" not in budget_line.lower()
    assert "would land in" not in budget_line.lower()
    assert "wrong band" not in budget_line.lower()


def test_budget_refusal_without_gib_fields_falls_back_without_band_prose():
    """A malformed/incomplete budget-refusal record (missing the GiB fields)
    must still never fall through to band phrasing -- worst case it uses the
    record's own reason text or a generic budget-flavored fallback."""
    cfg = _cfg(refused_tiers=[
        {"tier": None, "family": "magicquant", "rule": "budget",
         "predicted_band": None, "claimed_band": None},
    ])
    card = generate_model_card(cfg, [], rocmfpx=True)
    section = card.split("## Tiers this build does not produce", 1)[1]
    section = section.split("\n## ", 1)[0]
    assert "would land in" not in section.lower()
    assert "budget" in section.lower()


def test_budget_refusal_shipped_shape_renders_size_overshoot():
    """Minor 2: the sparse fixtures above use tier=None and None bands. The
    real producer's shape (Task 6's size-prediction guard) is different --
    tier is a "BUDGET-<N>GiB" label, and unclaimed band fields are "" not
    None. Cover that shape too, not just the placeholder."""
    cfg = _cfg(refused_tiers=[
        {"tier": "BUDGET-100GiB", "family": "magicquant", "rule": "budget",
         "requested_budget_gib": 100.0, "predicted_gib": 118.4,
         "predicted_band": "", "claimed_band": "",
         "reason": "would land in the wrong band"},
    ])
    card = generate_model_card(cfg, [], rocmfpx=True)
    section = card.split("## Tiers this build does not produce", 1)[1]
    section = section.split("\n## ", 1)[0]
    budget_line = _bullet_lines(section)[0]
    assert "100" in budget_line
    assert "118.4" in budget_line
    assert "overshoot" in budget_line.lower()
    assert "band" not in budget_line.lower()
    assert "would land in" not in budget_line.lower()


# ── Important 2: pin the rule-aware closing itself -- the existing mixed-set
# test above never exercises it (it only checks the per-row bullets), so
# deleting the if/else entirely still passed all tests. ────────────────────

def test_all_budget_refusal_closing_pins_budget_wording():
    cfg = _cfg(refused_tiers=[
        {"tier": "BUDGET-100GiB", "family": "magicquant", "rule": "budget",
         "requested_budget_gib": 100.0, "predicted_gib": 118.4,
         "predicted_band": "", "claimed_band": "",
         "reason": "predicted to exceed the requested budget"},
    ])
    card = generate_model_card(cfg, [], rocmfpx=True)
    section = card.split("## Tiers this build does not produce", 1)[1]
    section = section.split("\n## ", 1)[0]
    assert "may fit within tolerance" in section
    assert "will not appear in a later build" not in section


def test_mixed_refusal_closing_is_neutral_not_a_permanence_claim():
    """Important 1: a refused_tiers set mixing a permanent (band) refusal
    with a budget size-overshoot refusal must use NEITHER single-rule
    closing -- each asserts something false for the other kind in the same
    list (the permanence closing says a fresh search can't help; the budget
    closing implies a scheme-rounding gap might just need a retry)."""
    cfg = _cfg(refused_tiers=[
        {"tier": "Q5", "family": "rocmfpx", "rule": "band",
         "reason": "predicts 14.10 GiB, which is the Q6 band, not Q5."},
        {"tier": "BUDGET-100GiB", "family": "magicquant", "rule": "budget",
         "requested_budget_gib": 100.0, "predicted_gib": 118.4,
         "predicted_band": "", "claimed_band": "",
         "reason": "would land in the wrong band"},
    ])
    card = generate_model_card(cfg, [], rocmfpx=True)
    section = card.split("## Tiers this build does not produce", 1)[1]
    section = section.split("\n## ", 1)[0]
    assert "will not appear in a later build" not in section
    assert "may fit within tolerance" not in section
    assert "not built for different reasons" in section


# ── CRITICAL fix: the card must consume core.publish_records.read_refusals
# directly, not depend solely on whatever output/_publish_tiers.py's own
# (buggy, field-projecting) find_refusals() forwarded through cfg. ─────────

def test_card_auto_discovers_refusals_from_disk_when_cfg_omits_them(tmp_path):
    mq_dir = tmp_path / "magicquant"
    mq_dir.mkdir()
    p = _fake_gguf(mq_dir / "Model-BUDGET-100GiB.gguf")
    (mq_dir / "_refusals.json").write_text(json.dumps([
        {"tier": "BUDGET-100GiB", "family": "magicquant", "rule": "budget",
         "requested_budget_gib": 100.0, "predicted_gib": 118.4,
         "predicted_band": "", "claimed_band": "",
         "reason": "would land in the wrong band"},
    ]))
    # cfg carries NO refused_tiers at all -- the on-disk record is the only
    # source, exactly as a real publish run's card generation would see it
    # if wired directly to the tracked reader instead of the buggy script.
    card = generate_model_card(
        _cfg(), [(p, "Model-BUDGET-100GiB.gguf")], rocmfpx=True)

    assert "## Tiers this build does not produce" in card
    section = card.split("## Tiers this build does not produce", 1)[1]
    section = section.split("\n## ", 1)[0]
    budget_line = _bullet_lines(section)[0]
    assert "118.4" in budget_line
    assert "overshoot" in budget_line.lower()
    assert "band" not in budget_line.lower()


def test_card_prefers_disk_refusal_over_field_impoverished_cfg_entry(tmp_path):
    """The exact shape of the CRITICAL bug: cfg carries a NON-empty
    refused_tiers list for this tier (as output/_publish_tiers.py's buggy
    find_refusals() would produce -- no "rule" key), while the on-disk
    record for the SAME tier has the full fields. The disk record must win,
    or the budget branch stays unreachable behind a cfg entry that merely
    exists but is missing what the branch checks for."""
    mq_dir = tmp_path / "magicquant"
    mq_dir.mkdir()
    p = _fake_gguf(mq_dir / "Model-BUDGET-100GiB.gguf")
    (mq_dir / "_refusals.json").write_text(json.dumps([
        {"tier": "BUDGET-100GiB", "family": "magicquant", "rule": "budget",
         "requested_budget_gib": 100.0, "predicted_gib": 118.4,
         "predicted_band": "", "claimed_band": "",
         "reason": "would land in the wrong band"},
    ]))
    cfg = _cfg(refused_tiers=[
        # output/_publish_tiers.py's projected shape: no "rule" key at all.
        {"tier": "BUDGET-100GiB", "family": "magicquant",
         "reason": "would land in the wrong band"},
    ])
    card = generate_model_card(
        cfg, [(p, "Model-BUDGET-100GiB.gguf")], rocmfpx=True)

    section = card.split("## Tiers this build does not produce", 1)[1]
    section = section.split("\n## ", 1)[0]
    budget_line = _bullet_lines(section)[0]
    assert "overshoot" in budget_line.lower()
    assert "band" not in budget_line.lower()
