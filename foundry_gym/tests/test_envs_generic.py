"""Generic contract suite for every registered environment family.

Parametrized over ``foundry_gym.core.registry.names()`` at collection time,
so any new family dropped into foundry_gym/envs/ is covered automatically
with zero test edits. Do not add family-specific assertions here — put those
in test_<family>.py instead (see test_math_logic.py).
"""

from __future__ import annotations

import json
import re

import pytest

from foundry_gym.core import registry
from foundry_gym.core.types import Task

ENV_NAMES = registry.names()
SEEDS = (0, 7)
DIFFICULTIES = (0.2, 0.8)
SOUNDNESS_SEEDS = range(5)
SOUNDNESS_DIFFICULTY = 0.5

ROBUSTNESS_RESPONSES = [
    "",
    "garbage",
    "{}",
    "[]",
    "Answer:",
    "```json\nnull\n```",
    "a" * 100_000,
]

_TASK_ID_RE = re.compile(r"^[0-9a-f]{24}$")


@pytest.fixture(scope="module")
def task_cache():
    """Module-scoped memoizing generator so repeated (env, seed, difficulty)
    lookups across tests in this file don't regenerate tasks."""
    cache: dict = {}

    def _get(env_name: str, seed: int, difficulty: float) -> Task:
        key = (env_name, seed, difficulty)
        if key not in cache:
            env = registry.get(env_name)
            cache[key] = env.generate({"difficulty": difficulty}, seed=seed)
        return cache[key]

    return _get


@pytest.mark.parametrize("env_name", ENV_NAMES)
class TestDeterminism:
    @pytest.mark.parametrize("seed", SEEDS)
    @pytest.mark.parametrize("difficulty", DIFFICULTIES)
    def test_generate_is_deterministic(self, env_name, seed, difficulty):
        env = registry.get(env_name)
        t1 = env.generate({"difficulty": difficulty}, seed=seed)
        t2 = env.generate({"difficulty": difficulty}, seed=seed)
        assert t1.to_json() == t2.to_json()

    @pytest.mark.parametrize("difficulty", DIFFICULTIES)
    def test_different_seeds_yield_different_task_ids(self, env_name, difficulty):
        env = registry.get(env_name)
        t_a = env.generate({"difficulty": difficulty}, seed=0)
        t_b = env.generate({"difficulty": difficulty}, seed=1)
        assert t_a.task_id != t_b.task_id


@pytest.mark.parametrize("env_name", ENV_NAMES)
class TestJsonRoundTrip:
    def test_round_trip_reconstructs_identical_task(self, env_name, task_cache):
        task = task_cache(env_name, 0, 0.5)
        restored = Task.from_json(task.to_json())
        assert restored == task

    def test_round_trip_verifies_reference_identically(self, env_name, task_cache):
        env = registry.get(env_name)
        task = task_cache(env_name, 0, 0.5)
        restored = Task.from_json(task.to_json())
        ref_response = env.reference_solution(task)
        original_result = env.verify(task, ref_response)
        restored_result = env.verify(restored, ref_response)
        assert original_result.reward == restored_result.reward
        assert restored_result.reward >= env.reference_threshold


@pytest.mark.parametrize("env_name", ENV_NAMES)
class TestTaskShape:
    def test_task_params_and_payload_are_json_serializable(self, env_name, task_cache):
        task = task_cache(env_name, 0, 0.5)
        json.dumps(task.task_params)
        json.dumps(task.payload)

    def test_prompt_is_nonempty_string(self, env_name, task_cache):
        task = task_cache(env_name, 0, 0.5)
        assert isinstance(task.prompt, str)
        assert task.prompt.strip() != ""

    def test_difficulty_in_unit_interval(self, env_name, task_cache):
        task = task_cache(env_name, 0, 0.5)
        assert 0.0 <= task.difficulty <= 1.0

    def test_env_name_matches_registry_key(self, env_name, task_cache):
        task = task_cache(env_name, 0, 0.5)
        assert task.env == env_name

    def test_task_id_is_24_hex_chars(self, env_name, task_cache):
        task = task_cache(env_name, 0, 0.5)
        assert _TASK_ID_RE.match(task.task_id), task.task_id


@pytest.mark.parametrize("env_name", ENV_NAMES)
class TestSoundness:
    def test_reference_and_corrupted_thresholds(self, env_name, task_cache):
        env = registry.get(env_name)
        for seed in SOUNDNESS_SEEDS:
            task = task_cache(env_name, seed, SOUNDNESS_DIFFICULTY)
            ref_reward = env.verify(task, env.reference_solution(task)).reward
            cor_reward = env.verify(task, env.corrupted_solution(task)).reward
            assert 0.0 <= ref_reward <= 1.0
            assert 0.0 <= cor_reward <= 1.0
            assert ref_reward >= env.reference_threshold, (
                f"{env_name} seed={seed}: reference reward {ref_reward} "
                f"< threshold {env.reference_threshold}"
            )
            assert cor_reward <= env.corrupted_threshold, (
                f"{env_name} seed={seed}: corrupted reward {cor_reward} "
                f"> threshold {env.corrupted_threshold}"
            )
            assert ref_reward > cor_reward


@pytest.mark.parametrize("env_name", ENV_NAMES)
class TestRobustness:
    def test_verify_never_raises_and_earns_no_accidental_credit(self, env_name, task_cache):
        env = registry.get(env_name)
        task = task_cache(env_name, 0, 0.5)
        responses = ROBUSTNESS_RESPONSES + [task.prompt]
        for response in responses:
            result = env.verify(task, response)
            assert 0.0 <= result.reward <= 1.0
            assert result.reward < env.reference_threshold, (
                f"{env_name}: response {response[:40]!r} scored "
                f"{result.reward} >= reference_threshold"
            )
