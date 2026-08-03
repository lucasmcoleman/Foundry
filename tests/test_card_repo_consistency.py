"""A model card must never contradict the repo it describes.

Two structural bugs shipped to public HF pages before being noticed:
  - generate_model_card built its GGUF table by stat()ing LOCAL paths, so
    regenerating a card after the cleanup stage deleted local GGUFs silently
    reduced the table to whatever was still on disk (e.g. just mmproj), even
    though the repo itself still held the real files.
  - Build-time refusals and mismatches between a card's claims and the repo's
    actual contents were only ever visible by scraping log text with a
    regex, which breaks the moment logs rotate, get cleaned up, or reworded.

FIX 1 (known_sizes): generate_model_card can source a file's size from an
authoritative dict instead of the filesystem, so a regeneration can source
sizes from the repo's own metadata and doesn't need local files to survive.

FIX 2 (audit_card_against_repo): a post-upload self-audit that compares a
just-pushed card's claims against the repo's actual file list and warns
loudly on mismatch, without ever failing the (already-succeeded) upload.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core"))

from hf_upload import (
    HFUploadConfig,
    audit_card_against_repo,
    card_rows_from_repo,
    generate_model_card,
)


# ── FIX 1: known_sizes ───────────────────────────────────────────────────────

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


def test_local_stat_path_unchanged_when_file_present(tmp_path):
    # Existing behavior must survive untouched: a real local file gets its
    # size from stat(), known_sizes not even needed.
    p = tmp_path / "model-Q4.gguf"
    p.write_bytes(b"x" * 2_000_000_000)  # 2 GB
    card = generate_model_card(_cfg(), [(p, "model-Q4.gguf")])
    assert "| [model-Q4.gguf](./model-Q4.gguf) | 2.0 GB |" in card


def test_known_sizes_used_when_local_file_absent(tmp_path):
    # The exact regression: local GGUFs were deleted by the cleanup stage,
    # so the local path no longer exists -- but a caller regenerating the
    # card from the repo's own metadata still has the real size.
    missing = tmp_path / "does-not-exist" / "model-Q5.gguf"
    assert not missing.exists()
    known_sizes = {"model-Q5.gguf": 5_500_000_000}  # 5.5 GB
    card = generate_model_card(
        _cfg(), [(missing, "model-Q5.gguf")], known_sizes=known_sizes
    )
    assert "| [model-Q5.gguf](./model-Q5.gguf) | 5.5 GB |" in card


def test_missing_local_file_and_no_known_size_is_skipped_not_crashed(tmp_path):
    # Without an authoritative size, generate_model_card must not raise
    # (the old stat()-on-a-missing-path behavior) or fabricate a number --
    # it just can't report a table row for it.
    missing = tmp_path / "gone.gguf"
    card = generate_model_card(_cfg(), [(missing, "gone.gguf")])  # must not raise
    assert "| [gone.gguf](./gone.gguf)" not in card


def test_known_sizes_covers_non_gguf_other_files_table_too(tmp_path):
    missing = tmp_path / "adapter_config.json"
    known_sizes = {"lora/adapter_config.json": 4_200_000}  # 4.2 MB
    card = generate_model_card(
        _cfg(upload_lora=True),
        [(missing, "lora/adapter_config.json")],
        known_sizes=known_sizes,
    )
    assert "| lora/adapter_config.json | 4 MB |" in card


# ── FIX 2: audit_card_against_repo ──────────────────────────────────────────

def _collect(log_calls):
    def log(msg, level="info"):
        log_calls.append((level, msg))
    return log


def test_audit_catches_missing_table_row(tmp_path):
    # Bug 5: a card regenerated from a cleaned-up local dir dropped every
    # GGUF row but mmproj, even though the repo still had the real files.
    p = tmp_path / "model-Q4.gguf"
    p.write_bytes(b"x" * 1_000_000_000)
    card = generate_model_card(_cfg(), [(p, "model-Q4.gguf")])

    # The repo actually holds Q4 AND Q5, but the card only describes Q4.
    repo_files = ["model-Q4.gguf", "model-Q5.gguf", "README.md"]
    calls = []
    warnings = audit_card_against_repo(card, repo_files, log=_collect(calls))

    # "Mentioned nowhere", not "missing a table row": a carried-over file is
    # deliberately disclosed in prose instead of the table, so demanding a row
    # flagged correct cards too (see
    # test_correctly_disclosed_carried_over_file_is_not_flagged).
    assert any("model-Q5.gguf" in w and "never mentions it" in w for w in warnings)
    assert any(level == "warn" for level, _ in calls)


def test_audit_catches_not_published_contradicted_by_present_file(tmp_path):
    # Bugs 1 & 2: a card claims "<TIER> was not published" while a file for
    # that tier sits right there in the repo.
    p = tmp_path / "model-Q4.gguf"
    p.write_bytes(b"x" * 1_000_000_000)
    cfg = _cfg(dropped_tiers=[{"tier": "Q5", "reason": "did not beat a smaller tier."}])
    card = generate_model_card(cfg, [(p, "model-Q4.gguf")])
    assert "Q5 was not published" in card  # sanity: the claim is actually in the card

    # But Q5 IS in the repo (the stale/wrong-repo scenario from the incident).
    repo_files = ["model-Q4.gguf", "model-Q5.gguf"]
    calls = []
    warnings = audit_card_against_repo(card, repo_files, log=_collect(calls))

    assert any("Q5" in w and "was not published" in w and "model-Q5.gguf" in w for w in warnings)


def test_audit_does_not_false_positive_on_similar_tier_names(tmp_path):
    # "Q4" must not match inside "Q40" or similar -- bounded match, not
    # substring, or the audit itself becomes a source of noise.
    p = tmp_path / "model-Q5.gguf"
    p.write_bytes(b"x" * 1_000_000_000)
    cfg = _cfg(dropped_tiers=[{"tier": "Q4", "reason": "band mismatch."}])
    card = generate_model_card(cfg, [(p, "model-Q5.gguf")])

    repo_files = ["model-Q5.gguf", "model-Q40-experimental.gguf"]
    warnings = audit_card_against_repo(card, repo_files, log=lambda *a, **k: None)
    assert not any("Q4" in w and "was not published" in w for w in warnings)


def test_audit_catches_carried_over_file_not_actually_in_repo():
    cfg = _cfg(carried_over=[{"name": "model-Q6.gguf", "gib": 12.3}])
    card = generate_model_card(cfg, [])
    assert "model-Q6.gguf" in card

    # The repo does NOT actually have that file.
    repo_files = ["model-Q4.gguf"]
    warnings = audit_card_against_repo(card, repo_files, log=lambda *a, **k: None)
    assert any("model-Q6.gguf" in w and "not in the repo" in w for w in warnings)


def test_audit_clean_case_produces_no_warnings(tmp_path):
    # Everything the card claims matches the repo exactly -- no findings.
    p4 = tmp_path / "model-Q4.gguf"
    p5 = tmp_path / "model-Q5.gguf"
    p4.write_bytes(b"x" * 1_000_000_000)
    p5.write_bytes(b"x" * 1_500_000_000)
    cfg = _cfg()
    card = generate_model_card(cfg, [(p4, "model-Q4.gguf"), (p5, "model-Q5.gguf")])

    repo_files = ["model-Q4.gguf", "model-Q5.gguf", "README.md", ".gitattributes"]
    calls = []
    warnings = audit_card_against_repo(card, repo_files, log=_collect(calls))

    assert warnings == []
    assert any("audit passed" in msg for _, msg in calls)


def test_audit_never_raises_on_malformed_card():
    # A detector must not itself become a new failure mode.
    warnings = audit_card_against_repo("not really a card at all", [], log=lambda *a, **k: None)
    assert warnings == []


# ── known_sizes is authoritative, and rows can come from the repo ───────────

def test_known_sizes_wins_over_a_stale_local_file(tmp_path):
    """"Authoritative" is only meaningful when the two sources DISAGREE.

    Every other known_sizes test exercises exactly one source (local file, no
    dict; or dict, no local file), so inverting the priority passed all of
    them. A half-written or truncated leftover on disk is the realistic
    conflict, and the repo's recorded size is the one a reader can verify.
    """
    p = tmp_path / "model-Q4.gguf"
    p.write_bytes(b"x" * 1_000_000)                 # stale 0.001 GB leftover
    card = generate_model_card(
        _cfg(), [(p, "model-Q4.gguf")],
        known_sizes={"model-Q4.gguf": 9_000_000_000},   # repo says 9.0 GB
    )
    assert "| [model-Q4.gguf](./model-Q4.gguf) | 9.0 GB |" in card
    assert "0.0 GB" not in card


def test_card_rows_from_repo_builds_rows_for_files_with_no_local_copy(tmp_path):
    """The actual fix for the collapsed-table bug.

    The table iterates files_to_upload, and its only producer globs the LOCAL
    dir -- so after cleanup deletes the GGUFs there is no ROW for a size to
    attach to, and known_sizes alone cannot help. The rows have to be sourced
    from the repo too.
    """
    class _Sibling:
        def __init__(self, name, size):
            self.rfilename, self.size = name, size

    siblings = [_Sibling("model-Q4.gguf", 4_000_000_000),
                _Sibling("model-Q5.gguf", 5_000_000_000)]
    rows, sizes = card_rows_from_repo(siblings, local_dir=tmp_path)
    assert not any(p.exists() for p, _ in rows)      # nothing local survives

    card = generate_model_card(_cfg(), rows, known_sizes=sizes)
    assert "| [model-Q4.gguf](./model-Q4.gguf) | 4.0 GB |" in card
    assert "| [model-Q5.gguf](./model-Q5.gguf) | 5.0 GB |" in card
    assert audit_card_against_repo(
        card, ["model-Q4.gguf", "model-Q5.gguf", "README.md"]) == []


def test_card_rows_from_repo_accepts_plain_name_size_pairs(tmp_path):
    rows, sizes = card_rows_from_repo([("model-Q6.gguf", 6_000_000_000)],
                                       local_dir=tmp_path)
    assert rows == [(tmp_path / "model-Q6.gguf", "model-Q6.gguf")]
    assert sizes == {"model-Q6.gguf": 6_000_000_000}


def test_a_row_dropped_for_lack_of_a_size_is_logged_not_silent(tmp_path):
    """The bug took a day to notice because the table shrank in silence."""
    seen = []
    generate_model_card(_cfg(), [(tmp_path / "gone-Q4.gguf", "gone-Q4.gguf")],
                        log=lambda msg, level="info": seen.append((level, msg)))
    assert any(lvl == "warn" and "gone-Q4.gguf" in msg for lvl, msg in seen)


# ── The audit must not cry wolf on a card that is CORRECT ──────────────────

def test_correctly_disclosed_carried_over_file_is_not_flagged(tmp_path):
    """Bug #2's own repo: a 23 GiB Q6 from an earlier run, disclosed exactly
    right under "Files from an earlier build" -- which is prose, not a table
    row, by design. Demanding a table row flagged every such correct card,
    and with the SAME message the audit emits when the table genuinely
    collapsed, making the one signal that matters indistinguishable from
    normal operation."""
    p4 = tmp_path / "model-Q4.gguf"; p4.write_bytes(b"x" * 1000)
    cfg = _cfg(carried_over=[{"name": "model-Q6.gguf", "gib": 23.0,
                              "actual_band": "Q6"}])
    card = generate_model_card(cfg, [(p4, "model-Q4.gguf")])
    assert audit_card_against_repo(
        card, ["model-Q4.gguf", "model-Q6.gguf", "README.md"]) == []


def test_a_repo_gguf_mentioned_nowhere_is_still_flagged(tmp_path):
    """The check still has to bite: relaxing "needs a table row" to "needs a
    mention" must not relax it to "needs nothing"."""
    p4 = tmp_path / "model-Q4.gguf"; p4.write_bytes(b"x" * 1000)
    card = generate_model_card(_cfg(), [(p4, "model-Q4.gguf")])
    warnings = audit_card_against_repo(card, ["model-Q4.gguf", "model-Q9.gguf"])
    assert len(warnings) == 1
    assert "model-Q9.gguf" in warnings[0]
    assert "never mentions it" in warnings[0]


# ── Refused tiers are the strongest contradiction, and were unchecked ──────

def test_audit_catches_a_refused_tier_that_is_present_in_the_repo(tmp_path):
    """A refused-tier note promises the file "will not appear in a later
    build either". With that file sitting in the repo, that is a harder
    contradiction than "was not published" -- and the regex only ever matched
    the latter, so this reported as a mere missing table row."""
    p4 = tmp_path / "model-Q4.gguf"; p4.write_bytes(b"x" * 1000)
    cfg = _cfg(refused_tiers=[{"tier": "Q5", "family": "rocmfpx",
                               "reason": "no 5-bit ROCmFPX type exists."}])
    card = generate_model_card(cfg, [(p4, "model-Q4.gguf")], rocmfpx=True)
    warnings = audit_card_against_repo(card, ["model-Q4.gguf", "model-mq-q5.gguf"])
    assert any("is not produced by this build" in w and "Q5" in w
               for w in warnings), warnings


def test_a_refused_tier_with_no_file_present_is_not_flagged(tmp_path):
    p4 = tmp_path / "model-Q4.gguf"; p4.write_bytes(b"x" * 1000)
    cfg = _cfg(refused_tiers=[{"tier": "Q5", "family": "rocmfpx",
                               "reason": "no 5-bit ROCmFPX type exists."}])
    card = generate_model_card(cfg, [(p4, "model-Q4.gguf")], rocmfpx=True)
    assert audit_card_against_repo(card, ["model-Q4.gguf"]) == []


# ── Tier matching is GGUF-only, in both directions ────────────────────────

def test_a_tier_named_non_gguf_artifact_does_not_suppress_a_true_claim(tmp_path):
    """An imatrix / search-results / lora artifact carrying a tier token is
    not a published quant. Matching it made the audit assert a tier "IS in
    the repo" when no GGUF for it existed -- a false negative dressed as a
    finding."""
    p4 = tmp_path / "model-Q4.gguf"; p4.write_bytes(b"x" * 1000)
    cfg = _cfg(dropped_tiers=[{"tier": "Q6", "family": "magicquant",
                               "rule": "dominance", "reason": "dominated."}])
    card = generate_model_card(cfg, [(p4, "model-Q4.gguf")])
    assert audit_card_against_repo(
        card, ["model-Q4.gguf", "search_results-Q6.json"]) == []


def test_an_mmproj_file_never_counts_as_a_tier(tmp_path):
    p4 = tmp_path / "model-Q4.gguf"; p4.write_bytes(b"x" * 1000)
    pm = tmp_path / "mmproj-Q6.gguf"; pm.write_bytes(b"x" * 100)
    cfg = _cfg(dropped_tiers=[{"tier": "Q6", "family": "magicquant",
                               "rule": "dominance", "reason": "dominated."}])
    card = generate_model_card(cfg, [(p4, "model-Q4.gguf"), (pm, "mmproj-Q6.gguf")])
    warnings = audit_card_against_repo(card, ["model-Q4.gguf", "mmproj-Q6.gguf"])
    assert not any("IS in the repo" in w for w in warnings), warnings


# ── The closing rationale must never assert a rule that did not fire ───────

def _rationale_card(dropped):
    return generate_model_card(_cfg(dropped_tiers=dropped), [])


def test_speed_rejection_is_not_explained_with_dominance():
    card = _rationale_card([{"tier": "Q5", "rule": "speed",
                             "reason": "no speed gain over the MagicQuant Q5."}])
    assert "measurably **faster**" in card
    assert "already matches or beats it" not in card


def test_an_unspecified_rule_does_not_inherit_the_dominance_claim():
    """reject()'s old rule default was "dominance", so a tier dropped merely
    because its perplexity run produced nan was published with "a smaller
    tier already beats it" -- false, and contradicting the bullet above it.
    An unknown rule must fall back to something true in every case."""
    card = _rationale_card([{"tier": "Q5",
                             "reason": "It did not meet the publishing criteria."}])
    assert "already matches or beats it" not in card
    assert "which of those it did not clear" in card


def test_a_mixed_rule_set_does_not_assert_any_single_rule():
    card = _rationale_card([
        {"tier": "Q5", "rule": "band", "reason": "landed in the Q6 band."},
        {"tier": "Q6", "rule": "dominance", "reason": "beaten by Q5."},
    ])
    assert "which of those it did not clear" in card
    assert "already matches or beats it" not in card
    assert "withheld rather than shipped under" not in card


def test_a_uniform_dominance_set_still_gets_the_dominance_rationale():
    card = _rationale_card([{"tier": "Q6", "rule": "dominance",
                             "reason": "beaten by Q5."}])
    assert "already matches or beats it" in card
