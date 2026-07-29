"""Shared REAP (expert-pruning) helpers used by both the CLI and the UI.

Centralizes three things the CLI and UI previously duplicated:
  - the set of REAP-supported architectures (as ``architectures[0]`` class
    names, cross-checked against reap.model_util.MODEL_ATTRS),
  - the configurable path to the REAP ``src/`` tree, and
  - artifact-source-resolution priority (reap > heretic > merged > gguf).
"""

import json
import logging
import os
from pathlib import Path
from typing import Dict, List, Optional

_log = logging.getLogger(__name__)

# REAP-supported architectures.
#
# ``_detect_model_arch`` returns ``config.json["architectures"][0]``, which is a
# *class name* (e.g. "Qwen3MoeForCausalLM"). The previous list mixed in HF
# repo-id strings ("Qwen3-Coder-30B-A3B-Instruct", "gpt-oss-20b") that can never
# match that output, so those models were silently skipped. This set contains
# only class-name strings; cross-check against reap.model_util.MODEL_ATTRS when
# REAP is installed.
REAP_SUPPORTED_ARCHS = frozenset({
    "Qwen3MoeForCausalLM",
    "NonUniformQwen3MoeForCausalLM",
    "Llama4ForCausalLM",
    "MixtralForCausalLM",
    "DeepseekV2ForCausalLM",
    "DeepseekV3ForCausalLM",
    "Ernie4_5_MoEForCausalLM",
    "Ernie4_5_MoeForCausalLM",
    "GptOssForCausalLM",
    "Glm4MoeForCausalLM",
    # poolside/Laguna-* (custom_code arch "laguna"). Fused 3D-nn.Parameter
    # experts + a sigmoid router at .gate + an unpruned per-block shared expert;
    # supported via the laguna entries in reap.model_util.MODEL_ATTRS and
    # reap.observer.OBSERVER_CONFIG_REGISTRY.
    "LagunaForCausalLM",
})


# ---------------------------------------------------------------------------
# Upstream drift check: REAP_SUPPORTED_ARCHS above is Foundry POLICY (hand-
# curated, deliberately fixed to use real class names -- see the frozenset's
# preceding comment and test_reap_arch.py's test_repo_id_entries_are_gone /
# test_gpt_oss_class_name_present), NOT a value that should be silently
# replaced by whatever reap.model_util.MODEL_ATTRS currently contains.
#
# reap.model_util.MODEL_ATTRS is a genuine upstream FACT (what the reap
# package itself has registered) but is known to mix real HF architecture
# class names with repo-id-shaped keys ("gpt-oss-20b",
# "Qwen3-Coder-30B-A3B-Instruct") that can never equal detect_model_arch()'s
# output (config.json's architectures[0] is always a valid Python class
# name / identifier) -- and, as of the reap source tree checked at
# /server/programming/reap/src, MODEL_ATTRS has no entry at all for
# GptOssForCausalLM or DeepseekV3ForCausalLM even though Foundry has
# separately verified REAP support for both. Blindly deriving
# REAP_SUPPORTED_ARCHS from MODEL_ATTRS would therefore both add dead
# entries and silently DROP two real, working architectures -- worse than
# the hand-curated literal, not more correct.
#
# So: policy (REAP_SUPPORTED_ARCHS) stays local and authoritative. When the
# `reap` package's model_util module happens to be importable in this
# process, we opportunistically diff its MODEL_ATTRS keys against the
# literal and log a warning naming any disagreement, so a human notices and
# re-verifies -- never to change behavior on its own.
# ---------------------------------------------------------------------------

def _reap_supported_archs_diff(model_util_module) -> Optional[Dict[str, List[str]]]:
    """Pure comparison of REAP_SUPPORTED_ARCHS against a
    reap.model_util-shaped module object's MODEL_ATTRS registry.

    ``model_util_module`` need only duck-type reap.model_util (expose a
    ``MODEL_ATTRS`` mapping) -- tests inject a fake module object rather
    than requiring the real (heavy, optional) ``reap`` package to be
    installed.

    Returns ``None`` if ``model_util_module`` has no ``MODEL_ATTRS``
    attribute (not a valid model_util module). Otherwise returns
    ``{"missing_from_literal": [...], "extra_in_literal": [...]}`` (each
    possibly empty, sorted). Never logs, never raises -- callers decide
    what to do with the result (see :func:`warn_if_reap_supported_archs_stale`).

    ``missing_from_literal`` is restricted to identifier-shaped upstream
    keys (``str.isidentifier()``): a HF architecture class name is always a
    valid Python identifier, so a non-identifier upstream key (the known
    repo-id-shaped quirks above) can never match anything
    :func:`detect_model_arch` produces and is not a meaningful "missing"
    diff.
    """
    attrs = getattr(model_util_module, "MODEL_ATTRS", None)
    if attrs is None:
        return None
    upstream = frozenset(attrs)
    missing = sorted(k for k in (upstream - REAP_SUPPORTED_ARCHS) if k.isidentifier())
    extra = sorted(REAP_SUPPORTED_ARCHS - upstream)
    return {"missing_from_literal": missing, "extra_in_literal": extra}


def warn_if_reap_supported_archs_stale(model_util_module=None) -> None:
    """Best-effort diagnostic: log a warning naming any disagreement between
    REAP_SUPPORTED_ARCHS (Foundry policy) and the installed ``reap``
    package's own reap.model_util.MODEL_ATTRS registry (upstream fact).

    Never changes REAP_SUPPORTED_ARCHS and never raises. If
    ``model_util_module`` is not given, tries ``import reap.model_util`` and
    silently does nothing (the normal case) if that fails -- reap's src tree
    is only put on sys.path (and its heavy optional deps stubbed) inside a
    generated REAP subprocess script (see :func:`install_reap_stubs` /
    :func:`reap_stub_block`), so it is not importable in most processes that
    import this module. This makes the derive/compare path opportunistic:
    it activates whenever reap happens to be importable, and is a silent
    no-op otherwise -- the hand-curated literal is always what's actually
    used by :func:`stage_reap` and the UI.
    """
    if model_util_module is None:
        try:
            import reap.model_util as model_util_module  # type: ignore[import-not-found]
        except ImportError:
            return
    diff = _reap_supported_archs_diff(model_util_module)
    if not diff:
        return
    missing, extra = diff["missing_from_literal"], diff["extra_in_literal"]
    if not missing and not extra:
        return
    parts = []
    if missing:
        parts.append(
            f"reap.model_util.MODEL_ATTRS has {missing} that "
            f"REAP_SUPPORTED_ARCHS is missing"
        )
    if extra:
        parts.append(
            f"REAP_SUPPORTED_ARCHS has {extra} not present in "
            f"reap.model_util.MODEL_ATTRS"
        )
    _log.warning(
        "REAP_SUPPORTED_ARCHS (core/reap_common.py, hand-curated policy) "
        "may be stale relative to the installed reap package's own "
        "registry: %s. Re-verify before changing the literal -- "
        "reap.model_util.MODEL_ATTRS is known to mix real class names with "
        "repo-id-shaped keys that can never match a detected architecture "
        "(see the comment above REAP_SUPPORTED_ARCHS).",
        "; ".join(parts),
    )


# Opportunistic: a no-op unless `reap` is already importable in this process.
warn_if_reap_supported_archs_stale()

# Default location of the REAP source tree. Overridable via FOUNDRY_REAP_SRC.
DEFAULT_REAP_SRC = "/server/programming/reap/src"

# Heavy optional REAP deps stubbed out at subprocess startup so importing
# ``reap.prune`` doesn't drag in vllm/lm_eval/etc. (whose pinned versions would
# break Foundry's ROCm torch stack).
REAP_STUB_MODULES = [
    "vllm", "vllm.entrypoints", "vllm.entrypoints.openai",
    "vllm.entrypoints.openai.api_server", "vllm.engine",
    "vllm.engine.arg_utils", "vllm.model_executor",
    "vllm.model_executor.models", "lm_eval", "lm_eval.utils",
    "evalplus", "evalplus.evaluate", "lcb_runner", "lcb_runner.runner",
    "lcb_runner.runner.main", "crfm_helm", "evalscope", "uvloop",
    "deepspeed", "wandb",
]


def reap_src_path() -> str:
    """Return the configured REAP src path (env override or default)."""
    return os.environ.get("FOUNDRY_REAP_SRC", DEFAULT_REAP_SRC)


def detect_model_arch(model_path: Path) -> Optional[str]:
    """Read config.json and return architectures[0], or None."""
    cfg_path = Path(model_path) / "config.json"
    if not cfg_path.exists():
        return None
    try:
        data = json.loads(cfg_path.read_text())
        archs = data.get("architectures") or []
        if archs and isinstance(archs, list):
            return archs[0]
    except (json.JSONDecodeError, OSError):
        pass
    return None


def reap_stub_block(src_path: Optional[str] = None) -> str:
    """Return the subprocess-script preamble that stubs REAP's heavy deps and
    inserts the REAP src path onto sys.path.

    ``src_path`` defaults to :func:`reap_src_path`; it is emitted via ``repr`` so
    it is configurable and injection-safe.
    """
    path = src_path if src_path is not None else reap_src_path()
    return (
        "import types, importlib.machinery\n"
        "\n"
        "def _stub(name):\n"
        "    m = types.ModuleType(name)\n"
        "    m.__spec__ = importlib.machinery.ModuleSpec(name, None)\n"
        "    sys.modules[name] = m\n"
        "    return m\n"
        "\n"
        f"for _name in {REAP_STUB_MODULES!r}:\n"
        "    _stub(_name)\n"
        "\n"
        "sys.modules['vllm'].TokensPrompt = type('TokensPrompt', (), {})\n"
        "sys.modules['vllm.entrypoints.openai.api_server'].run_server = lambda *a, **k: None\n"
        "sys.modules['vllm.engine.arg_utils'].AsyncEngineArgs = type('AsyncEngineArgs', (), {})\n"
        "sys.modules['vllm.model_executor.models'].ModelRegistry = type('ModelRegistry', (), {})\n"
        "sys.modules['lm_eval'].evaluator = type('evaluator', (), {})\n"
        "sys.modules['lm_eval.utils'].make_table = lambda *a, **k: None\n"
        "sys.modules['evalplus.evaluate'].evaluate = lambda *a, **k: None\n"
        "\n"
        f"sys.path.insert(0, {path!r})\n"
    )


def install_reap_stubs(src_path: Optional[str] = None) -> None:
    """Stub REAP's heavy optional deps and put the REAP src tree on sys.path.

    The programmatic equivalent of :func:`reap_stub_block` — used by the
    importable ``core/_reap_entry.py`` (audit H2) so the stage no longer relies
    on exec'ing a generated string. Must run before ``import reap.prune``.
    """
    import importlib.machinery
    import sys
    import types

    def _stub(name: str):
        m = types.ModuleType(name)
        m.__spec__ = importlib.machinery.ModuleSpec(name, None)
        sys.modules[name] = m
        return m

    for name in REAP_STUB_MODULES:
        _stub(name)

    sys.modules["vllm"].TokensPrompt = type("TokensPrompt", (), {})
    sys.modules["vllm.entrypoints.openai.api_server"].run_server = lambda *a, **k: None
    sys.modules["vllm.engine.arg_utils"].AsyncEngineArgs = type("AsyncEngineArgs", (), {})
    sys.modules["vllm.model_executor.models"].ModelRegistry = type("ModelRegistry", (), {})
    sys.modules["lm_eval"].evaluator = type("evaluator", (), {})
    sys.modules["lm_eval.utils"].make_table = lambda *a, **k: None
    sys.modules["evalplus.evaluate"].evaluate = lambda *a, **k: None

    path = src_path if src_path is not None else reap_src_path()
    sys.path.insert(0, path)


def resolve_artifact_source(output_dir: Path, *, require_safetensors: bool = True):
    """Return the highest-priority existing model artifact under ``output_dir``.

    Priority: reap_model > heretic_model > merged_model > model-bf16.gguf.
    For safetensors stages, a directory must contain ``*.safetensors`` to count.
    Returns the resolved ``Path`` or ``None`` when nothing is found.
    """
    output_dir = Path(output_dir)
    for sub in ("reap_model", "heretic_model", "merged_model"):
        d = output_dir / sub
        if d.exists() and (not require_safetensors or any(d.glob("*.safetensors"))):
            return d
    gguf = output_dir / "model-bf16.gguf"
    if gguf.exists():
        return gguf
    return None
