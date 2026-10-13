import numpy as np
import pytest

from easycarla.config import validate_config
from easycarla.tasks import DrivingTask


def observation():
    return {"route": {"progress": 0.}, "ego": {
        "acceleration": np.zeros(3), "speed": 0., "speed_limit": 10.}}


def task(config):
    result = DrivingTask(validate_config(config)["task"])
    result.reset(observation())
    return result


@pytest.mark.parametrize("facts,elapsed,reason", [
    ({"collision": True}, .1, "collision"), ({"offroad": True}, .1, "offroad"),
    ({"goal": True}, .1, "goal"), ({}, 301, "timeout")])
def test_default_termination_is_unchanged(facts, elapsed, reason):
    _, terminated, truncated, info = task({}).evaluate(observation(), facts, .05, elapsed)
    assert (terminated, truncated) == (reason != "timeout", reason == "timeout")
    assert info["termination_reason"] == reason


def test_collision_only_keeps_events_and_latches_goal_without_timeout():
    config = {"task": {"termination": {"offroad": False, "goal": False, "timeout": False},
                       "weights": {"goal": 0}}}
    candidate = task(config)
    _, term, trunc, info = candidate.evaluate(observation(), {"offroad": True, "goal": True}, .05, 10000)
    assert not term and not trunc and info["termination_reason"] == ""
    assert info["cost_components"]["offroad"] == 1
    assert info["goal_reached"] and info["reward_components"]["goal"] == 0
    _, term, trunc, info = candidate.evaluate(observation(), {}, .05, 10001)
    assert not term and not trunc and info["goal_reached"]
    _, term, trunc, info = candidate.evaluate(observation(), {"collision": True, "offroad": True}, .05, 10002)
    assert term and not trunc and info["termination_reason"] == "collision"
    candidate.reset(observation())
    assert not candidate.evaluate(observation(), {}, .05, .05)[3]["goal_reached"]


@pytest.mark.parametrize("value", [0, 1, None, "false", []])
def test_termination_flags_require_boolean(value):
    with pytest.raises(ValueError, match="termination"):
        validate_config({"task": {"termination": {"offroad": value}}})


def test_unknown_rule_and_native_override_rejected():
    with pytest.raises(ValueError, match="termination"):
        validate_config({"task": {"termination": {"signal": False}}})
    with pytest.raises(ValueError, match="driving only"):
        validate_config({"task": {"profile": "bench2drive_native", "termination": {"offroad": False}}})


def test_rewards_and_costs_do_not_depend_on_termination_switches():
    original, configured = task({}), task({"task": {"termination": {"offroad": False, "goal": False}}})
    for facts in [{"offroad": True, "goal": True}, {}, {"offroad": True}]:
        old = original.evaluate(observation(), facts, .05, .1)
        new = configured.evaluate(observation(), facts, .05, .1)
        assert old[0] == new[0]
        assert old[3]["reward_components"] == new[3]["reward_components"]
        assert old[3]["cost_components"] == new[3]["cost_components"]
