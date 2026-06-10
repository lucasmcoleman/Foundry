"""Shared REAP (expert-pruning) helpers used by both the CLI and the UI.

Centralizes three things the CLI and UI previously duplicated:
  - the set of REAP-supported architectures (as ``architectures[0]`` class
    names, cross-checked against reap.model_util.MODEL_ATTRS),
  - the configurable path to the REAP ``src/`` tree, and
  - artifact-source-resolution priority (reap > heretic > merged > gguf).
"""

import json
import os
from pathlib import Path
from typing import Optional

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
})

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
