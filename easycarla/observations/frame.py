"""Immutable physical-frame inputs shared by observation, control and task code."""
from __future__ import annotations
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
import math
import numpy as np

from .geometry import xyz, pose


class FrozenMapping(Mapping):
    """Read-only recursively frozen mappings; safe to retain in bounded history."""
    __slots__ = ("_data",)

    def __init__(self, values):
        object.__setattr__(self, "_data", MappingProxyType({key: freeze(value) for key, value in values.items()}))

    def __setattr__(self, key, value): raise TypeError("Captured frame state is immutable")
    def __getitem__(self, key): return self._data[key]
    def __iter__(self): return iter(self._data)
    def __len__(self): return len(self._data)
    def __deepcopy__(self, memo): return self

    def with_updates(self, **updates):
        """Reuse already-frozen geometry without recursively copying it."""
        result = object.__new__(type(self))
        values = dict(self._data)
        values.update({key: freeze(value) for key, value in updates.items()})
        object.__setattr__(result, "_data", MappingProxyType(values))
        return result


def freeze(value):
    if isinstance(value, FrozenMapping):
        return value
    if isinstance(value, Mapping):
        return FrozenMapping(value)
    if isinstance(value, (list, tuple)):
        return tuple(freeze(item) for item in value)
    if isinstance(value, np.ndarray):
        return tuple(freeze(item) for item in value.tolist())
    return value


def actor_state(actor, snapshot=None):
    """Read kinematics from a supplied ActorSnapshot; actuator telemetry once.

    The standalone helper remains useful outside a running kernel. Runtime
    consumers should use ObservationBuilder.get_state to share a committed frame.
    """
    physical = actor if snapshot is None else snapshot
    position, yaw = pose(physical.get_transform())
    velocity, acceleration = xyz(physical.get_velocity()), xyz(physical.get_acceleration())
    vehicle = actor.type_id.startswith("vehicle.")
    control = actor.get_control() if vehicle else None
    return {"position": position.tolist(), "yaw": yaw, "velocity": velocity.tolist(),
            "acceleration": acceleration.tolist(), "speed": float(np.linalg.norm(velocity)),
            "yaw_rate": -math.radians(physical.get_angular_velocity().z),
            "size": (2 * np.abs(xyz(actor.bounding_box.extent))).tolist(),
            "control": [control.steer, control.throttle, control.brake] if control is not None else [0, 0, 0],
            "type": actor.type_id}


@dataclass(frozen=True)
class FrameState:
    frame: int
    timestamp: float
    actors: FrozenMapping
    signals: tuple
    obstacles: tuple


class FrameStateError(RuntimeError):
    """A consumer requested a missing or uncommitted physical frame."""


def capture_frame(session, snapshot, geometry_cache=None, signal_cache=None, ego_id=None):
    """Enumerate the world once and read each actor/signal once for this frame."""
    states, signals, obstacles = {}, [], []
    find = getattr(snapshot, "find", None)
    for actor in session.world.get_actors():
        kind = actor.type_id
        if kind.startswith(("vehicle.", "walker.pedestrian.")):
            if not actor.is_alive:
                continue
            physical = find(actor.id) if callable(find) else None
            if callable(find) and physical is None:
                # Actors spawned since the snapshot do not yet belong to this frame.
                continue
            state = actor_state(actor, physical)
            if geometry_cache is not None:
                from .vehicle_geometry import geometry, enrich
                physics_states = getattr(session.registry, "physics_enabled", None)
                physics_enabled = physics_states.get(actor.id, False) if physics_states is not None else True
                cached_geometry = geometry_cache.get(actor.id)
                if cached_geometry is None or (cached_geometry.get("physics_pending")
                        and physics_enabled and getattr(actor, "is_active", True)):
                    geometry_cache[actor.id] = geometry(actor, getattr(session, "world_origin", None), physics_enabled)
                state = enrich(actor, physical or actor, state, geometry_cache[actor.id],
                               getattr(session, "carla", None) if actor.id == ego_id else None)
            if kind.startswith("vehicle."):
                state["speed_limit"] = float(actor.get_speed_limit()) / 3.6
                light_query = getattr(actor, "is_at_traffic_light", None)
                state["signal_red"] = (bool(light_query()) and str(actor.get_traffic_light_state()) == "Red") if callable(light_query) else False
            states[actor.id] = state
        elif kind in ("traffic.traffic_light", "traffic.stop"):
            cached_signal = signal_cache.get(actor.id) if signal_cache is not None else None
            if cached_signal is None:
                transform = actor.get_transform()
                trigger = transform.transform(actor.trigger_volume.location)
                waypoints = (actor.get_stop_waypoints() if kind == "traffic.traffic_light"
                             else [session.map.get_waypoint(trigger)])
                rows = []
                for wp in waypoints:
                    if wp is None:
                        continue
                    position = xyz(wp.transform.location)
                    direction = xyz(wp.transform.get_forward_vector())
                    right = xyz(wp.transform.get_right_vector())
                    side = right * wp.lane_width / 2
                    rows.append(freeze({"actor_id": actor.id, "type": kind,
                        "trigger_position": xyz(trigger), "position": position, "direction": direction,
                        "right": right, "lane_width": float(wp.lane_width),
                        "stop_line": np.stack([position - side, position + side]),
                        "lane_ids": (wp.road_id, wp.section_id, wp.lane_id)}))
                if signal_cache is not None:
                    signal_cache[actor.id] = tuple(rows)
            else:
                rows = cached_signal
            raw_state = actor.get_state() if kind == "traffic.traffic_light" else None
            signals.extend(row.with_updates(state=int(raw_state) if raw_state is not None else 5,
                           prohibited=str(raw_state) == "Red" if raw_state is not None else True)
                           for row in rows)
        elif kind.startswith("static.") and (geometry_cache is not None or actor.id in session.registry.actors) and actor.is_alive:
            physical = find(actor.id) if callable(find) else None
            transform = physical.get_transform() if physical is not None else actor.get_transform()
            row = {"actor_id": actor.id, "position": xyz(transform.location),
                   "size": 2 * np.abs(xyz(actor.bounding_box.extent))}
            if geometry_cache is not None:
                from .vehicle_geometry import geometry, rotation, static_type
                if actor.id not in geometry_cache:
                    geometry_cache[actor.id] = geometry(actor)
                g = geometry_cache[actor.id]
                row.update(center=xyz(transform.location) + rotation(transform) @ np.asarray(g["bbox_offset"]),
                           yaw=-math.radians(transform.rotation.yaw)+g["bbox_yaw"],
                           type=static_type(kind), geometry_valid=g["bbox_valid"], source=1)
            obstacles.append(freeze(row))
    if geometry_cache is not None:
        live = set(states) | {o["actor_id"] for o in obstacles}
        for aid in tuple(geometry_cache):
            if aid not in live:
                del geometry_cache[aid]
    return FrameState(int(snapshot.frame), float(snapshot.timestamp.elapsed_seconds),
                      FrozenMapping(states), tuple(signals), tuple(obstacles))
