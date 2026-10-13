"""Owned actuator writes, acknowledged before the world's next physical tick.

All callers stage commands. Repeated setters by the same source are coalesced;
changing sources requires an explicit handoff. No method here advances physics.
"""
from collections import deque

import numpy as np

from easycarla.errors import EnvironmentFault
from .actions import validate_action, validate_applied_control
from .ownership import ControlArbiter, ControlConflict


class ControlGateway:
    MODES = frozenset({"manual", "autopilot", "delegated"})

    def __init__(self, session):
        self.session = session
        self.arbiter = ControlArbiter()
        self._modes = {}
        self._actors = {}
        self._pending = {}
        self._tick = 0
        self.last_applied = {}
        self.transitions = deque(maxlen=256)
        self.arbiter.begin_tick(0)

    def owner(self, actor_id):
        return self.arbiter.owner(actor_id)

    def claim(self, actor_id, owner, mode="manual"):
        if mode not in self.MODES:
            raise ValueError(f"Unknown control mode {mode!r}")
        self.arbiter.claim(actor_id, owner)
        self._modes.setdefault(actor_id, mode)

    def _owned(self, actor, owner):
        self.claim(actor.id, owner)
        self._actors[actor.id] = actor
        return self._pending.setdefault(actor.id, {})

    def handoff(self, actor, new_owner, expected_owner=None, mode="manual"):
        if mode not in self.MODES:
            raise ValueError(f"Unknown control mode {mode!r}")
        previous = self.owner(actor.id)
        if expected_owner is not None and previous != expected_owner:
            raise ControlConflict(f"Actor {actor.id} owner is {previous!r}, expected {expected_owner!r}")
        if previous is not None:
            self.arbiter.release(actor.id, previous)
        self.arbiter.claim(actor.id, new_owner)
        self._actors[actor.id] = actor
        self._pending.pop(actor.id, None)
        self._modes[actor.id] = mode
        if actor.type_id.startswith("vehicle."):
            self._pending[actor.id] = {"set_autopilot": (mode == "autopilot", self.session.sim["tm_port"]),
                                       "constant_velocity": None}
        self.transitions.append({"tick": self._tick, "actor_id": actor.id,
                                 "previous": previous, "owner": new_owner, "mode": mode})

    def apply_control(self, actor, control, owner, *, allow_combined=False):
        if actor.type_id.startswith("vehicle."):
            values = ([control.steer, control.throttle, control.brake]
                      if hasattr(control, "steer") else control)
            values = (validate_applied_control if allow_combined else validate_action)(values)
            if not hasattr(control, "steer"):
                control = self.session.carla.VehicleControl(steer=float(values[0]), throttle=float(values[1]), brake=float(values[2]))
            else:
                fields = ("steer", "throttle", "brake", "hand_brake", "reverse", "manual_gear_shift", "gear")
                control = self.session.carla.VehicleControl(**{key: getattr(control, key) for key in fields if hasattr(control, key)})
        elif actor.type_id.startswith("walker.pedestrian."):
            values = np.array([control.direction.x, control.direction.y, control.direction.z, control.speed], float)
            if not np.isfinite(values).all() or control.speed < 0:
                raise ValueError("Walker control must have finite direction and nonnegative speed")
            control = self.session.carla.WalkerControl(direction=self._vector(control.direction),
                                                       speed=float(control.speed), jump=bool(control.jump))
        else:
            raise ControlConflict(f"Actor {actor.type_id} does not accept driving control")
        pending = self._owned(actor, owner)
        if self._modes[actor.id] == "autopilot":
            raise ControlConflict("Disable autopilot explicitly before submitting manual control")
        if self._modes[actor.id] == "delegated" and actor.type_id.startswith("vehicle."):
            pending["set_autopilot"] = (False, self.session.sim["tm_port"])
        self._modes[actor.id] = "manual"
        pending["apply_control"] = control
        return values[:3].astype(np.float32) if actor.type_id.startswith("vehicle.") else values

    def set_autopilot(self, actor, enabled, owner, port=None):
        if not isinstance(enabled, bool) or not actor.type_id.startswith("vehicle."):
            raise ValueError("Autopilot requires a vehicle and a boolean state")
        if port is not None and int(port) != self.session.sim["tm_port"]:
            raise ControlConflict("Actor must use the instance's synchronized Traffic Manager")
        pending = self._owned(actor, owner)
        if enabled:
            for method in ("apply_control", "set_target_velocity", "set_target_angular_velocity"):
                pending.pop(method, None)
            pending["constant_velocity"] = None
        pending["set_autopilot"] = (enabled, self.session.sim["tm_port"])
        self._modes[actor.id] = "autopilot" if enabled else "manual"

    def _velocity(self, actor, velocity, owner, method):
        if not np.isfinite([velocity.x, velocity.y, velocity.z]).all():
            raise ValueError("Target velocity must be finite")
        pending = self._owned(actor, owner)
        if self._modes[actor.id] == "autopilot":
            raise ControlConflict("Disable autopilot explicitly before assigning target velocity")
        if self._modes[actor.id] == "delegated" and actor.type_id.startswith("vehicle."):
            pending["set_autopilot"] = (False, self.session.sim["tm_port"])
        self._modes[actor.id] = "manual"
        pending[method] = self._vector(velocity)

    def _vector(self, value):
        return self.session.carla.Vector3D(x=float(value.x), y=float(value.y), z=float(value.z))

    def set_target_velocity(self, actor, velocity, owner):
        self._velocity(actor, velocity, owner, "set_target_velocity")

    def set_target_angular_velocity(self, actor, velocity, owner):
        self._velocity(actor, velocity, owner, "set_target_angular_velocity")

    def enable_constant_velocity(self, actor, velocity, owner):
        if not actor.type_id.startswith("vehicle.") or not np.isfinite([velocity.x, velocity.y, velocity.z]).all():
            raise ValueError("Constant velocity requires a vehicle and a finite local velocity")
        pending = self._owned(actor, owner)
        if self._modes[actor.id] == "autopilot":
            raise ControlConflict("Disable autopilot explicitly before enabling constant velocity")
        if self._modes[actor.id] == "delegated":
            pending["set_autopilot"] = (False, self.session.sim["tm_port"])
        self._modes[actor.id] = "manual"
        pending["constant_velocity"] = self._vector(velocity)

    def disable_constant_velocity(self, actor, owner):
        if not actor.type_id.startswith("vehicle."):
            raise ValueError("Constant velocity is a vehicle actuator")
        self._owned(actor, owner)["constant_velocity"] = None

    def flush(self):
        """Publish one acknowledged batch. A partial batch failure faults the tick."""
        commands, trace = [], {}
        carla_commands = self.session.carla.command
        for actor_id, pending in self._pending.items():
            actor = self._actors[actor_id]
            if actor_id not in self.session.registry.actors:
                raise ControlConflict(f"Unowned actor {actor_id} received a queued control")
            # Newly spawned actors may not be in CARLA's latest snapshot yet;
            # the acknowledged batch response is authoritative for existence.
            summary = {"owner": self.owner(actor_id), "mode": self._modes[actor_id], "methods": []}
            # Delegation always changes before direct actuator writes in this batch.
            for method in ("set_autopilot", "apply_control", "set_target_velocity", "set_target_angular_velocity"):
                if method not in pending:
                    continue
                value = pending[method]
                if method == "set_autopilot":
                    command = carla_commands.SetAutopilot(actor_id, *value)
                elif method == "apply_control":
                    constructor = carla_commands.ApplyVehicleControl if actor.type_id.startswith("vehicle.") else carla_commands.ApplyWalkerControl
                    command = constructor(actor_id, value)
                    if actor.type_id.startswith("vehicle."):
                        self.arbiter.submit(actor_id, self.owner(actor_id), [value.steer, value.throttle, value.brake], allow_combined=True)
                        summary["control"] = [float(value.steer), float(value.throttle), float(value.brake)]
                else:
                    constructor = carla_commands.ApplyTargetVelocity if method == "set_target_velocity" else carla_commands.ApplyTargetAngularVelocity
                    command = constructor(actor_id, value)
                commands.append(command)
                summary["methods"].append(method)
            trace[actor_id] = summary
        if commands:
            responses = self.session.client.apply_batch_sync(commands, False)
            if len(responses) != len(commands) or any(response.error for response in responses):
                errors = [str(response.error) for response in responses if response.error]
                raise EnvironmentFault(f"Control batch failed before physics (partial application possible): {errors}")
        # CARLA exposes no command object for constant-velocity toggles. These
        # setters enqueue RPCs; a following synchronous RPC is the ordering
        # barrier before WorldSession sends the physics tick. Never describe the
        # individual setters themselves as acknowledged operations.
        constant_updates = [(actor_id, p["constant_velocity"]) for actor_id, p in self._pending.items()
                            if "constant_velocity" in p]
        if constant_updates:
            try:
                for actor_id, value in constant_updates:
                    actor = self._actors[actor_id]
                    if value is None:
                        actor.disable_constant_velocity()
                    else:
                        actor.enable_constant_velocity(value)
                    trace[actor_id]["methods"].append("disable_constant_velocity" if value is None else "enable_constant_velocity")
                responses = self.session.client.apply_batch_sync([], False)
                if responses:
                    raise RuntimeError("Unexpected results from actuator ordering barrier")
            except Exception as exc:
                raise EnvironmentFault(f"Constant-velocity commit failed before physics: {exc}") from exc
        self.last_applied = trace
        self._pending.clear()

    def after_tick(self):
        self._tick += 1
        self.arbiter.begin_tick(self._tick)

    def forget(self, actor_id):
        owner = self.owner(actor_id)
        if owner is not None:
            self.arbiter.release(actor_id, owner)
        self._actors.pop(actor_id, None)
        self._modes.pop(actor_id, None)
        self._pending.pop(actor_id, None)

    def clear(self):
        self._actors.clear()
        self._modes.clear()
        self._pending.clear()
        self.last_applied = {}
        self.transitions.clear()
        self.arbiter.clear()
        self._tick = 0
        self.arbiter.begin_tick(0)

    def export_state(self, actor_roles):
        pending = {}
        for actor_id, commands in self._pending.items():
            values = {}
            for method, value in commands.items():
                if method == "apply_control":
                    values[method] = ({key: getattr(value, key) for key in
                                       ("steer", "throttle", "brake", "hand_brake", "reverse", "manual_gear_shift", "gear")
                                       if hasattr(value, key)} if hasattr(value, "steer") else
                                      {"direction": [value.direction.x, value.direction.y, value.direction.z],
                                       "speed": value.speed, "jump": value.jump})
                elif value is not None and hasattr(value, "x"):
                    values[method] = [value.x, value.y, value.z]
                else:
                    values[method] = value
            pending[actor_roles.get(actor_id, f"unmapped_actor/{actor_id}")] = values
        return {"schema_version": 1, "owners": {
            actor_roles[actor_id]: {"source": owner, "mode": self._modes[actor_id]}
            for actor_id, owner in self.arbiter.state_dict()["owners"].items()
            if actor_id in actor_roles}, "pending": pending}
