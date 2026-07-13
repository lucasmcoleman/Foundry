"""TRL GRPOTrainer integration.

Conforms to TRL >= 1.0 ``reward_funcs``: a callable
``(prompts, completions, **dataset_columns) -> list[float | None]``.
Tasks ride along inside the dataset as a ``task_json`` column (Tasks are
JSON-serializable by design), so the reward function is stateless and
deterministic: reward = env.verify(task, completion).reward.

Usage:

    from foundry_gym.training import build_dataset, gym_reward
    ds = build_dataset(["math_logic", "code_repair"], n_per_family=64, seed_base=0)
    trainer = GRPOTrainer(model=..., reward_funcs=gym_reward,
                          args=GRPOConfig(...), train_dataset=ds)
"""

from __future__ import annotations

import sys
from typing import List, Optional, Sequence

from ..core.types import Task
from ..core import registry

# difficulty curriculum used when a spread (not a fixed value) is requested
DEFAULT_DIFFICULTIES = (0.1, 0.3, 0.5, 0.7, 0.9)


def _completion_text(completion) -> str:
    """Normalize a TRL completion (standard str or conversational messages)."""
    if isinstance(completion, str):
        return completion
    if isinstance(completion, list):  # [{"role": "assistant", "content": ...}]
        parts = []
        for msg in completion:
            if isinstance(msg, dict):
                content = msg.get("content", "")
                if isinstance(content, str):
                    parts.append(content)
        return "\n".join(parts)
    return str(completion)


def gym_reward(prompts: Sequence, completions: Sequence,
               task_json: Optional[Sequence[str]] = None, **kwargs) -> List[float]:
    """TRL reward function: verify each completion against its task.

    Defensive by design: a reward function that raises kills a training run
    hours in, so per-sample failures are caught, reported to stderr, and
    scored 0.0 (env verifiers themselves are contractually non-raising; this
    guards adapter-level surprises like a malformed task_json column).
    """
    if task_json is None:
        raise ValueError(
            "gym_reward needs the dataset to carry a 'task_json' column "
            "(build the dataset with foundry_gym.training.build_dataset)"
        )
    rewards: List[float] = []
    for tj, completion in zip(task_json, completions):
        try:
            task = Task.from_json(tj)
            env = registry.get(task.env)
            text = _completion_text(completion)
            rewards.append(float(env.verify(task, text).reward))
        except Exception as e:  # noqa: BLE001 — never kill the train loop
            print(f"[foundry_gym.gym_reward] scoring error -> reward 0.0: "
                  f"{type(e).__name__}: {e}", file=sys.stderr)
            rewards.append(0.0)
    return rewards


def build_tasks(families: Sequence[str], n_per_family: int, seed_base: int = 0,
                difficulty: Optional[float] = None) -> List[Task]:
    """Deterministically generate the task list backing a training dataset."""
    tasks: List[Task] = []
    for family in families:
        env = registry.get(family)
        for i in range(n_per_family):
            d = (difficulty if difficulty is not None
                 else DEFAULT_DIFFICULTIES[i % len(DEFAULT_DIFFICULTIES)])
            tasks.append(env.generate({"difficulty": d}, seed=seed_base + i))
    return tasks


def build_dataset(families: Sequence[str], n_per_family: int = 64,
                  seed_base: int = 0, difficulty: Optional[float] = None,
                  system_prompt: Optional[str] = None, conversational: bool = False):
    """Build a HuggingFace Dataset for GRPOTrainer.

    Columns: ``prompt`` (str, or chat messages when conversational=True) and
    ``task_json`` (the serialized Task consumed by :func:`gym_reward`).
    """
    from datasets import Dataset  # lazy: keep core import light

    tasks = build_tasks(families, n_per_family, seed_base, difficulty)
    rows = []
    for t in tasks:
        if conversational:
            msgs = []
            if system_prompt:
                msgs.append({"role": "system", "content": system_prompt})
            msgs.append({"role": "user", "content": t.prompt})
            prompt = msgs
        else:
            prompt = t.prompt if not system_prompt else f"{system_prompt}\n\n{t.prompt}"
        rows.append({"prompt": prompt, "task_json": t.to_json(),
                     "env": t.env, "difficulty": t.difficulty})
    return Dataset.from_list(rows)
