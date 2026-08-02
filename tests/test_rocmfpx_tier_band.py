"""A ROCmFPX render must land in the tier band it claims.

The ROCmFPX family is sparse -- ROCMFP3/4/6/8 at 3.5/4.5/6.5/8.25 bpw, with no
5-bit type -- so SCHEME_TO_ROCMFPX rounds Q5_K *up* to Q6_0_ROCMFPX. A Q5 tier
can therefore render larger than the same model's Q6 tier.

Measured on Qwen3.6-35B-A3B (BF16 baseline 66.15 GiB), which is where this was
found:

    mq-q4  21.18 GiB  PPL 6.7680   pp512 808 t/s  tg128 33.75 t/s
    mq-q5  27.51 GiB  PPL 7.0702           <- larger than q6, worse than q4
    mq-q6  26.94 GiB  PPL 7.4095  pp512 183 t/s  tg128 22.10 t/s

mq-q5 and mq-q6 are both strictly dominated by mq-q4. Only q5 is *mislabelled*
though, and that is what this guard catches: it checks the BAND, not quality.
Quality dominance is reselect_tiers' job.

The guard predicts rather than measures, because a 35B render costs minutes of
quantize time to produce a file that cannot be shipped.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core"))

import _rocmfpx_entry as entry


# Per-group parameter counts roughly matching Qwen3.6-35B-A3B: a MoE model
# whose expert group dominates, so X's mapped type decides the band.
GROUPS = {
    "X": 30_000_000_000, "U": 1_200_000_000, "D": 900_000_000,
    "Q": 700_000_000, "O": 700_000_000, "K": 200_000_000,
    "E": 500_000_000, "H": 500_000_000, "S": 300_000_000,
    "R": 20_000_000, "N": 5_000_000,
}


class _StubReader:
    """Stands in for GGUFReader: one synthetic tensor per group."""

    def __init__(self, path):
        self.path = path

    def open(self):
        pass

    def close(self):
        pass

    def get_tensor_names(self):
        return [f"{g}::t" for g in GROUPS]

    def get_tensor_info(self, name):
        return {"shape": [GROUPS[name.split("::")[0]]]}


class _StubClassifier:
    GROUP_PATTERNS = {}

    @staticmethod
    def classify_tensor(name):
        return name.split("::")[0]


@pytest.fixture
def patched(monkeypatch):
    import magicquant.gguf.reader as reader_mod
    import magicquant.gguf.tensor_groups as tg_mod
    monkeypatch.setattr(reader_mod, "GGUFReader", _StubReader)
    monkeypatch.setattr(tg_mod, "TensorGroupClassifier", _StubClassifier)
    return None


def _uniform(scheme):
    return {g: scheme for g in GROUPS if g != "N"}


def test_q4_config_renders_into_the_q4_band(patched):
    _, _, tier = entry.predict_rendered_tier(_uniform("Q4_K_M"), "stub.gguf")
    assert tier == "Q4"


def test_q6_config_renders_into_the_q6_band(patched):
    _, _, tier = entry.predict_rendered_tier(_uniform("Q6_K"), "stub.gguf")
    assert tier == "Q6"


def test_q5_config_renders_ABOVE_its_band(patched):
    """The defect: no 5-bit ROCmFPX type, so Q5_K rounds up to 6.5 bpw."""
    _, _, tier = entry.predict_rendered_tier(_uniform("Q5_K"), "stub.gguf")
    assert tier != "Q5"
    assert tier == "Q6"


def test_q5_renders_no_smaller_than_q6(patched):
    """Which is why the mq-q5 artifact was bigger than mq-q6 on a real model."""
    q5, _, _ = entry.predict_rendered_tier(_uniform("Q5_K"), "stub.gguf")
    q6, _, _ = entry.predict_rendered_tier(_uniform("Q6_K"), "stub.gguf")
    assert q5 >= q6


def test_translate_scheme_rounds_q5_up_not_down(patched):
    """Pin the mechanism itself, so a future remap is a deliberate choice."""
    assert entry.translate_scheme("Q5_K") == "Q6_0_ROCMFPX"
    assert entry.translate_scheme("Q4_K_M") == "Q4_0_ROCMFP4"


def test_untouched_groups_do_not_break_prediction(patched):
    """Groups absent from the config (norms) are counted, not crashed on."""
    cfg = _uniform("Q4_K_M")
    cfg.pop("R")
    gib, base, tier = entry.predict_rendered_tier(cfg, "stub.gguf")
    assert gib > 0 and base > gib
    assert tier == "Q4"


def test_baseline_is_the_bf16_size(patched):
    _, base, _ = entry.predict_rendered_tier(_uniform("Q6_K"), "stub.gguf")
    expected = sum(GROUPS.values()) * 16.0 / 8.0 / 2 ** 30
    assert base == pytest.approx(expected, rel=1e-9)
