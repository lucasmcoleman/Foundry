"""Environment registry.

Usage:
    from foundry_gym import registry
    env = registry.get("math_logic")
    task = env.generate({"difficulty": 0.7}, seed=42)
    result = env.verify(task, response_text)
"""

from __future__ import annotations

from typing import Dict, List, Type

from .env import Environment

_REGISTRY: Dict[str, Environment] = {}


def register(env_cls: Type[Environment]) -> Type[Environment]:
    """Class decorator: instantiate and register an environment family."""
    if not env_cls.name:
        raise ValueError(f"{env_cls.__name__} must set a non-empty .name")
    if env_cls.name in _REGISTRY:
        raise ValueError(f"duplicate environment name: {env_cls.name!r}")
    _REGISTRY[env_cls.name] = env_cls()
    return env_cls


def get(name: str) -> Environment:
    _ensure_loaded()
    try:
        return _REGISTRY[name]
    except KeyError:
        raise KeyError(
            f"unknown environment {name!r}; available: {sorted(_REGISTRY)}"
        ) from None


def names() -> List[str]:
    _ensure_loaded()
    return sorted(_REGISTRY)


def all_envs() -> List[Environment]:
    _ensure_loaded()
    return [_REGISTRY[n] for n in sorted(_REGISTRY)]


_loaded = False


def _ensure_loaded() -> None:
    """Import the envs package once so families self-register."""
    global _loaded
    if not _loaded:
        _loaded = True
        from .. import envs  # noqa: F401  (import side effect registers families)
