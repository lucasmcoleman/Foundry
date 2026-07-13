"""foundry_gym — RL environments with programmatic verifiers for Foundry.

Common interface (see README.md):

    from foundry_gym import registry
    env = registry.get("math_logic")
    task = env.generate({"difficulty": 0.6}, seed=7)     # deterministic
    result = env.verify(task, model_response_text)        # {reward, diagnostics}

Rewards are deterministic functions of the policy model's own outputs.
No frontier-model outputs are stored as training targets anywhere.
"""

from .core import Environment, Task, VerifyResult, registry

__version__ = "0.1.0"

__all__ = ["Environment", "Task", "VerifyResult", "registry", "__version__"]
