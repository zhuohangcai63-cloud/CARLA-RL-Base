"""Optional SI, current-rear-axle FLU observations; no model normalization."""

import numpy as np
from .geometry import local, wrap


def planning_observation(builder):
    c = builder.config
    h, n, k = c["history_frames"], c["max_objects"], c["max_obstacles"]
    ego = builder.get_state(builder.episode.ego)
    origin, yaw = np.asarray(ego["rear_axle"]), ego["yaw"]
    ego_id = builder.semantic_id(builder.episode.ego)
    candidates = sorted(
        (float(np.linalg.norm(np.asarray(s["bbox_center"]) - origin)), sid)
        for sid, s in builder.last_states.items()
        if sid != ego_id
    )
    candidates = [row for row in candidates if row[0] <= c["range"]]
    chosen = [sid for _, sid in candidates[:n]]
    ego_history = history_arrays((h,))
    objects = history_arrays((n, h))
    objects.update(
        ids=np.zeros(n, np.int64),
        type=np.zeros(n, np.uint8),
        count=np.int32(len(chosen)),
        truncated=np.int32(max(0, len(candidates) - n)),
    )
    objects["ids"][: len(chosen)] = chosen
    timestamps = np.zeros(h, np.float64)
    for hi, (ts, states) in enumerate(builder.history, start=h - len(builder.history)):
        timestamps[hi] = ts
        if ego_id in states:
            fill(ego_history, hi, states[ego_id], origin, yaw, rear=True)
        for i, sid in enumerate(chosen):
            if sid in states:
                fill(objects, (i, hi), states[sid], origin, yaw)
                objects["type"][i] = states[sid]["object_type"]
    g = ego["geometry"]
    coverage = (
        0.0 if not builder.history else builder.history[-1][0] - builder.history[0][0]
    )
    rows = list(builder.frame_state.obstacles) + builder.map_obstacles
    rows = sorted(
        (float(np.linalg.norm(np.asarray(o["center"]) - origin)), o["actor_id"], o)
        for o in rows
        if np.linalg.norm(np.asarray(o["center"]) - origin) <= c["range"]
    )
    static = {
        "center": np.zeros((k, 3), np.float32),
        "yaw": np.zeros(k, np.float32),
        "size": np.zeros((k, 3), np.float32),
        "type": np.zeros(k, np.uint8),
        "ids": np.zeros(k, np.int64),
        "valid": np.zeros(k, bool),
        "geometry_valid": np.zeros(k, bool),
        "source": np.zeros(k, np.uint8),
        "count": np.int32(min(k, len(rows))),
        "truncated": np.int32(max(0, len(rows) - k)),
    }
    for i, (_, aid, o) in enumerate(rows[:k]):
        static["center"][i] = local(o["center"], origin, yaw)
        static["yaw"][i] = wrap(o["yaw"] - yaw)
        for key in ("size", "type", "geometry_valid", "source"):
            static[key][i] = o[key]
        static["ids"][i], static["valid"][i] = aid, True
    return {
        "schema_version": np.int32(1),
        "reference_position_world": origin.astype(np.float64),
        "reference_yaw_world": np.float32(yaw),
        "reference_valid": np.bool_(g["rear_valid"]),
        "ego_history": ego_history,
        "objects": objects,
        "static_objects": static,
        "timestamps": timestamps,
        "history_coverage_seconds": np.float32(coverage),
        "history_ready": np.bool_(
            len(builder.history) == h
            and coverage >= (h - 1) * builder.history_dt - 1e-6
            and abs(builder.history[-1][0] - builder.frame_state.timestamp) < 1e-6
        ),
        "geometry": {
            "rear_axle_offset": np.asarray(g["rear_offset"], np.float32),
            "bbox_offset": np.asarray(g["bbox_offset"], np.float32),
            "bbox_valid": np.bool_(g["bbox_valid"]),
            "center_of_mass_offset": np.asarray(g["center_of_mass_offset"], np.float32),
            "center_of_mass_valid": np.bool_(g["center_of_mass_valid"]),
            "rear_axle_valid": np.bool_(g["rear_valid"]),
            "wheelbase": np.float32(g["wheelbase"]),
            "size": np.asarray(ego["size"], np.float32),
            "max_steer_angle": np.float32(g["max_steer_angle"]),
        },
        "steering_angle": np.float32(ego["steering_angle"]),
        "steering_valid": np.bool_(ego["steering_valid"]),
        "control": np.asarray(ego["control"], np.float32),
        "yaw_rate": np.float32(ego["yaw_rate"]),
        "current": current_state(ego, origin, yaw),
        "route_segment_index": np.int32(builder.route_segment_index),
    }


def current_state(state, origin, yaw):
    row = history_arrays(())
    fill(row, (), state, origin, yaw, rear=True)
    return row


def history_arrays(shape):
    return {
        "position": np.zeros((*shape, 3), np.float32),
        "velocity": np.zeros((*shape, 3), np.float32),
        "acceleration": np.zeros((*shape, 3), np.float32),
        "size": np.zeros((*shape, 3), np.float32),
        "yaw": np.zeros(shape, np.float32),
        "yaw_rate": np.zeros(shape, np.float32),
        "valid": np.zeros(shape, bool),
        "geometry_valid": np.zeros(shape, bool),
        "acceleration_valid": np.zeros(shape, bool),
        "velocity_valid": np.zeros(shape, bool),
    }


def fill(out, index, s, origin, yaw, rear=False):
    out["position"][index] = local(
        s["rear_axle"] if rear else s["bbox_center"], origin, yaw
    )
    out["velocity"][index] = local(
        s["rear_velocity"] if rear else s["bbox_velocity"], origin, yaw, vector=True
    )
    out["acceleration"][index] = local(
        (
            s.get("rear_acceleration", s["acceleration"])
            if rear
            else s.get("bbox_acceleration", s["acceleration"])
        ),
        origin,
        yaw,
        vector=True,
    )
    out["size"][index], out["yaw_rate"][index] = s["size"], s["yaw_rate"]
    out["yaw"][index] = wrap((s["yaw"] if rear else s["bbox_yaw"]) - yaw)
    out["valid"][index] = True
    out["geometry_valid"][index] = s["geometry"]["rear_valid" if rear else "bbox_valid"]
    out["velocity_valid"][index] = bool(
        out["geometry_valid"][index] and s["geometry"]["center_of_mass_valid"]
    )
    out["acceleration_valid"][index] = (
        s.get("rear_acceleration_valid", False)
        if rear
        else s.get("bbox_acceleration_valid", False)
    )
    out["acceleration_valid"][index] &= out["velocity_valid"][index]


def map_obstacles(session):
    """Recognizable CARLA environment props only; buildings/vegetation excluded."""
    result = []
    carla = getattr(session, "carla", None)
    query = getattr(session.world, "get_environment_objects", None)
    if not callable(query) or carla is None:
        return result
    import hashlib
    from .vehicle_geometry import static_type

    seen = set()
    world_origin = np.asarray(getattr(session, "world_origin", np.zeros(3)))
    for name in ("Other", "TrafficSigns"):
        label = getattr(carla.CityObjectLabel, name, None)
        if label is None:
            continue
        global_boxes = set()
        level_query = getattr(session.world, "get_level_bbs", None)

        def signature(box, origin):
            center = (
                np.asarray([box.location.x, -box.location.y, box.location.z]) + origin
            )
            return tuple(np.round(center, 1)) + tuple(
                np.round([box.extent.x, box.extent.y, box.extent.z], 2)
            )

        if np.any(world_origin) and callable(level_query):
            global_boxes = {signature(box, np.zeros(3)) for box in level_query(label)}
        for obj in query(label):
            if obj.id in seen:
                continue
            seen.add(obj.id)
            # 0.9.15 globalizes the environment object's transform, but not
            # its bounding box. Verify rebased boxes against global level BBs.
            box = obj.bounding_box
            center = (
                np.array([box.location.x, -box.location.y, box.location.z])
                + world_origin
            )
            verified = (
                not np.any(world_origin) or signature(box, world_origin) in global_boxes
            )
            aid = -1 - (
                int.from_bytes(
                    hashlib.blake2b(str(obj.id).encode(), digest_size=8).digest(),
                    "little",
                )
                & ((1 << 62) - 1)
            )
            result.append(
                {
                    "actor_id": aid,
                    "center": center.tolist(),
                    "yaw": -np.deg2rad(box.rotation.yaw),
                    "size": [2 * box.extent.x, 2 * box.extent.y, 2 * box.extent.z],
                    "type": static_type(obj.name),
                    "geometry_valid": bool(verified),
                    "source": 2,
                }
            )
    return result
