"""The LM head must not be quantized down with the body in uniform presets.

INCIDENT (2026-08-04): our published Fable-Fusion Q8_0_ROCMFPX quantized
`output.weight` along with everything else. Every reference build of the same
model protects it -- DavidAU's NEO-MAX Q8 keeps it at BF16 and says so
explicitly ("the output tensor (10-20% of output) was modified to full
precision"). Header diff of the two files:

    ours    866 tensors  {Q8_0_ROCMFPX: 506, F32: 360}   output.weight -> Q8_0_ROCMFPX
    NEO-MAX 866 tensors  {Q8_0: 505, BF16: 1, F32: 360}  output.weight -> BF16

`output.weight` emits the logits, so error there lands directly on token
probabilities and therefore on sampling behavior -- the reported symptom was
shorter, blander generations. It is ~4.7% of parameters (5120 x 248320) yet
was sitting at the file's LOWEST precision.

The mq-* hybrid path is NOT affected: it gets per-group precision from
MagicQuant's `H` (head) group. Only uniform presets were blind, because they
apply one type to every tensor by construction.
"""
import pytest

from core._rocmfpx_entry import (
    DEFAULT_HEAD_TYPE,
    _preset_quantize_cmd,
)


def _cmd(ggml_type="Q8_0_ROCMFPX", head_type=DEFAULT_HEAD_TYPE, imatrix=None):
    return _preset_quantize_cmd(
        quantize_bin="/x/llama-quantize",
        allow_requantize=False,
        imatrix=imatrix,
        bf16_gguf="/x/model-bf16.gguf",
        out_path="/x/out.gguf",
        ggml_type=ggml_type,
        head_type=head_type,
    )


def test_head_is_protected_by_default():
    cmd = _cmd()
    assert "--output-tensor-type" in cmd
    assert cmd[cmd.index("--output-tensor-type") + 1] == "bf16"


def test_default_head_type_is_bf16():
    """Pinned: the reference builds use BF16, and a silent drift to a
    quantized head is the whole defect this guards."""
    assert DEFAULT_HEAD_TYPE == "bf16"


def test_flag_precedes_positional_args():
    """llama-quantize takes options before `in out type`; an option emitted
    after the positionals is parsed as a positional and the call fails."""
    cmd = _cmd()
    assert cmd.index("--output-tensor-type") < cmd.index("/x/model-bf16.gguf")
    assert cmd[-3:] == ["/x/model-bf16.gguf", "/x/out.gguf", "Q8_0_ROCMFPX"]


@pytest.mark.parametrize("body", ["bf16", "BF16", "f16", "F16", "f32"])
def test_skipped_when_body_is_already_full_precision(body):
    """Forcing a BF16 head on an F16/BF16/F32 body only adds a redundant
    flag -- and for F32 it would DOWNGRADE the head."""
    assert "--output-tensor-type" not in _cmd(ggml_type=body)


def test_can_be_disabled_explicitly():
    assert "--output-tensor-type" not in _cmd(head_type=None)


def test_head_protection_does_not_disturb_imatrix():
    cmd = _cmd(imatrix="/x/imatrix.dat")
    assert cmd[cmd.index("--imatrix") + 1] == "/x/imatrix.dat"
    assert "--output-tensor-type" in cmd


def test_low_bit_presets_get_it_too():
    """ROCMFP3/4 bodies are exactly where an unprotected head hurts most --
    the precision gap between body and head is largest there."""
    for t in ("Q3_0_ROCMFPX", "Q4_0_ROCMFP4", "Q6_0_ROCMFPX"):
        assert "--output-tensor-type" in _cmd(ggml_type=t), t
