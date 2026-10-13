"""CARLA directed lane segments and ordered route corridors (not roadblocks)."""

import hashlib
from pathlib import Path
import numpy as np
from .geometry import xyz
from .maps import DEFAULT_MAP_CACHE, StaticMapCache, road_speed_records

ROUTE_CACHE = StaticMapCache(max_entries=8, max_bytes=32 * 1024 * 1024)


def build_planning_map(world_map):
    odr = world_map.to_opendrive()
    version = hashlib.sha256(
        b"".join(
            Path(__file__).with_name(name).read_bytes()
            for name in ("routes.py", "maps.py", "geometry.py")
        )
    ).hexdigest()
    map_id = hashlib.sha256(
        ("planning-map-v1:2.0:" + version + odr).encode()
    ).hexdigest()
    cached = DEFAULT_MAP_CACHE.get(map_id)
    if cached is not None:
        return cached
    groups = {}
    for wp in world_map.generate_waypoints(2.0):
        groups.setdefault((wp.road_id, wp.section_id, wp.lane_id), []).append(wp)
    segments = []
    for key, wps in sorted(groups.items()):
        wps.sort(key=lambda w: w.s, reverse=key[2] > 0)
        chunk = []
        for wp in wps:
            if chunk and (
                abs(wp.s - chunk[-1].s) > 6
                or bool(wp.is_junction) != bool(chunk[-1].is_junction)
            ):
                segments.append((key, chunk))
                chunk = []
            chunk.append(wp)
        if chunk:
            segments.append((key, chunk))
    keys = {}
    for i, (key, wps) in enumerate(segments):
        keys.setdefault(key, []).append(i)

    def index(wp):
        if wp is None:
            return -1
        choices = keys.get((wp.road_id, wp.section_id, wp.lane_id), [])
        if not choices:
            return -1
        return min(choices, key=lambda i: min(abs(wp.s - w.s) for w in segments[i][1]))

    center = []
    left = []
    right = []
    offsets = [0]
    edges = set()
    adj = []
    junction = []
    limits = []
    valid = []
    directions = []
    speeds = road_speed_records(odr)
    for i, (key, wps) in enumerate(segments):
        for wp in wps:
            p = xyz(wp.transform.location)
            d = xyz(wp.transform.get_forward_vector())
            side = xyz(wp.transform.get_right_vector()) * wp.lane_width / 2
            center.append(p)
            left.append(p - side)
            right.append(p + side)
            directions.append(d)
            records = [v for s, v in speeds.get(wp.road_id, []) if s <= wp.s]
            known = bool(records) and records[-1] is not None
            limits.append(records[-1] if known else 0.0)
            valid.append(known)
        offsets.append(len(center))
        junction.append(bool(wps[len(wps) // 2].is_junction))
        middle = wps[len(wps) // 2]
        adj.append([index(middle.get_left_lane()), index(middle.get_right_lane())])
        for wp in wps[-1].next(2.0):
            j = index(wp)
            if j >= 0 and j != i:
                edges.add((i, j))
        # A gap at a road-section boundary can be smaller than waypoint spacing.
        for wp in wps[0].previous(2.0):
            j = index(wp)
            if j >= 0 and j != i:
                edges.add((j, i))
    result = {
        "map_id": map_id,
        "schema_version": 2,
        "name": world_map.name,
        "coordinate_frame": "world_rh",
        "center": np.asarray(center, np.float32).reshape(-1, 3),
        "left_boundary": np.asarray(left, np.float32).reshape(-1, 3),
        "right_boundary": np.asarray(right, np.float32).reshape(-1, 3),
        "direction": np.asarray(directions, np.float32).reshape(-1, 3),
        "offsets": np.asarray(offsets, np.int32),
        "lane_ids": np.asarray([k for k, _ in segments], np.int32).reshape(-1, 3),
        "is_junction": np.asarray(junction, bool),
        "adjacency": np.asarray(adj, np.int32).reshape(-1, 2),
        "connections": np.asarray(sorted(edges), np.int32).reshape(-1, 2),
        "speed_limit": np.asarray(limits, np.float32),
        "speed_limit_valid": np.asarray(valid, bool),
        "boundary_source": "waypoint_half_lane_width_approximation",
        "boundary_is_road_mesh": False,
        "topology_source": "CARLA_waypoint_next_previous",
        "corridor_semantics": "directed_CARLA_lane_segments",
        "speed_limit_source": "OpenDRIVE_road_default",
        "sampling_distance_m": 2.0,
        "builder_version": version,
    }
    return DEFAULT_MAP_CACHE.put(map_id, result)


def describe_route(session, route, static):
    """Project original ordered route to directed map segments, retaining repeats."""
    positions = np.asarray([xyz(t.location) for t in route], np.float64).reshape(-1, 3)
    route_id = hashlib.sha256(
        b"carla-route-v1:" + static["map_id"].encode() + positions.tobytes()
    ).hexdigest()
    cached = ROUTE_CACHE.get(route_id)
    if cached is not None:
        return cached
    lane_indices = {}
    for i, key in enumerate(static["lane_ids"]):
        lane_indices.setdefault(tuple(key), []).append(i)
    point_lane = []
    point_segment = []
    ordered = []
    for transform in route:
        wp = session.map.get_waypoint(transform.location)
        lane = -1
        if wp is not None:
            key = (wp.road_id, wp.section_id, wp.lane_id)
            matches = lane_indices.get(key, [])
            if len(matches):
                p = xyz(transform.location)
                lane = int(
                    min(
                        matches,
                        key=lambda i: np.min(
                            np.linalg.norm(
                                static["center"][
                                    static["offsets"][i] : static["offsets"][i + 1]
                                ]
                                - p,
                                axis=1,
                            )
                        ),
                    )
                )
        point_lane.append(lane)
        if not ordered or lane != ordered[-1]:
            ordered.append(lane)
        point_segment.append(len(ordered) - 1)
    corridor = np.zeros(len(static["lane_ids"]), bool)
    for lane in ordered:
        if lane >= 0:
            corridor[lane] = True
    ids = np.asarray(ordered, np.int32)
    return ROUTE_CACHE.put(
        route_id,
        {
            "route_id": route_id,
            "map_id": static["map_id"],
            "schema_version": 1,
            "ordered_segments": ids,
            "corridor": corridor,
            "point_lane_index": np.asarray(point_lane, np.int32),
            "point_segment_index": np.asarray(point_segment, np.int32),
            "points_world": positions,
            "segment_valid": ids >= 0,
            "source": "CARLA_route_waypoint_projection",
            "semantics": "ordered_directed_lane_segments; not_nuPlan_roadblocks",
        },
    )
