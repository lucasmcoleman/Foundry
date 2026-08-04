"""Pure ship/drop decision logic for publishing GGUF tiers to HuggingFace.

Ported out of ``output/_publish_tiers.py`` -- a scratch script that lived in
the gitignored ``output/`` directory and therefore carried every publishing
criterion (band check, dominance with a noise floor, the ROCmFPX speed rule)
completely untested. None of the four model-card bugs of 2026-08 was itself a
wrong band/dominance/speed verdict -- those were disclosure, reconcile and
rendering bugs, fixed in ``core/hf_upload.py`` and ``core/_rocmfpx_entry.py``.
What this module fixes is the condition that let them all ship unnoticed: the
rules deciding what reaches a public page were unreachable from pytest. Moving
the DECISION functions (not the subprocess/HF-API orchestration around them)
into a tracked, importable module is what lets them be tested.

Three rules, applied in this order, decide whether a NAMED-TIER (Q4/Q5/Q6)
build ships:

  BAND      -- a tier must land in the size band its name claims, checked via
               ``magicquant.quant.tiers.classify_tier`` against the BF16
               baseline size. A tier name is a size band, never a promise
               about which schemes are inside it.
  DOMINANCE -- drop a tier when another, SMALLER tier of the same family
               beats it on quality by more than ``NOISE_MARGIN``. A gap
               under the floor is noise, not a verdict -- in EITHER
               direction. A bigger tier that measures only a hair better
               than a smaller one is exactly as unproven as one that
               measures a hair worse: both raise a QUESTION instead of
               deciding anything. FableFusion's real numbers are the
               regression pin for the "hair better" side (Q5 loss
               0.0025999, Q6 0.0024145 -- Q6 is technically ahead, but by
               0.000185, which is below the floor, so the rule must NOT
               read that as proof Q6 earned its extra size; both ship and a
               question is raised). ThinkingCap's real numbers pin the
               other side: Q5 0.0009 vs Q6 0.0075 is a 0.0066 gap, comfortably
               past the floor, so Q6 drops.
  SPEED     -- ROCmFPX trades quality for throughput at the same size, so its
               only justification over a same-band MagicQuant tier is speed.
               A ROCmFPX tier ships only if it clears ``SPEED_MARGIN`` faster.

A fourth rule, BUDGET, applies instead to size-target builds that claim a
GiB size rather than a Q-tier band: ``decide_budget_build`` ships a build
that fits its requested size within ``BUDGET_TOLERANCE``, and
``decide_rocmfpx_budget`` applies the same SPEED justification as above to a
ROCmFPX budget build against its MagicQuant budget peer. Both live near the
bottom of this module.

Every function here is pure: sizes, losses, and tok/s in, a decision out. No
filesystem, no subprocess, no HF API calls -- that split is what makes this
testable. ``output/_publish_tiers.py`` imports from here instead of keeping
its own copy of the rules.
"""

import re

from magicquant.quant.tiers import classify_tier

# Dominance needs a MEANINGFUL margin, not any margin. FableFusion's Q6
# shipped only because it measured 0.000185 better than its Q5 (PPL 6.2507
# vs 6.2518, a 0.0012 gap on a 100-chunk wikitext pass). Below this floor the
# two tiers are statistically indistinguishable, so the rule keeps both and
# raises a question rather than letting noise pick a winner. Expressed as
# relative loss; 0.001 is 0.1% of baseline perplexity, ~5x the gap that
# actually decided FableFusion.
NOISE_MARGIN = 0.001

# A ROCmFPX tier must generate at least this many times its same-band
# MagicQuant peer's tok/s to justify the quality it trades away. Qwen's
# surviving ROCmFPX tiers cleared 2.4x -- this is a floor, not a target.
SPEED_MARGIN = 1.15


def _band_reason(claimed, actual):
    return (f"It was built but landed in the {actual} size band while "
            f"claiming {claimed}, so shipping it would have mislabeled it.")


def band_drop(claimed, gib, baseline_gib):
    """Return a BAND drop entry for a mislabeled tier, or None if it is fine.

    Exported so a caller that must band-check EARLY -- ``_publish_tiers.py``
    rejects a mislabeled ROCmFPX file before paying for its llama-perplexity
    run -- reuses this rule instead of forking it. The forked copy it replaced
    had drifted to different wording than ``decide_rocmfpx_tiers``, so the same
    rejection read two different ways depending on which path caught it.
    """
    actual = classify_tier(gib, baseline_gib)
    if actual == claimed:
        return None
    return {"tier": claimed, "rule": "band",
            "reason": _band_reason(claimed, actual),
            "gib": gib, "loss": None, "actual_band": actual}


def decide_magicquant_tiers(tiers, baseline_gib, noise_margin=NOISE_MARGIN):
    """Decide which MagicQuant tiers ship, given already-measured data.

    Args:
        tiers: iterable of ``{"tier": "Q5", "gib": 12.3, "loss": 0.0026}``.
            ``loss`` is the search's ``measured_loss`` (relative to the BF16
            baseline; lower is better) and may be ``None`` when unmeasured --
            such a tier is never dominance-checked, only band-checked.
        baseline_gib: BF16 baseline size in GiB.
        noise_margin: relative-loss floor below which two tiers' quality is
            treated as indistinguishable (see module docstring).

    Returns:
        ``{"ship": [tier, ...], "drop": [entry, ...], "questions": [str, ...]}``
        sorted by tier name. Each drop entry carries ``tier``, ``rule``
        (``"band"`` or ``"dominance"``), a reader-facing ``reason``, ``gib``,
        ``loss`` (``None`` where unknown), and -- only when ``rule`` is
        ``"dominance"`` -- ``beaten_by``: ``{"tier", "gib", "loss"}`` of the
        smaller tier that beat it.
    """
    survivors = {}
    drop = []
    questions = []

    for t in tiers:
        claimed = t["tier"]
        gib = t["gib"]
        loss = t.get("loss")
        band = band_drop(claimed, gib, baseline_gib)
        if band is not None:
            drop.append({**band, "loss": loss})
            continue
        if claimed in survivors:
            # Two band-correct files claim the same tier. Silently overwriting
            # meant dominance was decided on the second file's numbers while
            # the first one shipped -- and since the uploader globs the
            # directory, a file this function never ruled on ships anyway.
            questions.append(
                f"Two files both claim {claimed} and both land in that band "
                f"({survivors[claimed]['gib']:.2f} GiB and {gib:.2f} GiB). "
                f"Only one can be published under that name; the quality "
                f"verdict below was decided on {gib:.2f} GiB. Remove one "
                f"before publishing.")
        survivors[claimed] = {"gib": gib, "loss": loss}

    # Dominance + near-tie. Each unordered pair is visited exactly once, in
    # the direction (larger=a, smaller=b) -- the inner loop's gib check
    # excludes the reverse ordering, so there is no double-counting.
    dropped = set()
    near_ties = []
    for tier, a in list(survivors.items()):
        if a["loss"] is None:
            continue
        for other, b in survivors.items():
            if other == tier or other in dropped or b["loss"] is None:
                continue
            if b["gib"] >= a["gib"]:
                continue  # only a genuinely smaller tier can dominate
            margin = a["loss"] - b["loss"]
            if margin >= noise_margin:
                drop.append({
                    "tier": tier, "rule": "dominance",
                    "reason": (
                        f"The {other} tier is both smaller and measurably "
                        f"better, so {tier} would be a larger download for "
                        f"worse quality."),
                    "gib": a["gib"], "loss": a["loss"],
                    "beaten_by": {"tier": other, "gib": b["gib"],
                                  "loss": b["loss"]},
                })
                dropped.add(tier)
                break
            if margin <= -noise_margin:
                continue  # a is meaningfully better despite being bigger
            near_ties.append((tier, other, a, b, margin))

    # Near-tie questions are emitted only AFTER every drop is known, and only
    # for pairs where both tiers actually shipped. Emitting them inline
    # produced "Both were published; Q6 is the larger file ..." for a Q6 that
    # a later pair in the same loop then dropped on dominance -- a stated
    # rationale contradicting the decision that fired, which is exactly the
    # bug class (2026-08 public model cards) this module exists to prevent.
    questions.extend(_near_tie_questions(near_ties, dropped, noise_margin))

    ship = sorted(t for t in survivors if t not in dropped)
    return {"ship": ship, "drop": drop, "questions": questions}


def _near_tie_questions(near_ties, dropped, noise_margin):
    out = []
    for tier, other, a, b, margin in near_ties:
        if tier in dropped or other in dropped:
            continue
        out.append(
            f"{tier} ({a['gib']:.2f} GiB, loss {a['loss']:+.4f}) and "
            f"{other} ({b['gib']:.2f} GiB, loss {b['loss']:+.4f}) are "
            f"{abs(margin):.6f} apart, below the {noise_margin} noise "
            f"floor -- statistically indistinguishable on quality. Both "
            f"were published; {tier} is the larger file for the same "
            f"measurable quality, so it is arguably redundant. Say the "
            f"word and I will remove it.")
    return out


def decide_rocmfpx_tiers(fpx_tiers, mq_tiers, baseline_gib, baseline_ppl,
                          noise_margin=NOISE_MARGIN, speed_margin=SPEED_MARGIN):
    """Decide which ROCmFPX tiers ship, given already-measured data.

    Args:
        fpx_tiers: iterable of ``{"tier": "Q5", "gib": 12.3, "ppl": 7.07,
            "tg": 33.7}``. ``ppl`` must already be a real number -- a build
            whose perplexity run failed to parse (nan/broken) is an I/O-level
            concern the caller filters out before calling this, not a
            band/dominance/speed decision.
        mq_tiers: ``{"Q5": {"tg": 22.1}, ...}`` -- the same-band MagicQuant
            peer's measured tok/s, for the speed rule. A tier absent here (or
            with ``tg`` unknown) ships with a question instead of a verdict:
            there is nothing to compare speed against.
        baseline_gib: BF16 baseline size in GiB, for the band check.
        baseline_ppl: BF16 baseline perplexity, used to convert the relative
            ``noise_margin`` into absolute PPL units for this comparison.
        noise_margin: see ``decide_magicquant_tiers``.
        speed_margin: minimum tok/s ratio over the MagicQuant peer required
            to justify shipping (see module docstring).

    Returns: same shape as ``decide_magicquant_tiers``. ``loss`` is always
        ``None`` in drop entries here (this family is compared on PPL, not
        relative loss); ``beaten_by`` uses ``ppl`` instead of ``loss``.
    """
    survivors = {}
    drop = []
    questions = []

    for t in fpx_tiers:
        claimed = t["tier"]
        gib = t["gib"]
        band = band_drop(claimed, gib, baseline_gib)
        if band is not None:
            drop.append(band)
            continue
        survivors[claimed] = {"gib": gib, "ppl": t["ppl"], "tg": t.get("tg")}

    # Same floor as MagicQuant's, expressed in PPL units: this comparison is
    # on absolute perplexity, not relative loss.
    ppl_margin = noise_margin * (baseline_ppl or 1.0)
    dropped = set()
    near_ties = []
    for tier, a in list(survivors.items()):
        for other, b in survivors.items():
            if other == tier or other in dropped:
                continue
            if b["gib"] >= a["gib"]:
                continue
            margin = a["ppl"] - b["ppl"]
            if margin >= ppl_margin:
                drop.append({
                    "tier": tier, "rule": "dominance",
                    "reason": (
                        f"The {other} tier is both smaller and measurably "
                        f"better on perplexity, so {tier} would be a larger "
                        f"download for worse quality."),
                    "gib": a["gib"], "loss": None,
                    "beaten_by": {"tier": other, "gib": b["gib"],
                                  "ppl": b["ppl"]},
                })
                dropped.add(tier)
                break
            if margin <= -ppl_margin:
                continue
            near_ties.append((tier, other, a, b, margin))

    # Speed is ROCmFPX's only justification -- it loses on quality at equal
    # size, so a surviving tier still needs a peer to beat.
    for tier in list(survivors):
        if tier in dropped:
            continue
        a = survivors[tier]
        peer = (mq_tiers or {}).get(tier)
        peer_tg = peer.get("tg") if peer else None
        if peer_tg is None or a["tg"] is None:
            questions.append(
                f"ROCmFPX {tier}: no MagicQuant {tier} to compare speed "
                f"against (or bench failed) -- shipped without a speed "
                f"justification.")
            continue
        if a["tg"] <= peer_tg * speed_margin:
            drop.append({
                "tier": tier, "rule": "speed",
                "reason": (
                    f"ROCmFPX trades some quality for throughput, but this "
                    f"build measured {a['tg']:.1f} tok/s against the "
                    f"MagicQuant {tier}'s {peer_tg:.1f} -- no speed gain to "
                    f"justify the tradeoff."),
                "gib": a["gib"], "loss": None,
            })
            dropped.add(tier)

    # After the speed rule, not before it: a near-tie question here claims
    # "both published", and on this family the speed loop can still drop one
    # of the pair. Same contradiction the MagicQuant side had.
    for tier, other, a, b, margin in near_ties:
        if tier in dropped or other in dropped:
            continue
        questions.append(
            f"ROCmFPX {tier} ({a['gib']:.2f} GiB, PPL {a['ppl']:.4f}) and "
            f"{other} ({b['gib']:.2f} GiB, PPL {b['ppl']:.4f}) are "
            f"{abs(margin):.4f} PPL apart, below the {ppl_margin:.4f} "
            f"noise floor. Both published; say the word to drop {tier}.")

    ship = sorted(t for t in survivors if t not in dropped)
    return {"ship": ship, "drop": drop, "questions": questions}


def recommend_tier(tiers, noise_margin=NOISE_MARGIN):
    """Pick the tier most people should download, with a reason to show.

    The rule: **the smallest tier whose measured loss is within the noise floor
    of the best measured loss.** Once two tiers are statistically
    indistinguishable on quality, the only thing separating them is download
    size, and the smaller one wins by default.

    This exists because the ladder alone under-serves the reader. FableFusion
    ships Q4/Q5/Q6 at +1.63% / +0.26% / +0.24%: the jump from Q4 to Q5 buys a
    real 1.4 points, while Q5 to Q6 costs 18% more bytes for 0.018 points --
    below what a 100-chunk perplexity pass can even resolve. A table of three
    numbers leaves everyone to derive that themselves, and most won't.

    Args:
        tiers: iterable of ``{"tier": "Q5", "gib": 17.68, "loss": 0.0026}``.
            Entries with ``loss`` of ``None`` are ignored -- an unmeasured tier
            cannot be recommended on measured grounds.
        noise_margin: relative-loss floor below which two tiers count as equal.

    Returns:
        ``{"tier", "reason", "gib", "loss"}`` for the pick, or ``None`` when
        fewer than two tiers carry measurements (nothing to choose between).
    """
    measured = [
        t for t in tiers
        if isinstance(t.get("loss"), (int, float)) and t.get("gib")
    ]
    if len(measured) < 2:
        return None

    best_loss = min(t["loss"] for t in measured)
    # Every tier statistically tied with the best, then the smallest of them.
    tied = [t for t in measured if t["loss"] - best_loss < noise_margin]
    pick = min(tied, key=lambda t: t["gib"])

    bigger = [
        t for t in measured
        if t["gib"] > pick["gib"] and t["loss"] - best_loss < noise_margin
    ]
    if bigger:
        largest = max(bigger, key=lambda t: t["gib"])
        pct = (largest["gib"] / pick["gib"] - 1) * 100
        gap = (largest["loss"] - pick["loss"]) * 100
        reason = (
            f"It is the smallest tier that is statistically tied with the best "
            f"measured quality here. {largest['tier']} is {pct:.0f}% larger for "
            f"{abs(gap):.3f} percentage points of perplexity, which is below "
            f"what this measurement can resolve -- so the extra bytes buy "
            f"nothing you can detect."
        )
    else:
        smaller = [t for t in measured if t["gib"] < pick["gib"]]
        if smaller:
            nxt = max(smaller, key=lambda t: t["gib"])
            gap = (nxt["loss"] - pick["loss"]) * 100
            reason = (
                f"It has the best measured quality on offer, and the next size "
                f"down ({nxt['tier']}) gives up a real {gap:.2f} percentage "
                f"points of perplexity rather than a difference lost in noise."
            )
        else:
            reason = "It has the best measured quality of the tiers published here."
    return {"tier": pick["tier"], "reason": reason,
            "gib": pick["gib"], "loss": pick["loss"]}


# --- Budget (size-target) builds --------------------------------------------
# A budget build claims a SIZE, not a tier band. BAND and DOMINANCE do not
# apply (no band claim, no siblings); the one rule is the tolerance below.
# v2's budget bounds allocatable-tensor bytes, not file size -- metadata,
# alignment padding, and passthrough tensors with unknown sizes are uncounted,
# and ROCmFPX fork-type rounding can overshoot. 2% covers both; one constant
# so build-time and publish-time guards can never disagree.
BUDGET_TOLERANCE = 0.02

BUDGET_FILE_RE = re.compile(r"-BUDGET-([\d.]+)GiB\.gguf$")


def decide_budget_build(*, name: str, actual_gib: float, budget_gib: float) -> dict:
    """SHIP/REFUSE for a MagicQuant budget build (publish-time size guard)."""
    limit = budget_gib * (1 + BUDGET_TOLERANCE)
    overshoot = actual_gib / budget_gib - 1.0
    if actual_gib <= limit:
        return {"ship": True, "rule": "budget", "overshoot_frac": overshoot,
                "reason": (f"{name}: {actual_gib:.2f} GiB fits the requested "
                           f"{budget_gib:g} GiB budget "
                           f"(tolerance {BUDGET_TOLERANCE:.0%}).")}
    return {"ship": False, "rule": "budget", "overshoot_frac": overshoot,
            "reason": (f"{name}: {actual_gib:.2f} GiB exceeds the requested "
                       f"{budget_gib:g} GiB budget by {overshoot:.1%} "
                       f"(> {BUDGET_TOLERANCE:.0%} tolerance) -- likely "
                       f"uncounted bytes (GGUF metadata, alignment padding, "
                       f"passthrough tensors) or fork-type rounding.")}


def decide_rocmfpx_budget(*, fx_tg: float | None, mq_tg: float | None) -> dict:
    """SPEED rule for a ROCmFPX budget build vs its MagicQuant budget peer.

    Same principle as decide_rocmfpx_tiers: ROCmFPX trades quality for
    throughput, so it ships only when measurably faster. Unmeasured never
    ships -- speed is the family's entire justification.
    """
    if fx_tg is None or mq_tg is None:
        missing = []
        if fx_tg is None:
            missing.append("ROCmFPX build")
        if mq_tg is None:
            missing.append("MagicQuant peer")
        which = " and ".join(missing)
        return {"ship": False, "rule": "speed",
                "reason": f"no throughput measurement for the {which}; "
                          f"a speed-justified family never ships unmeasured."}
    if fx_tg >= mq_tg * SPEED_MARGIN:
        return {"ship": True, "rule": "speed",
                "reason": (f"{fx_tg:.2f} tok/s vs MagicQuant peer "
                           f"{mq_tg:.2f} tok/s (>= {SPEED_MARGIN:g}x).")}
    return {"ship": False, "rule": "speed",
            "reason": (f"{fx_tg:.2f} tok/s is not >= {SPEED_MARGIN:g}x the "
                       f"MagicQuant peer's {mq_tg:.2f} tok/s -- not faster, "
                       f"so the quality trade buys nothing.")}
