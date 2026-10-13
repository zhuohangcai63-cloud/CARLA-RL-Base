"""Fixed numeric wire projection, published before any episode exists."""

import numpy as np


def observation_spec(config):
    c = config["observations"]
    h = c["history_frames"]
    n = c["max_objects"]
    k = c["max_obstacles"]
    r = c["route_points"]
    s = c["max_signals"]
    spec = {}

    def add(path, shape=(), dtype="float32"):
        spec[path] = {"shape": list(shape), "dtype": dtype}

    def history(prefix, shape):
        for name in ("position", "velocity", "acceleration", "size"):
            add(prefix + "." + name, (*shape, 3))
        for name in ("yaw", "yaw_rate"):
            add(prefix + "." + name, shape)
        for name in ("valid", "geometry_valid", "acceleration_valid", "velocity_valid"):
            add(prefix + "." + name, shape, "bool")

    add("frame", dtype="int64")
    add("timestamp", dtype="float64")
    for name in ("position_world",):
        add("ego." + name, (3,), "float64")
    for name in ("velocity", "acceleration", "control", "size"):
        add("ego." + name, (3,))
    for name in ("yaw_world", "yaw_rate", "speed", "speed_limit"):
        add("ego." + name, dtype="float64")
    for name in ("position", "velocity", "acceleration", "size"):
        add("objects." + name, (h, n, 3))
    for name in ("yaw", "yaw_rate"):
        add("objects." + name, (h, n))
    add("objects.valid", (h, n), "bool")
    add("objects.type", (h, n), "uint8")
    add("objects.ids", (n,), "int64")
    add("objects.timestamps", (h,), "float64")
    for name in ("count", "truncated"):
        add("objects." + name, dtype="int64")
    add("route.points", (r, 3))
    add("route.valid", (r,), "bool")
    for name in ("progress", "length"):
        add("route." + name, dtype="float64")
    add("route.index", dtype="int64")
    for name in ("position", "direction"):
        add("signals." + name, (s, 3))
    add("signals.stop_line", (s, 2, 3))
    add("signals.lane_ids", (s, 3), "int32")
    add("signals.ids", (s,), "int64")
    add("signals.state", (s,), "int32")
    add("signals.valid", (s,), "bool")
    add("signals.truncated", dtype="int64")
    for name in ("position", "size"):
        add("obstacles." + name, (k, 3))
    add("obstacles.valid", (k,), "bool")
    add("obstacles.truncated", dtype="int64")
    if c["profile"] == "planning_v1":
        for prefix, shape in (
            ("planning.ego_history", (h,)),
            ("planning.objects", (n, h)),
            ("planning.current", ()),
        ):
            history(prefix, shape)
        for name, shape, dtype in [
            ("schema_version", (), "int32"),
            ("reference_position_world", (3,), "float64"),
            ("reference_yaw_world", (), "float32"),
            ("reference_valid", (), "bool"),
            ("timestamps", (h,), "float64"),
            ("history_ready", (), "bool"),
            ("history_coverage_seconds", (), "float32"),
            ("steering_angle", (), "float32"),
            ("steering_valid", (), "bool"),
            ("control", (3,), "float32"),
            ("yaw_rate", (), "float32"),
            ("route_segment_index", (), "int32"),
        ]:
            add("planning." + name, shape, dtype)
        for name in (
            "rear_axle_offset",
            "bbox_offset",
            "size",
            "center_of_mass_offset",
        ):
            add("planning.geometry." + name, (3,))
        for name in ("bbox_valid", "rear_axle_valid", "center_of_mass_valid"):
            add("planning.geometry." + name, dtype="bool")
        for name in ("wheelbase", "max_steer_angle"):
            add("planning.geometry." + name)
        for name, shape, dtype in [
            ("ids", (n,), "int64"),
            ("type", (n,), "uint8"),
            ("count", (), "int32"),
            ("truncated", (), "int32"),
        ]:
            add("planning.objects." + name, shape, dtype)
        for name, shape, dtype in [
            ("center", (k, 3), "float32"),
            ("size", (k, 3), "float32"),
            ("yaw", (k,), "float32"),
            ("type", (k,), "uint8"),
            ("ids", (k,), "int64"),
            ("valid", (k,), "bool"),
            ("geometry_valid", (k,), "bool"),
            ("source", (k,), "uint8"),
            ("count", (), "int32"),
            ("truncated", (), "int32"),
        ]:
            add("planning.static_objects." + name, shape, dtype)
        add("signals.segment_index", (s,), "int32")
        add("signals.association_valid", (s,), "bool")
    for sensor in ("rgb", "bev", "lidar"):
        if not c[sensor]:
            continue
        prefix = "sensors." + sensor
        add(prefix + ".frame", dtype="int64")
        add(prefix + ".timestamp", dtype="float64")
        for name in ("sensor_to_world", "sensor_to_ego"):
            add(prefix + "." + name, (4, 4), "float64")
        if sensor == "lidar":
            add(prefix + ".points", (c["lidar_points"], 4))
            add(prefix + ".count", dtype="int64")
            add(prefix + ".truncated", dtype="int64")
        else:
            add(
                prefix + ".data",
                (
                    (c["bev_size"], c["bev_size"])
                    if sensor == "bev"
                    else (c["image_height"], c["image_width"], 3)
                ),
                "uint8",
            )
    return {
        "version": 1,
        "fields": spec,
        "lidar_padding": "client_zero_pad_to_capacity",
    }
