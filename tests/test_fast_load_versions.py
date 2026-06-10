"""L-fast-load-hack: version guard warns on out-of-range internals.

The version logic is pure string handling, but it lives in
core/fast_train_zeroclaw.py which imports torch at module load. We import torch
first (skip if unavailable) so this runs in the GPU dev venv and skips cleanly in
a torch-less CI.
"""

import pytest

pytest.importorskip("torch")

from fast_train_zeroclaw import check_internals_versions


def test_known_good_versions_pass():
    assert check_internals_versions("4.55.0", "1.0.0") == []
    assert check_internals_versions("4.40.0", "0.30.0") == []  # lower bounds


def test_too_new_transformers_warns():
    warnings = check_internals_versions("5.1.0", "1.0.0")
    assert any("transformers" in w for w in warnings)


def test_too_old_accelerate_warns():
    warnings = check_internals_versions("4.55.0", "0.20.0")
    assert any("accelerate" in w for w in warnings)


def test_both_out_of_range_warns_both():
    warnings = check_internals_versions("3.0.0", "0.1.0")
    assert len(warnings) == 2


def test_dev_suffix_is_tolerated():
    # e.g. '4.56.0.dev0' should parse to (4, 56, 0) and pass.
    assert check_internals_versions("4.56.0.dev0", "1.2.3") == []
