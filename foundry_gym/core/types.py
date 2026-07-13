"""Core data types for foundry_gym.

Design constraints (see docs/WORKLOG.md):
- Tasks must be JSON-serializable end-to-end so the TRL reward adapter can
  round-trip them through a HuggingFace Dataset column.
- Determinism: a Task is fully reproducible from (env, task_params, seed);
  ``task_id`` is a content hash so accidental nondeterminism is detectable.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, asdict
from typing import Any


def canonical_json(value: Any) -> str:
    """Deterministic JSON encoding (sorted keys, no whitespace variance)."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def stable_hash(*parts: str) -> str:
    """sha256 over the given string parts, hex digest."""
    h = hashlib.sha256()
    for p in parts:
        h.update(p.encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()


@dataclass(frozen=True)
class Task:
    """A single verifiable task instance.

    Attributes:
        env: registry name of the generating environment family.
        task_id: content hash of (env, task_params, seed, payload) — stable.
        seed: the seed that generated this task.
        difficulty: resolved difficulty in [0, 1].
        prompt: the exact text presented to the policy model.
        task_params: the (normalized) generation parameters, for reproduction.
        payload: everything the verifier needs (hidden tests, goal predicates,
            ground truth, world definition...). Never shown to the policy.
        metadata: non-essential info (human-readable summary, sub-family, ...).
    """

    env: str
    task_id: str
    seed: int
    difficulty: float
    prompt: str
    task_params: dict = field(default_factory=dict)
    payload: dict = field(default_factory=dict)
    metadata: dict = field(default_factory=dict)

    def to_json(self) -> str:
        return canonical_json(asdict(self))

    @staticmethod
    def from_json(s: str) -> "Task":
        d = json.loads(s)
        return Task(
            env=d["env"],
            task_id=d["task_id"],
            seed=int(d["seed"]),
            difficulty=float(d["difficulty"]),
            prompt=d["prompt"],
            task_params=d.get("task_params", {}) or {},
            payload=d.get("payload", {}) or {},
            metadata=d.get("metadata", {}) or {},
        )


@dataclass
class VerifyResult:
    """Result of verifying a policy response against a task.

    reward is always a float in [0, 1]. diagnostics is JSON-serializable and
    explains the score (per-check outcomes, failure reasons, exploit gates hit).
    """

    reward: float
    diagnostics: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Hard clamp: no verifier may emit rewards outside [0, 1]. NaN → 0.
        r = float(self.reward)
        if r != r:  # NaN
            r = 0.0
        self.reward = min(1.0, max(0.0, r))

    def as_dict(self) -> dict:
        return {"reward": self.reward, "diagnostics": self.diagnostics}
