"""Control ownership gate used only by the pinned upstream compatibility copy.

The preparation script redirects upstream actor control calls here. Actual
research controllers call actors normally outside upstream code. Actor destruction
and lifetime are deliberately never intercepted or extended.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar

_active = ContextVar("bench2drive_control", default=None)


@contextmanager
def control_scope(state):
    token = _active.set(state)
    try:
        yield
    finally:
        _active.reset(token)


def native_actor_call(actor, method, *args, **kwargs):
    state = _active.get()
    if state is not None:
        if method == "set_simulate_physics":
            # Lifecycle changes remain native-authoritative, including leased
            # actors. Witness them without altering their call or tick order.
            enabled = args[0] if args else kwargs.get("enabled", True)
            result = actor.set_simulate_physics(*args, **kwargs)
            state["controls"].session.registry.physics_enabled[actor.id] = bool(enabled)
            return result
        if method == "get_world":
            return state["world"]
        if method == "destroy":
            result = actor.destroy(*args, **kwargs)
            if result:
                state["world"].forget(actor.id)
            return result
        if actor.id in state["leased"]:
            state["suppressed"][actor.id] = state["suppressed"].get(actor.id, 0) + 1
            return None
        if actor.id == state.get("ego_id") and state.get("capture_ego"):
            if method != "apply_control":
                raise RuntimeError(f"Unsupported native ego control mutation: {method}")
            state["ego_control"] = args[0] if args else kwargs["control"]
            return None
        gateway = state.get("controls")
        if gateway is not None:
            owner = state["owner"]
            if method == "apply_control":
                return gateway.apply_control(actor, args[0] if args else kwargs["control"],
                                             owner=owner, allow_combined=True)
            if method == "set_autopilot":
                enabled = args[0] if args else kwargs.get("enabled", True)
                port = args[1] if len(args) > 1 else kwargs.get("tm_port")
                result = gateway.set_autopilot(actor, enabled, owner=owner, port=port)
                autopilot = state.setdefault("autopilot", set())
                autopilot.add(actor.id) if enabled else autopilot.discard(actor.id)
                if not enabled:
                    state.get("resume_autopilot", {}).pop(actor.id, None)
                return result
            if method == "apply_native_velocity_impulse":
                # This named pinned-upstream operation clears crossing space
                # for exactly one tick, including existing native TM actors.
                # Generic direct writes to an existing TM actor still reject.
                gateway.handoff(actor, owner, expected_owner=owner)
                state.setdefault("autopilot", set()).discard(actor.id)
                state.setdefault("resume_autopilot", {})[actor.id] = actor
                state["velocity_impulse_handoffs"] = state.get("velocity_impulse_handoffs", 0) + 1
                return gateway.set_target_velocity(actor, args[0], owner=owner)
            if method == "apply_native_transform_reset":
                # The pinned ActorTransformSetter clears motion before a
                # teleport. Temporarily lease existing TM actors for this
                # physical tick, restoring only actors that actually used TM.
                if actor.id in state.get("autopilot", ()):
                    gateway.handoff(actor, owner, expected_owner=owner)
                    state["autopilot"].discard(actor.id)
                    state.setdefault("resume_autopilot", {})[actor.id] = actor
                    state["transform_reset_handoffs"] = state.get("transform_reset_handoffs", 0) + 1
                return gateway.set_target_velocity(actor, args[0], owner=owner)
            if method in ("set_target_velocity", "set_target_angular_velocity"):
                value = args[0] if args else next(iter(kwargs.values()))
                if actor.id in state.get("autopilot", ()):
                    snapshot = state["world"].get_snapshot()
                    if snapshot.find(actor.id) is None:
                        # Native background spawning seeds a new TM vehicle's
                        # speed. Own this first tick manually, then restore TM
                        # next tick; never let two actuator writers race.
                        gateway.handoff(actor, owner, expected_owner=owner)
                        state["autopilot"].discard(actor.id)
                        state.setdefault("resume_autopilot", {})[actor.id] = actor
                        state["spawn_velocity_handoffs"] = state.get("spawn_velocity_handoffs", 0) + 1
                return getattr(gateway, method)(actor, value, owner=owner)
            if method == "enable_constant_velocity":
                if actor.id in state.get("autopilot", ()) and state["world"].get_snapshot().find(actor.id) is None:
                    # Native actor flows switch their newly spawned TM car to
                    # a continuous velocity controller. This is a permanent
                    # source handoff until an explicit native TM re-enable.
                    gateway.handoff(actor, owner, expected_owner=owner)
                    state["autopilot"].discard(actor.id)
                    state["constant_velocity_handoffs"] = state.get("constant_velocity_handoffs", 0) + 1
                state.get("resume_autopilot", {}).pop(actor.id, None)
                return gateway.enable_constant_velocity(actor, args[0] if args else kwargs["velocity"], owner=owner)
            if method == "disable_constant_velocity":
                return gateway.disable_constant_velocity(actor, owner=owner)
    return getattr(actor, method)(*args, **kwargs)


def mark_spawn_physics(spawn_command, physics_command, enabled):
    """Witness an opaque native SpawnActor.then(SetSimulatePhysics) chain."""
    state = _active.get()
    if state is not None:
        state.setdefault("spawn_physics", {})[id(spawn_command)] = bool(enabled)
    return physics_command
