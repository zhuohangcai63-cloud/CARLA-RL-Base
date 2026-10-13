"""Explicit, portable state encoding for versioned event schemas.

This is execution state for rebuild/replay comparisons, not a CARLA snapshot.
Only declared fields enter exports; incidental caches cannot change the schema.
"""
from __future__ import annotations
from collections.abc import Mapping
import math
import numpy as np
import carla


class StateExporter:
    ACTOR_FIELDS = frozenset({"vehicle_id", "walker_id", "hard_break_lead_vehicle_id",
                              "lead_brake_last_candidate_id"})
    ACTOR_LIST_FIELDS = frozenset({"actor_ids", "forced_crossing_walker_ids", "candidate_actor_ids"})

    def __init__(self, actor_roles: Mapping[int, str]):
        self.actor_roles = actor_roles
        self.limitations: list[str] = []

    def actor(self, actor_id):
        if actor_id is None:
            return None
        role = self.actor_roles.get(int(actor_id))
        if role is None:
            reason = "Event references an actor without a stable semantic role"
            if reason not in self.limitations:
                self.limitations.append(reason)
            return {"role": None, "unresolved": True}
        return {"role": str(role)}

    def fields(self, source, fields):
        return {key: self.value(source[key], key) for key in fields if key in source}

    def value(self, value, key=""):
        if key in self.ACTOR_FIELDS:
            return self.actor(value)
        if key in self.ACTOR_LIST_FIELDS:
            return [self.actor(actor_id) for actor_id in value]
        if value is None or isinstance(value, (str, bool, int)):
            return value
        if isinstance(value, (float, np.floating)):
            if not math.isfinite(value):
                raise ValueError(f"Non-finite event state field {key}")
            return float(value)
        if isinstance(value, np.integer):
            return int(value)
        if isinstance(value, np.ndarray):
            return self.value(value.tolist(), key)
        if isinstance(value, Mapping):
            return {str(k): self.value(v, str(k)) for k, v in value.items()}
        if isinstance(value, (tuple, list)):
            return [self.value(item) for item in value]
        if isinstance(value, (carla.Location, carla.Vector3D)):
            return {"x": float(value.x), "y": float(value.y), "z": float(value.z)}
        if isinstance(value, carla.VehicleDoor):
            return str(value)
        raise TypeError(f"Unsupported event state type for {key}: {type(value).__name__}")
