"""Unit tests for foundry_gym.core.types: Task round-trip, canonical_json
determinism, VerifyResult reward clamping."""

from __future__ import annotations

import math

from foundry_gym.core.types import Task, VerifyResult, canonical_json


def _sample_task() -> Task:
    return Task(
        env="math_logic",
        task_id="abc123def456abc123def456",
        seed=7,
        difficulty=0.42,
        prompt="What is 2 + 2?" + _INSTRUCTION_SUFFIX,
        task_params={"difficulty": 0.42, "sub": "arithmetic"},
        payload={"answer": 4, "sub": "arithmetic"},
        metadata={"sub": "arithmetic", "note": "unit test fixture"},
    )


_INSTRUCTION_SUFFIX = "\n\nAnswer: <value>"


class TestTaskRoundTrip:
    def test_to_json_from_json_round_trip_all_fields(self):
        task = _sample_task()
        restored = Task.from_json(task.to_json())
        assert restored.env == task.env
        assert restored.task_id == task.task_id
        assert restored.seed == task.seed
        assert restored.difficulty == task.difficulty
        assert restored.prompt == task.prompt
        assert restored.task_params == task.task_params
        assert restored.payload == task.payload
        assert restored.metadata == task.metadata
        assert restored == task

    def test_round_trip_preserves_nested_structures(self):
        task = Task(
            env="code_repair",
            task_id="deadbeefdeadbeefdeadbeef",
            seed=0,
            difficulty=0.0,
            prompt="fix the bug",
            task_params={"nested": {"list": [1, 2, 3], "flag": True}},
            payload={"tests": [{"id": "t1", "expr": "m.f(1)"}], "expected": [1]},
            metadata={},
        )
        restored = Task.from_json(task.to_json())
        assert restored.task_params == task.task_params
        assert restored.payload == task.payload

    def test_from_json_defaults_missing_optional_dicts(self):
        # from_json tolerates missing/None task_params, payload, metadata.
        minimal = (
            '{"env":"x","task_id":"y","seed":1,"difficulty":0.5,"prompt":"p"}'
        )
        restored = Task.from_json(minimal)
        assert restored.task_params == {}
        assert restored.payload == {}
        assert restored.metadata == {}

    def test_seed_and_difficulty_are_coerced(self):
        s = '{"env":"x","task_id":"y","seed":"3","difficulty":"0.25","prompt":"p"}'
        restored = Task.from_json(s)
        assert restored.seed == 3
        assert isinstance(restored.seed, int)
        assert restored.difficulty == 0.25
        assert isinstance(restored.difficulty, float)


class TestCanonicalJsonDeterminism:
    def test_key_order_does_not_affect_output(self):
        a = canonical_json({"b": 1, "a": 2, "c": 3})
        b = canonical_json({"c": 3, "a": 2, "b": 1})
        assert a == b

    def test_no_whitespace_variance(self):
        s = canonical_json({"a": 1, "b": [1, 2, 3]})
        assert " " not in s
        assert s == '{"a":1,"b":[1,2,3]}'

    def test_nested_dict_key_order_normalized(self):
        a = canonical_json({"outer": {"z": 1, "y": 2}})
        b = canonical_json({"outer": {"y": 2, "z": 1}})
        assert a == b

    def test_task_to_json_is_deterministic_across_construction_order(self):
        # asdict() field order is fixed by the dataclass, but canonical_json
        # sorts keys regardless, so two structurally-identical tasks with
        # differently-ordered dict payloads still serialize identically.
        t1 = Task(
            env="e", task_id="t", seed=1, difficulty=0.1, prompt="p",
            task_params={"a": 1, "b": 2}, payload={}, metadata={},
        )
        t2 = Task(
            env="e", task_id="t", seed=1, difficulty=0.1, prompt="p",
            task_params={"b": 2, "a": 1}, payload={}, metadata={},
        )
        assert t1.to_json() == t2.to_json()


class TestVerifyResultClamping:
    def test_reward_above_one_clamped_to_one(self):
        vr = VerifyResult(1.5)
        assert vr.reward == 1.0

    def test_reward_below_zero_clamped_to_zero(self):
        vr = VerifyResult(-0.5)
        assert vr.reward == 0.0

    def test_nan_reward_clamped_to_zero(self):
        vr = VerifyResult(float("nan"))
        assert vr.reward == 0.0

    def test_reward_within_range_unchanged(self):
        vr = VerifyResult(0.73)
        assert vr.reward == 0.73

    def test_boundary_values_pass_through(self):
        assert VerifyResult(0.0).reward == 0.0
        assert VerifyResult(1.0).reward == 1.0

    def test_as_dict_shape(self):
        vr = VerifyResult(0.6, {"correct": True})
        d = vr.as_dict()
        assert d == {"reward": 0.6, "diagnostics": {"correct": True}}

    def test_infinity_clamped(self):
        assert VerifyResult(float("inf")).reward == 1.0
        assert VerifyResult(float("-inf")).reward == 0.0

    def test_int_reward_coerced_to_float(self):
        vr = VerifyResult(1)
        assert vr.reward == 1.0
        assert isinstance(vr.reward, float)
        assert not math.isnan(vr.reward)
