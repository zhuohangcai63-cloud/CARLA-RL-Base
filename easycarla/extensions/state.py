"""Versioned diagnostics independent of the policy observation contract.

Only enumerated physical fields and adapter-owned export_state schemas are read.
This module never enumerates arbitrary Python instance attributes.
"""
from copy import deepcopy
from collections.abc import Mapping
from enum import Enum
import math
import random
import numpy as np
from ..control.npc import CONTROLLER_VERSION
from ..observations import actor_state
from ..observations.geometry import xyz
from ..observations.vehicle_geometry import can_query_vehicle_physics

def _plain_state(value, roles, limitations, path="state"):
    """Export known data types and expose unsupported hidden objects explicitly."""
    if isinstance(value, np.ndarray):
        return _plain_state(value.tolist(), roles, limitations, path)
    if isinstance(value, np.generic):
        return _plain_state(value.item(), roles, limitations, path)
    if isinstance(value, random.Random):
        return _plain_state(value.getstate(), roles, limitations, path + ".rng")
    if isinstance(value, Mapping):
        result = {}
        for key, item in value.items():
            key = str(key)
            if (key.endswith(("actor_id", "vehicle_id", "walker_id", "candidate_id"))
                    and isinstance(item, int)):
                result[key] = roles.get(item, f"unmapped_actor/{item}")
                if item not in roles:
                    limitations.add(f"{path}.{key}: actor identity no longer present in registry")
            elif key.endswith(("actor_ids", "walker_ids")) and isinstance(item, (list, tuple)):
                result[key] = [roles.get(i, f"unmapped_actor/{i}") for i in item]
            else:
                result[key] = _plain_state(item, roles, limitations, path + "." + key)
        return result
    if isinstance(value, (list, tuple, set)):
        values = sorted(value, key=str) if isinstance(value, set) else value
        return [_plain_state(item, roles, limitations, path + f"[{i}]") for i, item in enumerate(values)]
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"Non-finite experiment state at {path}")
        return value
    if isinstance(value, Enum):
        return value.name
    if hasattr(value, "type_id") and hasattr(value, "id"):
        if value.id not in roles:
            limitations.add(f"{path}: actor lacks a stable semantic identity")
        return {"actor": roles.get(value.id, f"unmapped_actor/{value.id}")}
    # CARLA waypoint/transform/vector/rotation types, exported without CARLA imports.
    if all(hasattr(value, key) for key in ("road_id", "lane_id", "s")):
        return {"road_id": int(value.road_id), "lane_id": int(value.lane_id),
                "s": float(value.s), "transform": _plain_state(value.transform, roles, limitations, path)}
    if hasattr(value, "location") and hasattr(value, "rotation"):
        return {"location": xyz(value.location).tolist(),
                "rotation": {"yaw": -math.radians(value.rotation.yaw),
                             "pitch": math.radians(value.rotation.pitch),
                             "roll": math.radians(value.rotation.roll)}}
    if all(hasattr(value, key) for key in ("x", "y", "z")):
        return xyz(value).tolist()
    if all(hasattr(value, key) for key in ("x", "y")):
        return [float(value.x), float(value.y)]
    if hasattr(value, "name") and isinstance(value.name, str):
        return value.name
    limitations.add(f"{path}: {type(value).__name__} state cannot be exported")
    return {"unavailable_type": type(value).__name__}


def _physics(actor, roles, limitations, physics_enabled=None):
    getter = getattr(actor, "get_physics_control", None)
    if not callable(getter):
        return None
    if actor.type_id.startswith("vehicle.") and not can_query_vehicle_physics(actor, physics_enabled):
        limitations.add(f"physics.{roles[actor.id]}: disabled, inactive or unknown vehicle physics; not queried")
        return None
    physics = getter()
    fields = ("mass", "drag_coefficient", "center_of_mass", "moi", "max_rpm", "use_gear_autobox",
              "damping_rate_full_throttle", "damping_rate_zero_throttle_clutch_engaged",
              "damping_rate_zero_throttle_clutch_disengaged", "gear_switch_time", "clutch_strength",
              "final_ratio", "use_sweep_wheel_collision", "torque_curve", "steering_curve")
    result = {key: getattr(physics, key) for key in fields if hasattr(physics, key)}
    result["wheels"] = [{key: getattr(wheel, key) for key in (
        "tire_friction", "damping_rate", "max_steer_angle", "radius", "max_brake_torque",
        "max_handbrake_torque", "lat_stiff_max_load", "lat_stiff_value", "long_stiff_value", "position")
        if hasattr(wheel, key)} for wheel in getattr(physics, "wheels", [])]
    result["forward_gears"] = [{key: getattr(gear, key) for key in ("ratio", "down_ratio", "up_ratio")
                                 if hasattr(gear, key)} for gear in getattr(physics, "forward_gears", [])]
    return _plain_state(result, roles, limitations, "physics")


def capture_initial_manifest(runtime):
    """Called once after reset; registration order is the actual spawn order."""
    if runtime.decision_steps or runtime.physical_ticks:
        raise ValueError("Initial manifests must be captured before the first decision")
    roles = runtime.session.registry.roles
    limitations, actors = set(), []
    for index, (actor_id, actor) in enumerate(runtime.session.registry.actors.items()):
        if not actor.is_alive:
            continue
        getter = getattr(actor, "get_transform", None)
        transform = _plain_state(getter(), roles, limitations, "initial_transform") if callable(getter) else None
        actors.append({"spawn_order": index, "role": roles[actor_id], "blueprint": actor.type_id,
                       "attributes": dict(getattr(actor, "attributes", {})), "transform": transform,
                       "physics": _physics(actor, roles, limitations,
                                           getattr(runtime.session.registry, "physics_enabled", {}).get(actor_id))})
    route = []
    for point in getattr(runtime.episode, "route", []):
        route.append(_plain_state(point, roles, limitations, "ego_route"))
    effective_settings = getattr(runtime.session, "effective_world_settings", {})
    if effective_settings is None:
        raise RuntimeError("Effective world settings must be captured before an episode manifest")
    manifest = {"schema_version": 1, "actors": actors,
                "effective_world_settings": deepcopy(effective_settings), "ego_route": route,
                "applied_map_override": deepcopy(getattr(runtime.session, "applied_map_override", {})),
                "controller_routes": {role: controller.tracker.route.tolist()
                                      for role, (_, controller) in getattr(runtime.episode, "controllers", {}).items()},
                "controller_version": CONTROLLER_VERSION,
                "source_identity": {key: runtime.episode.metadata.get(key) for key in
                                    ("source", "scenario_id", "source_sha256", "upstream_commit",
                                     "patch_version", "route_fingerprint")},
                "limitations": sorted(limitations)}
    return deepcopy(manifest)


def raw_state(runtime, identities):
    """Read full diagnostic state without ticking; normalize transient identities."""
    roles = dict(runtime.session.registry.roles)
    limitations = set()
    capabilities = runtime.episode.capabilities()
    prefix = capabilities.get("prefix_replay", False)
    if isinstance(prefix, dict):
        limitations.update(prefix.get("limitations", []))
    elif not prefix:
        limitations.update(capabilities.get("replay_restrictions", ["Scene has no verified replay implementation"]))
    world_actors = list(runtime.session.world.get_actors())
    # Preserve mappings for destroyed actors already present in event/history
    # records. This is diagnostic identity bookkeeping, not simulation state.
    identities["roles"].update(roles)
    roles = identities["roles"]
    sid_to_role = identities["semantic"]
    actors = {}
    for actor in world_actors:
        if not actor.is_alive or not actor.type_id.startswith(("vehicle.", "walker.pedestrian.", "static.")):
            continue
        sid = runtime.observer.semantic_id(actor)
        role = roles.get(actor.id)
        if role is None:
            role = f"unowned/{sid}"
            limitations.add("Unowned actor identity and controller state are not reconstructable")
        if role in actors:
            raise RuntimeError(f"Duplicate semantic role {role!r} prevents repeatability checks")
        sid_to_role[sid] = role
        frame_state = getattr(runtime.observer, "frame_state", None)
        actors[role] = (runtime.observer.get_state(actor)
                        if frame_state is not None and not actor.type_id.startswith("static.") else actor_state(actor))
    now = float(runtime.snapshot.timestamp.elapsed_seconds)
    origin = now - float(runtime.elapsed)
    history = []
    for timestamp, states in runtime.observer.history:
        normalized = {sid_to_role.get(sid, f"unmapped_history/{sid}"): deepcopy(value)
                      for sid, value in states.items()}
        history.append({"time": float(timestamp) - origin, "actors": normalized})
    signals, signal_ids = {}, {}
    for actor in world_actors:
        if not actor.is_alive or actor.type_id not in ("traffic.traffic_light", "traffic.stop"):
            continue
        location = xyz(actor.get_transform().location)
        identity = actor.type_id + "/" + "/".join(f"{v:.4f}" for v in location)
        signal_ids[actor.id] = identity
        if identity in signals:
            limitations.add("Traffic lights with duplicate locations need stronger semantic IDs")
        signal = {"state": str(actor.get_state()), "frozen": bool(actor.is_frozen())} if actor.type_id == "traffic.traffic_light" else {"state": "stop"}
        for field in ("elapsed_time", "green_time", "yellow_time", "red_time"):
            function = getattr(actor, "get_" + field, None)
            if callable(function):
                signal[field] = float(function())
        signals[identity] = signal
    controllers = {role: controller.state_dict() for role, (_, controller)
                   in getattr(runtime.episode, "controllers", {}).items()}
    controllers["ego_trajectory"] = runtime.tracker.state_dict()
    ownership = runtime.arbiter.state_dict()
    controllers["ownership"] = {"tick": ownership["tick"],
        "owners": {roles.get(actor_id, f"unmapped_actor/{actor_id}"): owner
                   for actor_id, owner in ownership["owners"].items()}}
    gateway = getattr(runtime.session, "controls", None)
    if gateway is not None:
        controllers["control_gateway"] = gateway.export_state(roles)
    else:
        controllers["control_gateway"] = {"schema_version": 1, "available": False}
        limitations.add("Control gateway state is unavailable")
    event_state = runtime.episode.export_state(roles)
    if not isinstance(event_state, dict) or type(event_state.get("schema_version")) is not int or event_state["schema_version"] != 1:
        raise ValueError("Scenario diagnostic state must declare schema_version=1")
    limitations.update(event_state.get("limitations", []))
    def signal_event(value):
        if not isinstance(value, str) or not value.startswith("signal:"):
            return value
        items = []
        for part in value.removeprefix("signal:").split(","):
            actor_id, road_id, lane_id = part.split(":")
            items.append(f"{signal_ids.get(int(actor_id), 'missing_signal')}:{road_id}:{lane_id}")
        return "signal:" + ",".join(sorted(items))
    task = {"previous_progress": runtime.task.previous_progress,
            "previous_acceleration": runtime.task.previous_acceleration,
            "events": sorted((signal_event(value) for value in runtime.task.events), key=str),
            "route_index": runtime.observer.route_index,
            "decision_steps": runtime.decision_steps, "physical_ticks": runtime.physical_ticks,
            "elapsed": runtime.elapsed, "runtime_state": runtime.state}
    rules = getattr(runtime, "traffic_rules", None)
    if rules is not None:
        def rule_key(key):
            signal_id, road_id, lane_id = key
            if signal_id not in signal_ids:
                limitations.add("TrafficRules references a missing traffic signal")
            return f"{signal_ids.get(signal_id, 'missing_signal')}:{road_id}:{lane_id}"
        task["traffic_rules"] = {"previous": {rule_key(key): value for key, value in rules.previous.items()},
                                 "stopped": sorted(rule_key(key) for key in rules.stopped)}
    result = {"schema_version": 1, "actors": actors, "signals": signals, "controllers": controllers,
              "events": event_state, "history": history, "task": task}
    result = _plain_state(result, roles, limitations)
    result["limitations"] = sorted(limitations)
    return result
