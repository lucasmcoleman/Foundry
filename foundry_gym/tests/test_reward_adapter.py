"""Guard the batch-to-reward contract without loading a training model."""

import pytest

from foundry_gym.training.reward_adapter import gym_reward


@pytest.mark.parametrize("tasks,completions", [([], ["answer"]), (["{}"], [])])
def test_misaligned_reward_batch_fails_explicitly(tasks, completions):
    with pytest.raises(ValueError, match="one task_json per completion"):
        gym_reward([], completions, task_json=tasks)


def test_bad_task_preserves_one_reward_per_completion():
    assert gym_reward(["prompt"], ["answer"], task_json=["invalid JSON"]) == [0.0]
