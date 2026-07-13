"""Environment base class and determinism helpers."""

from __future__ import annotations

import random
from abc import ABC, abstractmethod
from typing import Any, Optional

from .types import Task, VerifyResult, canonical_json, stable_hash


class Environment(ABC):
    """Base class for a task family.

    Contract:
    - ``generate(task_params, seed)`` is deterministic: identical inputs produce
      a byte-identical ``Task`` (including task_id). All randomness must come
      from ``self.rng(seed, task_params)``; never touch the global RNG, never
      rely on set/dict iteration order of non-literal collections.
    - ``verify(task, response)`` is a pure function of (task, response) — no
      network, no wall-clock dependence in the score (timeouts guard resources
      but a response that verifies must verify every time).
    - Rewards live in [0, 1].
    - ``reference_solution`` / ``corrupted_solution`` exist for soundness
      demonstrations and tests: the reference must score >= reference_threshold,
      the corruption (a *plausible* wrong answer, not garbage) must score
      <= corrupted_threshold.
    """

    #: registry name, e.g. "code_repair"
    name: str = ""
    #: soundness thresholds used by tests and scripts/demo_soundness.py
    reference_threshold: float = 0.95
    corrupted_threshold: float = 0.35

    # -- determinism helpers -------------------------------------------------

    def rng(self, seed: int, task_params: Optional[dict] = None) -> random.Random:
        """A Random instance derived from (env name, params, seed) only."""
        key = stable_hash(self.name, canonical_json(task_params or {}), str(int(seed)))
        return random.Random(int(key[:16], 16))

    def make_task_id(self, task_params: dict, seed: int, payload: dict) -> str:
        return stable_hash(
            self.name, canonical_json(task_params), str(int(seed)), canonical_json(payload)
        )[:24]

    @staticmethod
    def resolve_difficulty(task_params: Optional[dict]) -> float:
        d = 0.5
        if task_params and "difficulty" in task_params:
            d = float(task_params["difficulty"])
        return min(1.0, max(0.0, d))

    # -- required API ---------------------------------------------------------

    @abstractmethod
    def generate(self, task_params: Optional[dict] = None, seed: int = 0) -> Task:
        """Generate a task. Deterministic given (task_params, seed)."""

    @abstractmethod
    def verify(self, task: Task, response: str) -> VerifyResult:
        """Score a policy response. reward in [0,1], diagnostics JSON-safe."""

    @abstractmethod
    def reference_solution(self, task: Task) -> str:
        """A response that should earn reward >= self.reference_threshold."""

    @abstractmethod
    def corrupted_solution(self, task: Task) -> str:
        """A plausible-but-wrong response that should earn
        reward <= self.corrupted_threshold."""
