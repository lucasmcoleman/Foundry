"""Compatibility shim for amd-quark 0.11.x on torch nightlies.

amd-quark 0.11.x imports from `torch.ao.quantization.pt2e.utils`,
`torch.ao.quantization.pt2e.prepare`, and `torch.ao.quantization.quantizer`.
These modules were removed/restructured in recent torch nightlies (>= 2.10).
Quark uses them only for graph-mode/PT2E quantization workflows -- the LLM
AWQ flow Foundry runs never reaches that code.

This module installs stub modules with stub symbols. The stubs raise
NotImplementedError if actually called (they shouldn't be), making
debugging clear if the LLM AWQ path ever drifts to use these symbols.

It also stubs `quark.contrib.llm_eval.eval_model` as a no-op, because
amd-quark's reference `quantize_quark.py` script always imports and
calls eval_model() at the end of main() -- even when `--do-eval` is
not passed -- and importing the real module pulls in nltk/lm_eval/etc.
which aren't dependencies of amd-quark.

Idempotent and self-disabling: if torch already has the real modules,
this is a no-op. Safe to call multiple times.
"""

from __future__ import annotations

import sys
import types


def install() -> dict[str, bool]:
    """Install all needed shims. Returns a dict of {module_name: was_shimmed}.

    Call before `import quark.torch` (or before any subprocess that does so).
    Subsequent calls are no-ops.
    """
    actions = {}
    actions.update(_install_torch_pt2e_shim())
    actions.update(_install_torch_quantizer_shim())
    actions.update(_install_quark_eval_shim())
    return actions


def _shim_unavailable(*args, **kwargs):
    raise NotImplementedError(
        "amd-quark code path stubbed by core.quark_torch_compat -- "
        "this symbol is unavailable in this torch nightly. The LLM AWQ "
        "path should not reach it; if you see this, the workflow has "
        "drifted to use a graph-mode/PT2E feature that needs torch.ao.quantization.pt2e."
    )


def _make_stub(name: str) -> types.ModuleType:
    """Create or return a stub module under `name`."""
    if name in sys.modules:
        return sys.modules[name]
    m = types.ModuleType(name)
    sys.modules[name] = m
    return m


def _install_torch_pt2e_shim() -> dict[str, bool]:
    """Install torch.ao.quantization.pt2e.{utils,prepare} shims if missing."""
    actions = {}
    try:
        import torch.ao.quantization.pt2e.utils  # noqa: F401
        actions["torch.ao.quantization.pt2e"] = False
        return actions
    except ImportError:
        pass

    pt2e = _make_stub("torch.ao.quantization.pt2e")
    pt2e_utils = _make_stub("torch.ao.quantization.pt2e.utils")
    pt2e_prepare = _make_stub("torch.ao.quantization.pt2e.prepare")
    pt2e_utils._get_tensor_constant_from_node = _shim_unavailable
    pt2e_prepare._get_edge_or_node_to_group_id = _shim_unavailable
    pt2e_prepare._get_edge_or_node_to_qspec = _shim_unavailable
    # Suppress unused variable warnings — these are registered in sys.modules.
    _ = pt2e
    actions["torch.ao.quantization.pt2e"] = True
    return actions


def _install_torch_quantizer_shim() -> dict[str, bool]:
    """Install torch.ao.quantization.quantizer shim if missing."""
    actions = {}
    try:
        import torch.ao.quantization.quantizer  # noqa: F401
        actions["torch.ao.quantization.quantizer"] = False
        return actions
    except ImportError:
        pass

    qtzr = _make_stub("torch.ao.quantization.quantizer")

    class EdgeOrNode:
        """Stub for torch.ao.quantization.quantizer.EdgeOrNode."""
        pass

    qtzr.EdgeOrNode = EdgeOrNode
    actions["torch.ao.quantization.quantizer"] = True
    return actions


def _install_quark_eval_shim() -> dict[str, bool]:
    """Install a no-op for quark.contrib.llm_eval to avoid heavy eval deps.

    Quark's reference quantize_quark.py imports and calls eval_model() at
    the end of main(), even without --do-eval. The real module pulls in
    nltk, lm_eval, evalplus, etc., which amd-quark doesn't declare as deps.
    We don't evaluate; a no-op is fine.
    """
    actions = {}
    if "quark.contrib.llm_eval" in sys.modules:
        # Already imported (real or stub) -- leave it alone.
        actions["quark.contrib.llm_eval"] = False
        return actions

    llm_eval = _make_stub("quark.contrib.llm_eval")
    llm_eval.eval_model = lambda *a, **k: None  # no-op
    actions["quark.contrib.llm_eval"] = True
    return actions
