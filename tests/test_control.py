"""Pure control regressions; these do not claim CARLA integration coverage."""
import json
import math
from dataclasses import asdict

import numpy as np
import pytest

from easycarla.control import (ControlArbiter, ControlConflict, ReactiveNPCController,
                              TrajectoryTracker, indexed_uniform, legacy_action,
                              mechanism_preset, validate_action, validate_applied_control)


def vehicle(x=0, y=0, yaw=0, speed=8, identity="npc"):
    return {"id": identity, "position": [x, y, 0], "yaw": yaw, "speed": speed,
            "velocity": [speed * math.cos(yaw), speed * math.sin(yaw), 0],
            "dimensions": [4.5, 1.8, 1.5]}


def advance(state, action, dt=0.05):
    speed = max(0, state["speed"] + (3 * action[1] - 6 * action[2]) * dt)
    yaw = state["yaw"] - speed / 2.8 * math.tan(float(action[0]) * 0.7) * dt
    return vehicle(state["position"][0] + speed * math.cos(yaw) * dt,
                   state["position"][1] + speed * math.sin(yaw) * dt,
                   yaw, speed, state["id"])


@pytest.mark.parametrize("action", [[0, 1], [0, -0.1, 0], [1.1, 0, 0],
                                    [0, 0.1, 0.2], [float("nan"), 0, 0]])
def test_invalid_actions_are_rejected(action):
    with pytest.raises(ValueError):
        validate_action(action)


def test_explicit_legacy_action_adapter():
    np.testing.assert_array_equal(legacy_action([0.5, -0.8]), np.array([0.5, 0, 0.8], np.float32))


def test_combined_native_control_requires_explicit_opt_in_and_stays_bounded():
    native = [0, 0.5, 0.2]
    with pytest.raises(ValueError, match="both"):
        validate_action(native)
    np.testing.assert_array_equal(validate_applied_control(native), np.array(native, np.float32))
    for invalid in ([0, 1.1, 0.2], [0, 0.1, float("nan")], [2, 0.5, 0.2]):
        with pytest.raises(ValueError):
            validate_applied_control(invalid)
    arbiter = ControlArbiter()
    arbiter.claim("ego", "native")
    arbiter.begin_tick(0)
    with pytest.raises(ValueError, match="both"):
        arbiter.submit("ego", "native", native)
    arbiter.submit("ego", "native", native, allow_combined=True)
    with pytest.raises(ControlConflict):
        arbiter.submit("ego", "native", native, allow_combined=True)


def test_actor_can_have_only_one_submission_even_after_handoff():
    arbiter = ControlArbiter()
    arbiter.claim("npc", "native")
    with pytest.raises(ControlConflict):
        arbiter.claim("npc", "experiment")
    arbiter.begin_tick(1)
    arbiter.submit("npc", "native", [0, 0.5, 0])
    arbiter.release("npc", "native")
    arbiter.claim("npc", "experiment")
    with pytest.raises(ControlConflict):
        arbiter.submit("npc", "experiment", [0, 0, 1])
    arbiter.begin_tick(2)
    arbiter.submit("npc", "experiment", [0, 0, 1])
    with pytest.raises(ControlConflict):
        arbiter.begin_tick(2)


def test_trajectory_validation_reference_and_fixed_coordinate_frame():
    tracker = TrajectoryTracker()
    trajectory = {"reference_frame": 10, "times": [0, 1, 2],
                  "points": [[0, 0, 0, 5], [5, 0, 0, 5], [10, 1, 0.2, 5]]}
    with pytest.raises(ValueError, match="stale"):
        tracker.start(trajectory, 11, vehicle())
    with pytest.raises(ValueError, match="covered"):
        tracker.start(trajectory, 10, vehicle(), 3)
    tracker.start(trajectory, 10, vehicle(10, 20, math.pi / 2, speed=5), 0.2)
    np.testing.assert_allclose(tracker.points[1, :2], [10, 25])
    tracker.control(vehicle(10, 20, math.pi / 2, speed=5), 0.05)
    restored = TrajectoryTracker()
    restored.load_state_dict(json.loads(json.dumps(tracker.state_dict())))
    np.testing.assert_array_equal(tracker.control(vehicle(10, 20.25, math.pi / 2, speed=5), 0.05),
                                  restored.control(vehicle(10, 20.25, math.pi / 2, speed=5), 0.05))


@pytest.mark.parametrize("frame", [10.1, "10", True, -1])
def test_trajectory_frame_is_not_silently_coerced(frame):
    tracker = TrajectoryTracker()
    trajectory = {"reference_frame": frame, "times": [0, 1], "points": [[0, 0, 0, 1], [1, 0, 0, 1]]}
    with pytest.raises(ValueError, match="nonnegative integer"):
        tracker.start(trajectory, 10, vehicle())
    assert tracker.state_dict()["active"] is False


def test_trajectory_tracks_closed_loop_and_left_is_negative_carla_steer():
    tracker = TrajectoryTracker()
    times = np.arange(0, 4.01, 0.2)
    points = [[4 * t, t * 0.5, math.atan2(0.5, 4), 4] for t in times]
    tracker.start({"reference_frame": 0, "times": times, "points": points}, 0, vehicle(speed=0))
    state = vehicle(speed=0)
    first = tracker.control(state, 0.05)
    assert first[0] < 0
    state = advance(state, first)
    for _ in range(79):
        state = advance(state, tracker.control(state, 0.05))
    assert state["position"][0] > 10
    assert state["position"][1] > 1
    assert abs(state["position"][1] - state["position"][0] / 8) < 0.8
    with pytest.raises(RuntimeError, match="not active"):
        tracker.control(state, 0.05)


def test_presets_change_only_declared_axis_even_with_shared_randomization():
    for randomize in (False, True):
        baseline = asdict(mechanism_preset("C", 12, "npc", randomize))
        for name, field in [("A", "proactive_yield"), ("L", "reaction_delay"), ("G", "accepted_gap")]:
            alternative = asdict(mechanism_preset(name, 12, "npc", randomize))
            assert {key for key in baseline if baseline[key] != alternative[key]} == {field}


def test_indexed_noise_is_independent_of_call_order():
    before = indexed_uniform(42, "npc_a", 55, "jitter")
    for tick in range(100):
        indexed_uniform(42, "npc_b", tick, "jitter")
    assert indexed_uniform(42, "npc_a", 55, "jitter") == before
    assert indexed_uniform(42, "npc_a", 55, "other") != before


def controller(name="C"):
    return ReactiveNPCController("npc", [[0, 0], [100, 0]], mechanism=name)


def test_proactive_yield_axis_is_a_live_reaction_and_records_trigger():
    state = vehicle()
    other = vehicle(24, -24, math.pi / 2, identity="other")
    cooperative, assertive = controller("C"), controller("A")
    for tick in range(6):
        action_c = cooperative.control(state, [other], 0.05, tick)
        action_a = assertive.control(state, [other], 0.05, tick)
    assert action_c[2] > action_a[2]
    assert "proactive_yield" in cooperative.last_diagnostics["active"]
    assert "proactive_yield" not in assertive.last_diagnostics["active"]
    assert cooperative.trigger_counts["proactive_yield"] > 0
    for tick in range(6, 12):
        cooperative.control(state, [], 0.05, tick)
    assert cooperative.last_diagnostics["active"] == []


def test_delay_and_gap_axes_activate_without_disabling_emergency_floor():
    normal, delayed = controller("C"), controller("L")
    leader = vehicle(16, 0, speed=6, identity="leader")
    for tick in range(7):
        normal_action = normal.control(vehicle(), [leader], 0.05, tick)
        delayed_action = delayed.control(vehicle(), [leader], 0.05, tick)
    assert normal_action[2] > delayed_action[2]
    assert delayed.last_diagnostics["active"] == []
    for tick in range(7, 18):
        delayed.control(vehicle(), [leader], 0.05, tick)
    assert "following" in delayed.last_diagnostics["active"]
    cautious, small_gap = controller("C"), controller("G")
    own = vehicle(speed=3)
    crossing = vehicle(4, -12, math.pi / 2, speed=3, identity="cross")
    for tick in range(6):
        cautious_action = cautious.control(own, [crossing], 0.05, tick)
        small_action = small_gap.control(own, [crossing], 0.05, tick)
    assert cautious_action[2] > small_action[2]
    assert "gap_wait" in cautious.last_diagnostics["active"]
    assert "gap_wait" not in small_gap.last_diagnostics["active"]
    for mechanism in ("C", "A", "L", "G"):
        npc = controller(mechanism)
        action = npc.control(vehicle(), [vehicle(7, 0, speed=0, identity="blocked")], 0.05, 0)
        np.testing.assert_array_equal(action[1:], [0, 1])


def test_state_restore_and_switch_keep_history_and_low_level_state():
    original = controller("C")
    own = vehicle()
    leader = vehicle(16, 0, speed=6, identity="leader")
    for tick in range(7):
        original.control(own, [leader], 0.05, tick)
    material = json.loads(json.dumps(original.state_dict()))
    restored = controller("C")
    restored.load_state_dict(material)
    np.testing.assert_array_equal(original.control(own, [leader], 0.05, 7),
                                  restored.control(own, [leader], 0.05, 7))
    previous = restored.state_dict()
    restored.set_mechanism("L")
    switched = restored.state_dict()
    assert previous["history"] == switched["history"]
    assert previous["tracker"] == switched["tracker"]
    assert previous["time"] == switched["time"]
    assert switched["events"][-1]["type"] == "mechanism_switch"


def test_two_npcs_react_in_closed_loop_with_separate_states():
    follower = controller("C")
    leader = ReactiveNPCController("leader", [[0, 0], [100, 0]], "L", target_speed=6)
    follow_state, lead_state = vehicle(speed=8), vehicle(18, 0, speed=6, identity="leader")
    initial_gap = lead_state["position"][0] - follow_state["position"][0]
    for tick in range(80):
        follow_action = follower.control(follow_state, [lead_state], 0.05, tick)
        lead_action = leader.control(lead_state, [follow_state], 0.05, tick)
        follow_state, lead_state = advance(follow_state, follow_action), advance(lead_state, lead_action)
    assert follow_state["speed"] < 7
    assert 5 < lead_state["position"][0] - follow_state["position"][0] < initial_gap
    assert follower.trigger_counts["following"] > 0
    assert leader.trigger_counts["following"] == 0


def test_signal_constraint_bypasses_delayed_mechanism():
    state = dict(vehicle(), signal_red=True, stop_distance=1)
    action = controller("L").control(state, [], 0.05, 0)
    assert action[2] > 0 and action[1] == 0
