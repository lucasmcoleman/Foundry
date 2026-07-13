"""Family-specific tests for foundry_gym.envs.math_logic (MathLogicEnv)."""

from __future__ import annotations

import pytest

from foundry_gym.core import registry

SUBFAMILIES = ["arithmetic", "modpow", "linear_system", "logic_count", "sequence"]


@pytest.fixture(scope="module")
def env():
    return registry.get("math_logic")


class TestSubfamilies:
    @pytest.mark.parametrize("sub", SUBFAMILIES)
    def test_each_subfamily_generates_and_reference_scores_full(self, env, sub):
        task = env.generate({"sub": sub, "difficulty": 0.5}, seed=0)
        assert task.metadata["sub"] == sub
        assert task.payload["sub"] == sub
        result = env.verify(task, env.reference_solution(task))
        assert result.reward == 1.0


class TestAnswerExtraction:
    def test_multiple_answer_lines_last_wins(self, env):
        task = env.generate({"sub": "arithmetic", "difficulty": 0.3}, seed=1)
        correct = task.payload["answer"]
        wrong = correct + 1000
        response = f"first thought\nAnswer: {wrong}\nsecond thought\nAnswer: {correct}\n"
        result = env.verify(task, response)
        assert result.reward == 1.0
        assert result.diagnostics["correct"] is True

    def test_ambiguous_multi_value_answer_is_unparseable(self, env):
        task = env.generate({"sub": "arithmetic", "difficulty": 0.3}, seed=1)
        result = env.verify(task, "Answer: 3 or 5")
        assert result.reward == 0.0

    def test_no_marker_scores_zero(self, env):
        task = env.generate({"sub": "arithmetic", "difficulty": 0.3}, seed=1)
        result = env.verify(task, "just rambling with no final marker")
        assert result.reward == 0.0

    def test_non_integer_answer_scores_partial_credit(self, env):
        task = env.generate({"sub": "arithmetic", "difficulty": 0.3}, seed=1)
        result = env.verify(task, "Answer: 3.5")
        assert result.reward == 0.1
        assert result.diagnostics["correct"] is False

    def test_correct_answer_scores_full_credit(self, env):
        task = env.generate({"sub": "modpow", "difficulty": 0.5}, seed=2)
        correct = task.payload["answer"]
        result = env.verify(task, f"Working through it.\n\nAnswer: {correct}")
        assert result.reward == 1.0

    def test_incorrect_well_formed_answer_scores_shaping_credit(self, env):
        task = env.generate({"sub": "sequence", "difficulty": 0.4}, seed=3)
        wrong = int(task.payload["answer"]) + 12345
        result = env.verify(task, f"Answer: {wrong}")
        assert result.reward == 0.1
        assert result.diagnostics["correct"] is False
