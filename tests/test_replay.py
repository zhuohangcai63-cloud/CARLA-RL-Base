"""Replay protocol regressions using deterministic state transitions, without CARLA."""
import copy
import math

import numpy as np
import pytest

from easycarla.extensions import PrefixCapacityError, PrefixRecorder, compare_states, replay_prefix


TOLERANCES = {"position": 0.05, "velocity": 0.01, "yaw": 0.01, "signals": 0,
              "controllers": 1e-6, "events": 0, "history": 1e-6, "task": 1e-6}


def raw_state(x=0, speed=0):
    return {"actors": {"ego": {"position": [x, 0, 0], "velocity": [speed, 0, 0],
                                "yaw": 0, "blueprint": "vehicle.fixture"}},
            "signals": {"light_a": "green"}, "controllers": {"ego": {"integral": 0}},
            "events": [], "history": [x], "task": {"progress": x}}


def test_no_tolerances_or_missing_hidden_state_never_claims_comparability():
    state = raw_state()
    assert compare_states(state, state)["status"] == "unverified"
    del state["controllers"]
    result = compare_states(state, state, TOLERANCES)
    assert result["status"] == "unverified"
    assert not result["comparable"]


def test_complete_matching_state_passes_and_yaw_wrap_uses_shortest_angle():
    a, b = raw_state(), raw_state()
    a["actors"]["ego"]["yaw"] = math.pi - 0.001
    b["actors"]["ego"]["yaw"] = -math.pi + 0.001
    result = compare_states(a, b, TOLERANCES)
    assert result["status"] == "passed"
    assert result["checks"]["yaw"]["max_error"] == pytest.approx(0.002)


@pytest.mark.parametrize("section", ["controllers", "events", "history", "task", "signals"])
def test_non_pose_state_differences_are_not_hidden(section):
    a, b = raw_state(), raw_state()
    b[section] = {"changed": True}
    result = compare_states(a, b, TOLERANCES)
    assert result["status"] == "failed"
    assert any(section in text for text in result["mismatches"])


def test_actor_identity_and_velocity_drift_fail():
    a, b = raw_state(), raw_state(speed=0.1)
    assert compare_states(a, b, TOLERANCES)["status"] == "failed"
    b = raw_state()
    b["actors"]["other"] = b["actors"].pop("ego")
    result = compare_states(a, b, TOLERANCES)
    assert result["checks"]["actor_set"]["missing"] == ["ego"]
    assert result["status"] == "failed"


def test_prefix_copies_shared_arrays_and_capacity_never_discards_commands():
    recorder = PrefixRecorder(max_commands=1)
    state = raw_state()
    state["history"] = np.array([1.0])
    recorder.start({"seed": 12}, state)
    recorder.append({"operation": "step", "action": np.array([0, 0.2, 0])}, state)
    state["history"][0] = 99
    with pytest.raises(PrefixCapacityError):
        recorder.append({"operation": "step"}, state)
    record = recorder.finish()
    assert record["initial_state"]["history"] == [1]
    assert record["steps"][0]["state"]["history"] == [1]
    assert len(record["steps"]) == 1
    with pytest.raises(PrefixCapacityError):
        PrefixRecorder(max_bytes=4).start({}, state)


def test_replay_reconstructs_and_executes_commands_and_reports_drift():
    world = {"state": raw_state()}
    def rebuild(recipe):
        world["state"] = raw_state(recipe["initial_x"])
        return world["state"]
    def execute(command):
        previous_x = world["state"]["actors"]["ego"]["position"][0]
        world["state"] = raw_state(previous_x + command["delta"])
        return world["state"]
    recipe = {"initial_x": 2, "seed": 42}
    rebuild(recipe)
    recorder = PrefixRecorder()
    recorder.start(recipe, world["state"])
    for delta in (1, 3, 2):
        command = {"delta": delta}
        recorder.append(command, execute(command))
    material = recorder.finish()
    report = replay_prefix(material, rebuild, execute, lambda: world["state"], TOLERANCES)
    assert report["commands_executed"] == 3
    assert report["status"] == "passed"
    assert report["result"]["actors"]["ego"]["position"][0] == 8
    def perturbed(command):
        return execute({"delta": command["delta"] + 0.1})
    report = replay_prefix(material, rebuild, perturbed, lambda: world["state"], TOLERANCES)
    assert report["status"] == "failed"
    assert report["final"]["checks"]["position"]["max_error"] == pytest.approx(0.3)


def test_corrupt_record_and_runtime_failures_do_not_produce_normal_result():
    recorder = PrefixRecorder()
    recorder.start({}, raw_state())
    recorder.append({"operation": "step"}, raw_state())
    material = recorder.finish()
    corrupt = copy.deepcopy(material)
    corrupt["steps"][0]["command"]["operation"] = "other"
    with pytest.raises(ValueError, match="checksum"):
        replay_prefix(corrupt, lambda _: None, lambda _: None, raw_state, TOLERANCES)
    def failed_step(_):
        raise RuntimeError("simulator disconnected")
    with pytest.raises(RuntimeError, match="disconnected"):
        replay_prefix(material, lambda _: None, failed_step, raw_state, TOLERANCES)


@pytest.mark.parametrize("bad", [{"position": -0.01}, {"position": float("inf")}, {"position": True}])
def test_generic_replay_validates_tolerances_before_rebuild(bad):
    recorder = PrefixRecorder()
    recorder.start({}, raw_state())
    called = []
    with pytest.raises(ValueError):
        replay_prefix(recorder.finish(), lambda recipe: called.append(recipe), lambda command: None, raw_state, bad)
    assert called == []


def test_generic_replay_validates_every_step_before_rebuild():
    import hashlib
    import json
    recorder = PrefixRecorder()
    recorder.start({}, raw_state())
    material = recorder.finish()
    material.pop("sha256")
    material["steps"] = [{"command": {}, "state": raw_state()}, {"command": {}, "state": "bad"}]
    material["sha256"] = hashlib.sha256(json.dumps(material, sort_keys=True, ensure_ascii=False,
                                                  separators=(",", ":")).encode()).hexdigest()
    called = []
    with pytest.raises(ValueError, match="step 1"):
        replay_prefix(material, lambda recipe: called.append(recipe), lambda command: None, raw_state, TOLERANCES)
    assert called == []


def test_drift_report_identifies_actor_and_numeric_field_without_relaxing_tolerance():
    a, b = raw_state(), raw_state(x=0.2)
    b["controllers"]["ego"]["integral"] = 0.05
    result = compare_states(a, b, TOLERANCES)
    assert result["status"] == "failed"
    assert result["checks"]["position"]["worst_actor"] == "ego"
    assert result["checks"]["controllers"]["largest_differences"] == [
        {"path": "controllers.ego.integral", "error": 0.05}]


def test_difference_paths_remain_bounded():
    a, b = raw_state(), raw_state()
    a["events"] = {str(index): 0 for index in range(100)}
    b["events"] = {str(index): index for index in range(100)}
    report = compare_states(a, b, TOLERANCES)
    fields = report["checks"]["events"]["largest_differences"]
    assert len(fields) == 5
    assert fields[0] == {"path": "events.99", "error": 99.0}
