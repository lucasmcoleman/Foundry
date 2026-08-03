"""Differential test: fast_export's streaming LoRA merge vs PEFT's own merge.

core/fast_export.py's module docstring states it "re-implements PEFT's LoRA
merge (W + scaling * B @ A) for streaming performance" -- a deliberate
mechanics divergence (shard-by-shard vs whole-model) that must stay
MATH-equivalent to what PEFT itself produces via ``merge_and_unload()``,
including the scaling convention (plain alpha/r vs rsLoRA's alpha/sqrt(r)).

Two pieces of fast_export.py are exercised:
  - ``build_lora_map()`` (core/fast_export.py) -- factored/importable, called
    directly here. It reads {"r", "lora_alpha", "use_rslora"} from the adapter
    config dict and computes ``scaling = alpha / sqrt(r)`` when
    ``use_rslora`` is set, else ``alpha / r``.
  - The per-tensor merge expression inside ``streaming_merge()``'s shard loop
    -- NOT factored into a separate callable, so it is replicated verbatim
    below (see ``_fast_export_merge_tensor``, which quotes the exact lines
    from core/fast_export.py it mirrors). If that inline expression changes,
    update the quoted lines and this helper together.

INCIDENT (now fixed): this differential test originally caught
``build_lora_map()`` reading only ``lora_config["r"]`` and
``lora_config["lora_alpha"]`` and never ``use_rslora`` -- so it always
computed ``alpha / r``, even when the adapter was trained with rsLoRA
(``alpha / sqrt(r)``). core/pipeline.py's TrainingConfig defaults
``use_rslora=True``, so this was not a theoretical edge case: the *default*
Foundry training config produced adapters whose PEFT-correct merge scaling
fast_export's export stage silently got wrong. ``build_lora_map`` has since
been patched to read ``use_rslora`` and branch on it (see the incident note
on that function in core/fast_export.py); the rsLoRA case below was a strict
``xfail`` documenting the bug and is now a plain passing regression test.
"""

import copy
import math

import pytest

# torch is part of the heavy ML stack that CI deliberately does not install. A
# bare `import torch` raised at COLLECTION time, and a collection error aborts
# the whole pytest run instead of skipping one module -- so this line alone
# turned every CI run red. Guard it like the importorskip below already does.
torch = pytest.importorskip("torch")
nn = torch.nn

peft = pytest.importorskip("peft")
from peft import LoraConfig, get_peft_model  # noqa: E402

from core.fast_export import build_lora_map  # noqa: E402

pytestmark = pytest.mark.filterwarnings("ignore")


class _ToyBlock(nn.Module):
    """Stand-in for one decoder layer's attention/MLP projections.

    Named so PEFT's key convention
    ("base_model.model.<path>.lora_A.default.weight") and fast_export's key
    stripping in ``build_lora_map`` (drop "base_model.model.", drop
    ".lora_A.weight") land on the same base-model key fast_export expects
    from a real checkpoint, e.g. "model.layers.0.self_attn.q_proj.weight".
    """

    def __init__(self):
        super().__init__()
        self.self_attn = nn.Module()
        self.self_attn.q_proj = nn.Linear(16, 16, bias=False)
        self.mlp = nn.Module()
        self.mlp.down_proj = nn.Linear(32, 16, bias=False)


class _ToyModel(nn.Module):
    def __init__(self):
        super().__init__()
        self.model = nn.Module()
        self.model.layers = nn.ModuleList([_ToyBlock()])

    def forward(self, x):
        # merge_and_unload() never calls forward(); unused but required for
        # get_peft_model() to treat this as a well-formed nn.Module.
        return x


def _build_toy(seed: int) -> _ToyModel:
    g = torch.Generator().manual_seed(seed)
    model = _ToyModel().float()
    with torch.no_grad():
        model.model.layers[0].self_attn.q_proj.weight.copy_(
            torch.randn(16, 16, generator=g)
        )
        model.model.layers[0].mlp.down_proj.weight.copy_(
            torch.randn(16, 32, generator=g)
        )
    return model


def _randomize_lora_weights(peft_model, seed: int) -> None:
    """PEFT zero-inits lora_B by default, so an unperturbed merge would equal
    the base weight regardless of scaling -- give both A and B nonzero random
    values so a scaling error actually shows up in the merged weight."""
    g = torch.Generator().manual_seed(seed)
    for module in peft_model.modules():
        lora_a = getattr(module, "lora_A", None)
        lora_b = getattr(module, "lora_B", None)
        if lora_a is None or "default" not in lora_a:
            continue
        with torch.no_grad():
            lora_a["default"].weight.copy_(
                torch.randn(lora_a["default"].weight.shape, generator=g)
            )
            lora_b["default"].weight.copy_(
                torch.randn(lora_b["default"].weight.shape, generator=g)
            )


def _fast_export_merge_tensor(base_weight, a_weight, b_weight, scaling):
    """Verbatim replica of the per-tensor merge inside streaming_merge()'s
    shard loop (core/fast_export.py) -- not factored into a callable there:

        w = shard_data[name].to(DEVICE, dtype=torch.float32)
        delta = scaling * (b_weight.float() @ a_weight.float())
        shard_data[name] = (w + delta).to(dtype=orig_dtype).cpu()

    DEVICE/.cpu() are dropped here (CPU-only test); the arithmetic is exact.
    """
    orig_dtype = base_weight.dtype
    w = base_weight.to(dtype=torch.float32)
    delta = scaling * (b_weight.float() @ a_weight.float())
    return (w + delta).to(dtype=orig_dtype)


def _run_case(r: int, alpha: int, use_rslora: bool, seed: int = 0):
    """Merge a toy model's two LoRA-targeted Linears both ways.

    Returns ((fast_export_q, peft_q), (fast_export_down, peft_down)).
    """
    base = _build_toy(seed)

    lora_config = LoraConfig(
        r=r,
        lora_alpha=alpha,
        target_modules=["q_proj", "down_proj"],
        use_rslora=use_rslora,
        bias="none",
    )
    peft_model = get_peft_model(copy.deepcopy(base), lora_config)
    _randomize_lora_weights(peft_model, seed + 1)

    # --- PEFT's own ground-truth merge ---
    merged_ref = copy.deepcopy(peft_model).merge_and_unload()
    ref_q = merged_ref.model.layers[0].self_attn.q_proj.weight.detach().clone()
    ref_down = merged_ref.model.layers[0].mlp.down_proj.weight.detach().clone()

    # --- fast_export's path: build_lora_map() + the replicated merge expr ---
    # peft_model.base_model.model is the wrapped original _ToyModel; the
    # LoraLinear modules expose .lora_A/.lora_B keyed by adapter name.
    q_layer = peft_model.base_model.model.model.layers[0].self_attn.q_proj
    d_layer = peft_model.base_model.model.model.layers[0].mlp.down_proj
    lora_weights = {
        "base_model.model.model.layers.0.self_attn.q_proj.lora_A.weight":
            q_layer.lora_A["default"].weight.detach().clone(),
        "base_model.model.model.layers.0.self_attn.q_proj.lora_B.weight":
            q_layer.lora_B["default"].weight.detach().clone(),
        "base_model.model.model.layers.0.mlp.down_proj.lora_A.weight":
            d_layer.lora_A["default"].weight.detach().clone(),
        "base_model.model.model.layers.0.mlp.down_proj.lora_B.weight":
            d_layer.lora_B["default"].weight.detach().clone(),
    }
    lora_config_dict = {
        "r": r, "lora_alpha": alpha, "target_modules": ["q_proj", "down_proj"],
        "use_rslora": use_rslora,
    }
    lora_map = build_lora_map(lora_config_dict, lora_weights)

    a_q, b_q, scaling_q = lora_map["model.layers.0.self_attn.q_proj.weight"]
    a_d, b_d, scaling_d = lora_map["model.layers.0.mlp.down_proj.weight"]

    got_q = _fast_export_merge_tensor(
        base.model.layers[0].self_attn.q_proj.weight, a_q, b_q, scaling_q
    )
    got_down = _fast_export_merge_tensor(
        base.model.layers[0].mlp.down_proj.weight, a_d, b_d, scaling_d
    )

    return (got_q, ref_q), (got_down, ref_down)


@pytest.mark.parametrize(
    "r,alpha",
    [
        (4, 8),    # alpha == 2r
        (8, 8),    # alpha == r (scaling == 1)
        (4, 16),   # alpha == 4r
    ],
)
def test_plain_lora_matches_peft_merge(r, alpha):
    """fast_export's scaling = alpha/r must match PEFT's plain-LoRA merge
    exactly (both compute the same alpha/r scaling; this locks the matmul +
    dtype-cast expression, not just the scaling formula)."""
    (got_q, ref_q), (got_down, ref_down) = _run_case(r, alpha, use_rslora=False)
    torch.testing.assert_close(got_q, ref_q, atol=1e-5, rtol=1e-5)
    torch.testing.assert_close(got_down, ref_down, atol=1e-5, rtol=1e-5)


def test_rslora_merge_matches_peft():
    """build_lora_map() now reads adapter_config.json's 'use_rslora' field and
    computes scaling = alpha / sqrt(r) for rsLoRA adapters, matching PEFT's own
    merge_and_unload() (see the fix + incident note on build_lora_map in
    core/fast_export.py). Was xfail(strict=True) documenting the divergence
    this differential test caught; now a plain passing regression test."""
    r, alpha = 4, 8  # sqrt(4) = 2, so alpha/r=2.0 vs alpha/sqrt(r)=4.0 -- a
    # large, unmistakable gap, not a rounding-level difference.
    (got_q, ref_q), (got_down, ref_down) = _run_case(r, alpha, use_rslora=True)
    torch.testing.assert_close(got_q, ref_q, atol=1e-5, rtol=1e-5)
    torch.testing.assert_close(got_down, ref_down, atol=1e-5, rtol=1e-5)


def test_rslora_scaling_formula_sanity():
    """Non-xfail control: confirms PEFT really does use alpha/sqrt(r) for
    rsLoRA (so the xfail above is measuring a real formula gap, not a test
    bug). If PEFT's convention ever changes, this fails independently of the
    merge test above and points straight at the assumption to revisit."""
    r, alpha = 4, 8
    lora_config = LoraConfig(
        r=r, lora_alpha=alpha, target_modules=["q_proj"], use_rslora=True, bias="none",
    )
    base = _build_toy(seed=0)
    peft_model = get_peft_model(copy.deepcopy(base), lora_config)
    layer = peft_model.base_model.model.model.layers[0].self_attn.q_proj
    expected = alpha / math.sqrt(r)
    assert layer.scaling["default"] == pytest.approx(expected)
    assert layer.scaling["default"] != pytest.approx(alpha / r)
