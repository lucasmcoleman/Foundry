"""Publishing decision logic must be right, because it was untested for
months: it lived in ``output/_publish_tiers.py``, which is gitignored (see
CLAUDE.md's ``output/`` policy), so none of band/dominance/speed ever ran
under pytest. None of the four model-card bugs of 2026-08 was itself a wrong
band/dominance/speed verdict -- those were disclosure, reconcile and rendering
bugs, covered by tests/test_card_repo_consistency.py and
tests/test_rocmfpx_refusal_record.py. What this file covers is the condition
that let them ship unnoticed: the rules deciding what reaches a public page
were unreachable from pytest. It also pins the two failures the decision
logic CAN produce on its own -- a rejection explained by a rule that never
fired, and a question that contradicts the drop it accompanies.

Two of the cases below are regression pins on real numbers from the
FableFusion and ThinkingCap runs (see MEMORY.md / the module docstring),
because a synthetic "some tier beats another tier" test would not have
caught the actual defect: a tie that is *technically* in the bigger tier's
favor is still noise below the floor, not a verdict.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core"))

from publish_criteria import (
    NOISE_MARGIN,
    SPEED_MARGIN,
    band_drop,
    decide_magicquant_tiers,
    decide_rocmfpx_tiers,
)

BASELINE_GIB = 100.0
# Chosen so each lands cleanly inside one size band (see TIER_BOUNDARIES_V2):
# Q4 (0.242, 0.328], Q5 (0.328, 0.375], Q6 (0.375, 0.46].
Q4_GIB = 28.0
Q5_GIB = 35.0
Q6_GIB = 40.0


# ── BAND ─────────────────────────────────────────────────────────────────

def test_tier_outside_its_claimed_band_is_dropped_with_rule_band():
    # Q6_GIB (40 GiB / 100 GiB baseline) lands in the Q6 band, not Q5.
    result = decide_magicquant_tiers(
        [{"tier": "Q5", "gib": Q6_GIB, "loss": 0.01}], BASELINE_GIB)
    assert result["ship"] == []
    assert len(result["drop"]) == 1
    d = result["drop"][0]
    assert d["tier"] == "Q5"
    assert d["rule"] == "band"
    assert d["reason"]


def test_band_check_applies_to_rocmfpx_tiers_too():
    result = decide_rocmfpx_tiers(
        [{"tier": "Q5", "gib": Q6_GIB, "ppl": 7.0, "tg": 30.0}],
        mq_tiers={}, baseline_gib=BASELINE_GIB, baseline_ppl=5.0)
    assert result["ship"] == []
    d = result["drop"][0]
    assert d["rule"] == "band"
    assert d["reason"]


def test_band_correct_tier_is_not_dropped_for_band():
    result = decide_magicquant_tiers(
        [{"tier": "Q5", "gib": Q5_GIB, "loss": 0.01}], BASELINE_GIB)
    assert result["ship"] == ["Q5"]
    assert result["drop"] == []


# ── DOMINANCE (margin exceeds the floor) ────────────────────────────────

def test_dominance_fires_when_margin_exceeds_floor():
    # Q5 (smaller) meaningfully beats Q4... wait -- dominance only drops a
    # LARGER tier for being beaten by a smaller one, so make Q5 the larger,
    # worse tier and Q4 the smaller, better one.
    tiers = [
        {"tier": "Q4", "gib": Q4_GIB, "loss": 0.02},
        {"tier": "Q5", "gib": Q5_GIB, "loss": 0.05},
    ]
    result = decide_magicquant_tiers(tiers, BASELINE_GIB)
    assert result["ship"] == ["Q4"]
    assert len(result["drop"]) == 1
    d = result["drop"][0]
    assert d["tier"] == "Q5"
    assert d["rule"] == "dominance"
    assert d["reason"]
    assert d["beaten_by"] == {"tier": "Q4", "gib": Q4_GIB, "loss": 0.02}
    assert result["questions"] == []


def test_thinkingcap_real_numbers_drop_q6():
    """Regression pin: ThinkingCap's Q5 0.0009 vs Q6 0.0075 -- a 0.0066 gap,
    comfortably clearing NOISE_MARGIN, so Q6 must be dropped."""
    tiers = [
        {"tier": "Q5", "gib": Q5_GIB, "loss": 0.0009},
        {"tier": "Q6", "gib": Q6_GIB, "loss": 0.0075},
    ]
    result = decide_magicquant_tiers(tiers, BASELINE_GIB)
    assert result["ship"] == ["Q5"]
    assert [d["tier"] for d in result["drop"]] == ["Q6"]
    d = result["drop"][0]
    assert d["rule"] == "dominance"
    assert d["beaten_by"]["tier"] == "Q5"
    assert result["questions"] == []


# ── Sub-floor margin: keep both, raise a question ───────────────────────

def test_fablefusion_real_numbers_keep_both_and_raise_question():
    """Regression pin: FableFusion's Q5 loss 0.0025999 vs Q6 0.0024145 --
    Q6 is technically ahead, but by 0.000185, below NOISE_MARGIN. Both must
    ship; a question must be raised instead of trusting the coin flip.

    This is the direction the original hand-written script's loop could not
    see (it only ever checked whether the BIGGER tier looked worse), which
    is exactly the gap this port had to close.
    """
    tiers = [
        {"tier": "Q5", "gib": Q5_GIB, "loss": 0.0025999},
        {"tier": "Q6", "gib": Q6_GIB, "loss": 0.0024145},
    ]
    result = decide_magicquant_tiers(tiers, BASELINE_GIB)
    assert result["ship"] == ["Q5", "Q6"]
    assert result["drop"] == []
    assert len(result["questions"]) == 1
    q = result["questions"][0]
    assert "Q5" in q and "Q6" in q


def test_near_tie_margin_below_floor_does_not_drop_either_tier():
    margin = NOISE_MARGIN / 2
    tiers = [
        {"tier": "Q4", "gib": Q4_GIB, "loss": 0.01},
        {"tier": "Q5", "gib": Q5_GIB, "loss": 0.01 + margin},
    ]
    result = decide_magicquant_tiers(tiers, BASELINE_GIB)
    assert result["ship"] == ["Q4", "Q5"]
    assert result["drop"] == []
    assert len(result["questions"]) == 1


def test_margin_exactly_at_floor_drops_not_ties():
    # margin == NOISE_MARGIN is the ">=" boundary -- a real, if marginal,
    # verdict, not a coin flip. Built from the same float literal on both
    # sides so the subtraction is exact (0.01 + NOISE_MARGIN loses the last
    # bit to float rounding and lands a hair under the floor instead).
    tiers = [
        {"tier": "Q4", "gib": Q4_GIB, "loss": 0.0},
        {"tier": "Q5", "gib": Q5_GIB, "loss": NOISE_MARGIN},
    ]
    result = decide_magicquant_tiers(tiers, BASELINE_GIB)
    assert result["ship"] == ["Q4"]
    assert [d["tier"] for d in result["drop"]] == ["Q5"]
    assert result["questions"] == []


def test_unmeasured_loss_ships_without_a_dominance_verdict():
    """A tier with no measured_loss can't be judged, so it is neither
    dropped nor questioned -- it ships on the strength of its band check
    alone, same as the original script's `loss is None: continue` guard."""
    tiers = [
        {"tier": "Q4", "gib": Q4_GIB, "loss": 0.001},
        {"tier": "Q5", "gib": Q5_GIB, "loss": None},
    ]
    result = decide_magicquant_tiers(tiers, BASELINE_GIB)
    assert result["ship"] == ["Q4", "Q5"]
    assert result["drop"] == []
    assert result["questions"] == []


# ── ROCmFPX speed rule ───────────────────────────────────────────────────

def test_rocmfpx_tier_with_no_speed_advantage_is_dropped_with_rule_speed():
    fpx_tiers = [{"tier": "Q5", "gib": Q5_GIB, "ppl": 7.0, "tg": 20.0}]
    mq_tiers = {"Q5": {"tg": 20.0}}  # same speed -- no advantage
    result = decide_rocmfpx_tiers(fpx_tiers, mq_tiers, BASELINE_GIB, 5.0)
    assert result["ship"] == []
    assert len(result["drop"]) == 1
    d = result["drop"][0]
    assert d["tier"] == "Q5"
    assert d["rule"] == "speed"
    assert d["reason"]


def test_rocmfpx_tier_that_is_faster_ships():
    fpx_tiers = [{"tier": "Q5", "gib": Q5_GIB, "ppl": 7.0, "tg": 30.0}]
    mq_tiers = {"Q5": {"tg": 20.0}}  # 1.5x -- clears SPEED_MARGIN (1.15x)
    result = decide_rocmfpx_tiers(fpx_tiers, mq_tiers, BASELINE_GIB, 5.0)
    assert result["ship"] == ["Q5"]
    assert result["drop"] == []


def test_rocmfpx_speed_margin_boundary_is_exclusive():
    fpx_tiers = [{"tier": "Q5", "gib": Q5_GIB, "ppl": 7.0, "tg": 23.0}]
    mq_tiers = {"Q5": {"tg": 20.0}}  # exactly SPEED_MARGIN -- "<=" drops it
    result = decide_rocmfpx_tiers(fpx_tiers, mq_tiers, BASELINE_GIB, 5.0,
                                   speed_margin=SPEED_MARGIN)
    assert result["ship"] == []
    assert result["drop"][0]["rule"] == "speed"


def test_rocmfpx_with_no_magicquant_peer_ships_with_a_question():
    fpx_tiers = [{"tier": "Q5", "gib": Q5_GIB, "ppl": 7.0, "tg": 30.0}]
    result = decide_rocmfpx_tiers(fpx_tiers, mq_tiers={},
                                   baseline_gib=BASELINE_GIB, baseline_ppl=5.0)
    assert result["ship"] == ["Q5"]
    assert result["drop"] == []
    assert len(result["questions"]) == 1
    assert "Q5" in result["questions"][0]


def test_rocmfpx_dominance_and_near_tie_use_ppl_not_relative_loss():
    # PPL noise floor = NOISE_MARGIN * baseline_ppl = 0.001 * 5.0 = 0.005.
    fpx_tiers = [
        {"tier": "Q4", "gib": Q4_GIB, "ppl": 7.000, "tg": 33.0},
        {"tier": "Q5", "gib": Q5_GIB, "ppl": 7.070, "tg": 22.0},
    ]
    result = decide_rocmfpx_tiers(fpx_tiers, mq_tiers={}, baseline_gib=BASELINE_GIB,
                                   baseline_ppl=5.0)
    # margin = 0.07, well past the 0.005 floor -- Q5 dominated by Q4.
    assert [d["tier"] for d in result["drop"] if d["rule"] == "dominance"] == ["Q5"]


# ── A question must never contradict the decision that fired ────────────

def test_a_dropped_tier_never_gets_a_both_were_published_question():
    """Q6 is a near-tie with Q4 (0.0005, sub-floor) but is DOMINATED by Q5.

    The near-tie question asserts "Both were published". Emitted inline, it
    was appended for the Q4/Q6 pair before the Q5/Q6 pair dropped Q6 -- so a
    run that rejected Q6 also published a sentence saying Q6 shipped. That is
    the same shape as the 2026-08 card bug where the closing rationale
    contradicted the bullet above it, and it must not be reachable.
    """
    tiers = [
        {"tier": "Q4", "gib": Q4_GIB, "loss": 0.0100},
        {"tier": "Q5", "gib": Q5_GIB, "loss": 0.0020},
        {"tier": "Q6", "gib": Q6_GIB, "loss": 0.0105},
    ]
    result = decide_magicquant_tiers(tiers, BASELINE_GIB)
    assert result["ship"] == ["Q4", "Q5"]
    assert [d["tier"] for d in result["drop"]] == ["Q6"]
    for q in result["questions"]:
        assert "Q6" not in q, f"question names a tier that was dropped: {q}"


def test_rocmfpx_near_tie_question_is_withheld_when_speed_drops_the_tier():
    """Same rule on the ROCmFPX side, where the SPEED loop runs after
    dominance and can still drop a tier the near-tie branch already spoke
    for."""
    fpx_tiers = [
        {"tier": "Q4", "gib": Q4_GIB, "ppl": 7.000, "tg": 40.0},
        {"tier": "Q5", "gib": Q5_GIB, "ppl": 7.001, "tg": 20.0},  # no speed edge
    ]
    result = decide_rocmfpx_tiers(fpx_tiers, mq_tiers={"Q4": {"tg": 20.0},
                                                       "Q5": {"tg": 20.0}},
                                   baseline_gib=BASELINE_GIB, baseline_ppl=5.0)
    assert result["ship"] == ["Q4"]
    assert [d["rule"] for d in result["drop"]] == ["speed"]
    for q in result["questions"]:
        assert "Q5" not in q, f"question names a tier that was dropped: {q}"


# ── The reason text must match the rule that fired ──────────────────────

def test_drop_reason_text_matches_the_rule_that_fired():
    """Bug #3 was a dominance-shaped sentence ("a smaller tier already beats
    it") explaining a SPEED rejection -- false, and contradicting its own
    bullet. Pinning `rule` alone does not catch that; the prose is what
    reaches the page, so pin the prose too."""
    speed = decide_rocmfpx_tiers(
        [{"tier": "Q5", "gib": Q5_GIB, "ppl": 7.0, "tg": 20.0}],
        mq_tiers={"Q5": {"tg": 20.0}}, baseline_gib=BASELINE_GIB,
        baseline_ppl=5.0)["drop"][0]
    assert speed["rule"] == "speed"
    assert "tok/s" in speed["reason"]
    assert "smaller" not in speed["reason"].lower()

    dom = decide_magicquant_tiers(
        [{"tier": "Q4", "gib": Q4_GIB, "loss": 0.02},
         {"tier": "Q5", "gib": Q5_GIB, "loss": 0.05}],
        BASELINE_GIB)["drop"][0]
    assert dom["rule"] == "dominance"
    assert "smaller" in dom["reason"].lower()
    assert "tok/s" not in dom["reason"]

    band = decide_magicquant_tiers(
        [{"tier": "Q5", "gib": Q6_GIB, "loss": 0.01}], BASELINE_GIB)["drop"][0]
    assert band["rule"] == "band"
    assert "band" in band["reason"].lower()
    assert "smaller" not in band["reason"].lower()


# ── The floor is relative, and applies in both directions ───────────────

def test_rocmfpx_noise_floor_scales_with_baseline_perplexity():
    """The ROCmFPX comparison is on ABSOLUTE perplexity, so the relative
    noise floor has to be converted into PPL units. A 0.01 PPL gap against a
    50.0 baseline is 0.02% -- noise. Compared against the bare 0.001
    relative figure it would look like a 10x-the-floor verdict and wrongly
    drop the tier."""
    fpx_tiers = [
        {"tier": "Q4", "gib": Q4_GIB, "ppl": 7.00, "tg": 40.0},
        {"tier": "Q5", "gib": Q5_GIB, "ppl": 7.01, "tg": 40.0},
    ]
    result = decide_rocmfpx_tiers(fpx_tiers, mq_tiers={}, baseline_gib=BASELINE_GIB,
                                   baseline_ppl=50.0)
    assert result["ship"] == ["Q4", "Q5"]
    assert [d for d in result["drop"] if d["rule"] == "dominance"] == []


def test_rocmfpx_near_tie_below_floor_keeps_both_and_raises_a_question():
    fpx_tiers = [
        {"tier": "Q4", "gib": Q4_GIB, "ppl": 7.000, "tg": 40.0},
        {"tier": "Q5", "gib": Q5_GIB, "ppl": 7.001, "tg": 40.0},
    ]
    result = decide_rocmfpx_tiers(
        fpx_tiers, mq_tiers={"Q4": {"tg": 20.0}, "Q5": {"tg": 20.0}},
        baseline_gib=BASELINE_GIB, baseline_ppl=5.0)
    assert result["ship"] == ["Q4", "Q5"]
    assert result["drop"] == []
    assert len(result["questions"]) == 1
    assert "noise floor" in result["questions"][0]


def test_bigger_tier_that_is_meaningfully_better_ships_without_a_question():
    """The healthy-ladder case: a larger tier earning its size is a normal
    result, not something to ask about. Without the negative-direction guard
    every good run would emit a false "Q5 is arguably redundant"."""
    tiers = [
        {"tier": "Q4", "gib": Q4_GIB, "loss": 0.02},
        {"tier": "Q5", "gib": Q5_GIB, "loss": 0.01},   # bigger AND better
    ]
    result = decide_magicquant_tiers(tiers, BASELINE_GIB)
    assert result["ship"] == ["Q4", "Q5"]
    assert result["drop"] == []
    assert result["questions"] == []


# ── The band rule has exactly one implementation ────────────────────────

def test_band_drop_helper_is_the_rule_both_families_use():
    """``_publish_tiers.py`` band-checks ROCmFPX files early, before paying
    for their perplexity run. It calls ``band_drop`` rather than carrying its
    own copy -- the forked copy it replaced had drifted to different wording,
    so one rejection read two ways depending on which path caught it."""
    assert band_drop("Q5", Q5_GIB, BASELINE_GIB) is None
    entry = band_drop("Q5", Q6_GIB, BASELINE_GIB)
    assert entry["rule"] == "band"
    assert entry["actual_band"] == "Q6"

    from_mq = decide_magicquant_tiers(
        [{"tier": "Q5", "gib": Q6_GIB, "loss": 0.01}], BASELINE_GIB)["drop"][0]
    from_fpx = decide_rocmfpx_tiers(
        [{"tier": "Q5", "gib": Q6_GIB, "ppl": 7.0, "tg": 30.0}],
        mq_tiers={}, baseline_gib=BASELINE_GIB, baseline_ppl=5.0)["drop"][0]
    assert entry["reason"] == from_mq["reason"] == from_fpx["reason"]


# ── Every drop entry is disclosure-complete ─────────────────────────────

def test_every_drop_entry_carries_a_rule_and_a_nonempty_reason():
    mq = decide_magicquant_tiers(
        [
            {"tier": "Q5", "gib": Q6_GIB, "loss": 0.01},   # band
            {"tier": "Q4", "gib": Q4_GIB, "loss": 0.0009},  # survives
            {"tier": "Q6", "gib": Q6_GIB, "loss": 0.0075},  # dominated
        ],
        BASELINE_GIB,
    )
    fpx = decide_rocmfpx_tiers(
        [
            {"tier": "Q5", "gib": Q6_GIB, "ppl": 7.0, "tg": 30.0},  # band
            {"tier": "Q4", "gib": Q4_GIB, "ppl": 7.0, "tg": 20.0},  # no speed edge
        ],
        mq_tiers={"Q4": {"tg": 20.0}},
        baseline_gib=BASELINE_GIB, baseline_ppl=5.0,
    )
    all_drops = mq["drop"] + fpx["drop"]
    assert len(all_drops) >= 3
    for d in all_drops:
        assert d["rule"] in ("band", "dominance", "speed")
        assert isinstance(d["reason"], str) and d["reason"].strip()
        assert "tier" in d and "gib" in d and "loss" in d
        if d["rule"] == "dominance":
            assert "beaten_by" in d and d["beaten_by"]["tier"]


# ── Two files claiming one tier must not be resolved silently ────────────

def test_two_band_correct_files_claiming_one_tier_raise_a_question():
    """The survivors dict is keyed by tier, so a second same-tier file used to
    overwrite the first: dominance was then decided on file B's numbers while
    file A shipped, with no drop and no question. The uploader globs the
    directory, so the file nothing ruled on ships regardless."""
    tiers = [
        {"tier": "Q5", "gib": 34.0, "loss": 0.002},
        {"tier": "Q5", "gib": 36.0, "loss": 0.009},
    ]
    result = decide_magicquant_tiers(tiers, BASELINE_GIB)
    assert result["ship"] == ["Q5"]
    assert len(result["questions"]) == 1
    q = result["questions"][0]
    assert "34.00 GiB" in q and "36.00 GiB" in q


def test_a_band_drop_entry_carries_the_size_that_identifies_its_file():
    """``_publish_tiers._take_matching`` picks WHICH same-tier file to reject
    using this field. Without it, matching falls back to name order and can
    reject the band-correct file while the mislabeled one ships under that
    name -- the incident that put a uniform Q6_K on the hub labelled Q5."""
    result = decide_magicquant_tiers(
        [{"tier": "Q5", "gib": Q5_GIB, "loss": 0.002},
         {"tier": "Q5", "gib": Q6_GIB, "loss": 0.009}],
        BASELINE_GIB)
    assert [d["gib"] for d in result["drop"]] == [Q6_GIB]
    assert result["drop"][0]["rule"] == "band"


# ── recommended tier ─────────────────────────────────────────────────────────
#
# The ladder alone under-serves a reader: FableFusion's Q5->Q6 step costs 18%
# more bytes for 0.018 percentage points, below what the measurement resolves,
# and most people will not derive that from three numbers in a table.

def test_recommends_smallest_tier_statistically_tied_with_the_best():
    """FableFusion's real numbers: Q6 measures best, but Q5 ties it."""
    from core.publish_criteria import recommend_tier
    r = recommend_tier([
        {"tier": "Q4", "gib": 14.65, "loss": 0.016265},
        {"tier": "Q5", "gib": 17.68, "loss": 0.002600},
        {"tier": "Q6", "gib": 20.89, "loss": 0.002415},
    ])
    assert r["tier"] == "Q5", "Q6 is only 0.000185 better -- below the floor"
    assert "18% larger" in r["reason"]
    assert "below what this measurement can resolve" in r["reason"]


def test_recommends_the_best_when_the_gap_is_real():
    """ThinkingCap's real numbers: Q4->Q5 is a genuine 1.75pp, not noise."""
    from core.publish_criteria import recommend_tier
    r = recommend_tier([
        {"tier": "Q4", "gib": 16.68, "loss": 0.018392},
        {"tier": "Q5", "gib": 18.78, "loss": 0.000932},
    ])
    assert r["tier"] == "Q5"
    assert "real" in r["reason"] and "1.75" in r["reason"]


def test_no_recommendation_without_at_least_two_measured_tiers():
    from core.publish_criteria import recommend_tier
    assert recommend_tier([{"tier": "Q5", "gib": 17.68, "loss": 0.0026}]) is None
    assert recommend_tier([
        {"tier": "Q4", "gib": 14.65, "loss": None},
        {"tier": "Q5", "gib": 17.68, "loss": 0.0026},
    ]) is None


def test_recommendation_never_picks_a_larger_tier_over_a_tied_smaller_one():
    """Guards the direction: ties must resolve toward the smaller download."""
    from core.publish_criteria import recommend_tier
    r = recommend_tier([
        {"tier": "Q4", "gib": 10.0, "loss": 0.0021},
        {"tier": "Q5", "gib": 15.0, "loss": 0.0020},
        {"tier": "Q6", "gib": 20.0, "loss": 0.0019},
    ])
    assert r["tier"] == "Q4", "all three within the floor -> smallest wins"


# --- budget builds -----------------------------------------------------------
from core.publish_criteria import (BUDGET_FILE_RE, BUDGET_TOLERANCE,
                                   decide_budget_build, decide_rocmfpx_budget)


def test_budget_tolerance_value_pinned():
    assert BUDGET_TOLERANCE == 0.02


def test_budget_file_regex_extracts_the_number():
    m = BUDGET_FILE_RE.search("Model-BUDGET-12.5GiB.gguf")
    assert m and m.group(1) == "12.5"
    assert not BUDGET_FILE_RE.search("Model-Q4_K_M.gguf")


def test_budget_build_ships_under_budget():
    d = decide_budget_build(name="M-BUDGET-100GiB.gguf",
                            actual_gib=99.0, budget_gib=100.0)
    assert d["ship"] is True and d["rule"] == "budget"


def test_budget_build_ships_inside_tolerance():
    d = decide_budget_build(name="M-BUDGET-100GiB.gguf",
                            actual_gib=101.9, budget_gib=100.0)
    assert d["ship"] is True


def test_budget_build_refused_beyond_tolerance_names_uncounted_bytes():
    d = decide_budget_build(name="M-BUDGET-100GiB.gguf",
                            actual_gib=102.1, budget_gib=100.0)
    assert d["ship"] is False
    assert "uncounted" in d["reason"]
    assert d["overshoot_frac"] == pytest.approx(0.021, abs=1e-4)


def test_rocmfpx_budget_speed_rule_ships_only_when_faster():
    assert decide_rocmfpx_budget(fx_tg=12.0, mq_tg=10.0)["ship"] is True
    slow = decide_rocmfpx_budget(fx_tg=10.0, mq_tg=10.0)
    assert slow["ship"] is False and slow["rule"] == "speed"
    assert "tok/s" in slow["reason"] or "faster" in slow["reason"]


def test_rocmfpx_budget_missing_measurement_refuses_and_says_which():
    d = decide_rocmfpx_budget(fx_tg=None, mq_tg=10.0)
    assert d["ship"] is False and "measure" in d["reason"].lower()
