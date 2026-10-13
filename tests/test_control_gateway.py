"""Control ownership and acknowledged actuator commits without CARLA bindings."""
from collections import Counter
from types import SimpleNamespace as NS

import numpy as np
import pytest

from easycarla.control.gateway import ControlGateway
from easycarla.errors import ControlConflict, EnvironmentFault
from easycarla.simulation.resources import ActorRegistry
from easycarla.simulation.session import WorldSession


class Actor:
    def __init__(self, actor_id, log, type_id="vehicle.test", alive=True):
        self.id, self.type_id, self.is_alive = actor_id, type_id, alive
        self.log = log
    def enable_constant_velocity(self, velocity): self.log.append(("constant_enable", self.id, velocity))
    def disable_constant_velocity(self): self.log.append(("constant_disable", self.id))
    def destroy(self): self.log.append(("destroy", self.id)); self.is_alive = False; return True


def vehicle_control(**kwargs):
    return NS(**{**dict(steer=0., throttle=0., brake=0., hand_brake=False, reverse=False), **kwargs})


def fixture():
    log = []
    command = NS(**{name: (lambda *args, name=name: (name, *args)) for name in (
        "SetAutopilot", "ApplyVehicleControl", "ApplyWalkerControl", "ApplyTargetVelocity", "ApplyTargetAngularVelocity")})
    session = WorldSession.__new__(WorldSession)
    session.carla = NS(VehicleControl=vehicle_control, WalkerControl=lambda **kw: NS(**kw),
                       Vector3D=lambda **kw: NS(**kw), command=command)
    session.sim = {"tm_port": 8500, "timeout": 1}
    session.client = NS(apply_batch_sync=lambda commands, tick: log.append(("batch", commands, tick)) or [NS(error="") for _ in commands])
    session.controls = ControlGateway(session)
    session.registry = ActorRegistry(on_forget=session.controls.forget)
    session.last_frame = 10
    session.ticks, session.timings = Counter(), Counter()
    frame = [10]
    def tick(timeout):
        log.append(("tick", timeout))
        frame[0] += 1
        return frame[0]
    session.world = NS(get_snapshot=lambda: NS(frame=frame[0]), tick=tick)
    actor = session.registry.register(Actor(1, log), "ego")
    return session, actor, log


def batches(log): return [item[1] for item in log if item[0] == "batch"]


def test_same_source_coalesces_into_one_commit_before_physics():
    session, actor, log = fixture()
    gateway = session.controls
    gateway.apply_control(actor, [0, .2, 0], "ego")
    gateway.apply_control(actor, [.1, .4, 0], "ego")
    assert not log
    frame = session.tick()
    assert frame.frame == 11 and session.ticks["step"] == 1
    assert [entry[0] for entry in log] == ["batch", "tick"]
    assert len(batches(log)[0]) == 1
    assert batches(log)[0][0][0] == "ApplyVehicleControl"
    assert batches(log)[0][0][2].throttle == pytest.approx(.4)
    assert all(entry[2] is False for entry in log if entry[0] == "batch")
    gateway.apply_control(actor, [0, 0, 1], "ego")
    session.tick()
    assert gateway.last_applied[1]["control"] == [0., 0., 1.]


def test_conflicting_sources_cannot_replace_pending_owner():
    session, actor, log = fixture()
    session.controls.apply_control(actor, [0, .2, 0], "first")
    with pytest.raises(ControlConflict): session.controls.apply_control(actor, [0, 0, 1], "second")
    assert session.controls.owner(1) == "first" and not log
    session.tick()
    assert batches(log)[0][0][2].throttle == pytest.approx(.2)


def test_fresh_spawn_missing_from_last_carla_snapshot_still_commits():
    session, actor, log = fixture()
    actor.is_alive = False
    session.controls.apply_control(actor, [0, .2, 0], "fresh_spawn")
    session.tick()
    assert [entry[0] for entry in log] == ["batch", "tick"]


def test_tm_to_manual_handoff_clears_old_control_and_constant_velocity():
    session, actor, log = fixture()
    gateway = session.controls
    gateway.set_autopilot(actor, True, "traffic", port=8500)
    with pytest.raises(ControlConflict): gateway.apply_control(actor, [0, .2, 0], "traffic")
    with pytest.raises(ControlConflict): gateway.handoff(actor, "research", expected_owner="incorrect")
    gateway.handoff(actor, "research", expected_owner="traffic", mode="manual")
    gateway.apply_control(actor, [0, 0, 1], "research")
    session.tick()
    commands = batches(log)[0]
    assert [cmd[0] for cmd in commands] == ["SetAutopilot", "ApplyVehicleControl"]
    assert commands[0] == ("SetAutopilot", 1, False, 8500)
    assert [entry[0] for entry in log] == ["batch", "constant_disable", "batch", "tick"]
    assert gateway.transitions[-1]["previous"] == "traffic"
    assert gateway.transitions[-1]["owner"] == "research"


def test_manual_to_autopilot_removes_all_prior_direct_actuators():
    session, actor, log = fixture()
    gateway = session.controls
    velocity = NS(x=3., y=0., z=0.)
    gateway.apply_control(actor, [0, .5, 0], "native")
    gateway.set_target_velocity(actor, velocity, "native")
    gateway.set_target_angular_velocity(actor, velocity, "native")
    gateway.enable_constant_velocity(actor, velocity, "native")
    gateway.set_autopilot(actor, True, "native")
    session.tick()
    assert [cmd[0] for cmd in batches(log)[0]] == ["SetAutopilot"]
    assert ("constant_disable", 1) in log
    assert all(entry[0] != "constant_enable" for entry in log)


def test_walker_uses_walker_command_and_invalid_control_is_rejected():
    session, _, log = fixture()
    actor = session.registry.register(Actor(2, log, "walker.pedestrian.test"), "walker")
    control = NS(direction=NS(x=1., y=0., z=0.), speed=1.5, jump=False)
    session.controls.apply_control(actor, control, "crossing")
    session.tick()
    assert batches(log)[0][0][0] == "ApplyWalkerControl"
    control.speed = float("nan")
    with pytest.raises(ValueError): session.controls.apply_control(actor, control, "crossing")
    with pytest.raises(ValueError): session.controls.set_autopilot(actor, True, "crossing")


@pytest.mark.parametrize("response", ["error", "missing"])
def test_control_batch_failure_never_advances_physics(response):
    session, actor, log = fixture()
    session.controls.apply_control(actor, [0, .5, 0], "ego")
    session.client.apply_batch_sync = lambda commands, tick: [NS(error="actor unavailable")] if response == "error" else []
    with pytest.raises(EnvironmentFault, match="Control batch failed before physics"):
        session.tick()
    assert not any(item[0] == "tick" for item in log)
    assert session.last_frame == 10 and not session.ticks


def test_constant_velocity_commit_and_ordering_barrier_precede_tick():
    session, actor, log = fixture()
    session.controls.enable_constant_velocity(actor, NS(x=2., y=0., z=0.), "native")
    session.tick()
    assert [entry[0] for entry in log] == ["constant_enable", "batch", "tick"]
    assert batches(log) == [[]]
    session.controls.disable_constant_velocity(actor, "native")
    session.tick()
    assert [entry[0] for entry in log][-3:] == ["constant_disable", "batch", "tick"]


def test_constant_velocity_failure_faults_before_tick():
    session, actor, log = fixture()
    def fail(_): raise RuntimeError("connection lost")
    actor.enable_constant_velocity = fail
    session.controls.enable_constant_velocity(actor, NS(x=2., y=0., z=0.), "native")
    with pytest.raises(EnvironmentFault, match="Constant-velocity commit failed before physics"):
        session.tick()
    assert not log and session.last_frame == 10


def test_registry_destroy_discards_pending_actor_commands_and_ownership():
    session, actor, log = fixture()
    session.controls.apply_control(actor, [0, .2, 0], "ego")
    assert session.registry.destroy([1]) == []
    assert session.controls.owner(1) is None
    session.tick()
    assert batches(log) == []
    assert [item[0] for item in log] == ["destroy", "tick"]


def test_unregistered_actor_cannot_commit_and_tm_port_is_fixed():
    session, actor, log = fixture()
    with pytest.raises(ControlConflict, match="Traffic Manager"):
        session.controls.set_autopilot(actor, True, "ego", port=9999)
    unowned = Actor(99, log)
    session.controls.apply_control(unowned, [0, .2, 0], "other")
    with pytest.raises(ControlConflict, match="Unowned actor"):
        session.tick()
    assert not log


def test_native_combined_controls_are_explicit_and_external_action_stays_strict():
    session, actor, log = fixture()
    with pytest.raises(ValueError): session.controls.apply_control(actor, [0, .2, .1], "ego")
    session.controls.apply_control(actor, [0, .2, .1], "native", allow_combined=True)
    session.tick()
    values = session.controls.last_applied[1]["control"]
    np.testing.assert_allclose(values, [0, .2, .1])


def test_staged_actuator_inputs_are_independent_copies():
    session, actor, log = fixture()
    control = vehicle_control(throttle=.3, hand_brake=True)
    velocity = NS(x=3., y=0., z=0.)
    session.controls.apply_control(actor, control, "native")
    session.controls.set_target_velocity(actor, velocity, "native")
    session.controls.enable_constant_velocity(actor, velocity, "native")
    control.throttle = 999.
    velocity.x = float("nan")
    session.tick()
    command = next(cmd for cmd in batches(log)[0] if cmd[0] == "ApplyVehicleControl")
    assert command[2].throttle == .3 and command[2].hand_brake
    target = next(cmd for cmd in batches(log)[0] if cmd[0] == "ApplyTargetVelocity")
    assert target[2].x == 3.
    constant = next(item for item in log if item[0] == "constant_enable")
    assert constant[2].x == 3.


def test_staged_walker_control_copies_direction_and_speed():
    session, _, log = fixture()
    actor = session.registry.register(Actor(2, log, "walker.pedestrian.test"), "walker")
    control = NS(direction=NS(x=1., y=0., z=0.), speed=1.5, jump=False)
    session.controls.apply_control(actor, control, "crossing")
    control.direction.x, control.speed = 99., -2.
    session.tick()
    committed = batches(log)[0][0][2]
    assert committed.direction.x == 1. and committed.speed == 1.5


def test_delegated_velocity_explicitly_disables_traffic_manager():
    session, actor, log = fixture()
    session.controls.claim(actor.id, "native", mode="delegated")
    session.controls.set_target_velocity(actor, NS(x=3., y=0., z=0.), "native")
    session.tick()
    commands = batches(log)[0]
    assert commands[0] == ("SetAutopilot", 1, False, 8500)
    assert commands[1][0] == "ApplyTargetVelocity"


def test_state_export_tracks_pending_values_with_semantic_roles():
    session, actor, _ = fixture()
    session.controls.apply_control(actor, [.1, .2, 0], "ego")
    before = session.controls.export_state({1: "ego"})
    session.controls.apply_control(actor, [0, 0, 1], "ego")
    after = session.controls.export_state({1: "ego"})
    assert before != after
    assert before["schema_version"] == 1
    import json
    json.dumps(after, allow_nan=False)
