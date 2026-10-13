"""Fixed route geometry for generic following, merging and crossing fixtures.

This module only reads map waypoints. It never spawns actors, changes control,
or advances physics. Invalid geometry fails within the requested route family.
"""
from dataclasses import dataclass
import math


class RouteGeometryError(ValueError):
    pass


@dataclass
class ReactiveRoute:
    transforms: list
    metadata: dict


def _key(waypoint):
    return (waypoint.road_id, waypoint.lane_id, waypoint.s)


def _direction(transform):
    angle = math.radians(transform.rotation.yaw)
    return math.cos(angle), math.sin(angle)


def _driving(waypoint, carla):
    return waypoint is not None and waypoint.lane_type == carla.LaneType.Driving


def _advance(waypoint, distance, carla, spacing=2.0):
    remaining = abs(distance)
    while remaining > 1e-6:
        step = min(remaining, spacing)
        choices = waypoint.next(step) if distance >= 0 else waypoint.previous(step)
        choices = [w for w in choices if _driving(w, carla)]
        if not choices:
            return None
        waypoint = min(choices, key=_key)
        remaining -= step
    return waypoint


def _walk(waypoint, length, carla, spacing=2.0):
    route = [waypoint]
    for _ in range(math.ceil(length / spacing)):
        waypoint = _advance(waypoint, spacing, carla)
        if waypoint is None:
            break
        route.append(waypoint)
    return route


def _same_direction_adjacent(base, side, carla):
    other = base.get_left_lane() if side == "left" else base.get_right_lane()
    if not _driving(other, carla):
        raise RouteGeometryError(f"merge: requested {side} adjacent lane is not Driving")
    a, b = _direction(base.transform), _direction(other.transform)
    if a[0] * b[0] + a[1] * b[1] < 0.9 or base.lane_id * other.lane_id <= 0:
        raise RouteGeometryError(f"merge: requested {side} lane has an opposing direction")
    return other


def _anchor(road_map, ego_route, distance, carla):
    travelled = 0.0
    for previous, current in zip(ego_route, ego_route[1:]):
        travelled += previous.location.distance(current.location)
        if travelled >= distance:
            waypoint = road_map.get_waypoint(current.location)
            if not _driving(waypoint, carla):
                break
            return waypoint
    raise RouteGeometryError(f"No Driving waypoint {distance:g} m ahead on the configured ego route")


def _merge(road_map, ego_route, spec, carla):
    side = spec.get("side")
    if side not in ("left", "right"):
        raise RouteGeometryError("merge requires an explicit left or right side")
    length = _positive(spec, "merge_length", 30.0)
    continuation = _positive(spec, "continuation_length", 80.0)
    anchor = _anchor(road_map, ego_route, _positive(spec, "distance", 25.0), carla)
    target_lane = _walk(anchor, length, carla)
    if len(target_lane) < math.ceil(length / 2.0) + 1:
        raise RouteGeometryError("merge: target lane ends before the merge can finish")
    locations = []
    for index, target in enumerate(target_lane):
        if target.is_junction:
            raise RouteGeometryError("merge: a junction interrupts the fixed adjacent-lane merge")
        source = _same_direction_adjacent(target, side, carla)
        if source.road_id != target.road_id:
            raise RouteGeometryError("merge: adjacent lane belongs to a different road")
        t = min(1.0, index * 2.0 / length)
        weight = t * t * (3.0 - 2.0 * t)
        a, b = source.transform.location, target.transform.location
        locations.append(carla.Location(x=a.x + weight * (b.x - a.x),
                                        y=a.y + weight * (b.y - a.y), z=a.z + weight * (b.z - a.z)))
    locations.extend(w.transform.location for w in _walk(target_lane[-1], continuation, carla)[1:])
    transforms = []
    for index, location in enumerate(locations):
        a, b = (location, locations[index + 1]) if index + 1 < len(locations) else (locations[index - 1], location)
        yaw = math.degrees(math.atan2(b.y - a.y, b.x - a.x))
        pitch = math.degrees(math.atan2(b.z - a.z, math.hypot(b.x - a.x, b.y - a.y)))
        transforms.append(carla.Transform(location, carla.Rotation(yaw=yaw, pitch=pitch)))
    return ReactiveRoute(transforms, {"route_kind": "merge", "side": side,
                                      "merge_length": length, "target_road_id": anchor.road_id,
                                      "target_lane_id": anchor.lane_id, "validation": "geometry_only"})


def _intersection(a, b, c, d):
    """Return directed 2D segment intersection fractions, excluding parallels."""
    rx, ry, sx, sy = b.x - a.x, b.y - a.y, d.x - c.x, d.y - c.y
    cross = rx * sy - ry * sx
    norm = math.hypot(rx, ry) * math.hypot(sx, sy)
    if norm < 1e-9 or abs(rx * sx + ry * sy) / norm > math.cos(math.radians(45)):
        return None
    if abs(cross) < 1e-9:
        return None
    qx, qy = c.x - a.x, c.y - a.y
    t, u = (qx * sy - qy * sx) / cross, (qx * ry - qy * rx) / cross
    if not (-1e-6 <= t <= 1 + 1e-6 and -1e-6 <= u <= 1 + 1e-6):
        return None
    if abs(a.z + t * (b.z - a.z) - c.z - u * (d.z - c.z)) > 2.0:
        return None
    return t, u


def _crossing(road_map, ego_route, spec, carla, planner):
    minimum = _positive(spec, "distance", 20.0)
    maximum = _positive(spec, "search_distance", 140.0)
    approach = _positive(spec, "approach_distance", 25.0)
    departure = _positive(spec, "continuation_length", 60.0)
    side = spec.get("side")
    if side not in (None, "left", "right") or minimum >= maximum:
        raise RouteGeometryError("crossing requires left/right side and distance < search_distance")
    segments, junctions = [], {}
    travelled = 0.0
    for a, b in zip(ego_route, ego_route[1:]):
        length = a.location.distance(b.location)
        if travelled > maximum:
            break
        if travelled + length >= minimum:
            segments.append((travelled, length, a.location, b.location))
            for transform in (a, b):
                waypoint = road_map.get_waypoint(transform.location)
                if _driving(waypoint, carla) and waypoint.is_junction:
                    junction = waypoint.get_junction()
                    junctions[junction.id] = junction
        travelled += length
    candidates = []
    for junction_id, junction in sorted(junctions.items()):
        pairs = sorted(junction.get_waypoints(carla.LaneType.Driving), key=lambda p: (_key(p[0]), _key(p[1])))
        for entry, exit_ in pairs:
            start, end = _advance(entry, -approach, carla), _advance(exit_, departure, carla)
            if start is None or end is None:
                continue
            try:
                route = [w.transform for w, _ in planner.trace_route(start.transform.location, end.transform.location)]
            except Exception as exc:
                if isinstance(exc, (ValueError, RuntimeError)) or type(exc).__name__ in ("NetworkXNoPath", "NodeNotFound"):
                    continue
                raise
            path_distance = 0.0
            found = None
            for c, d in zip(route, route[1:]):
                segment_length = c.location.distance(d.location)
                for ego_distance, ego_length, a, b in segments:
                    intersection = _intersection(a, b, c.location, d.location)
                    if intersection is None:
                        continue
                    t, u = intersection
                    along_ego = ego_distance + t * ego_length
                    along_npc = path_distance + u * segment_length
                    if not minimum <= along_ego <= maximum or along_npc < min(10.0, approach / 2):
                        continue
                    x, y = a.x + t * (b.x - a.x), a.y + t * (b.y - a.y)
                    right = -(b.y - a.y) * (start.transform.location.x - x) + (b.x - a.x) * (start.transform.location.y - y)
                    actual_side = "right" if right > 0 else "left"
                    if side is not None and side != actual_side:
                        continue
                    candidate = (along_ego, along_npc, x, y, actual_side)
                    if found is None or candidate[:2] < found[:2]:
                        found = candidate
                path_distance += segment_length
            if found is not None and len(route) >= 2:
                candidates.append((found[0], junction_id, _key(entry), route, found))
    if not candidates:
        raise RouteGeometryError(f"crossing: no transverse Driving route with a real junction crossing {minimum:g}–{maximum:g} m ahead, side={side}")
    _, junction_id, _, route, found = min(candidates, key=lambda item: item[:3])
    return ReactiveRoute(route, {"route_kind": "crossing", "junction_id": junction_id, "side": found[4],
                                "ego_distance_to_conflict": found[0], "npc_distance_to_conflict": found[1],
                                "conflict_xy_carla": [found[2], found[3]], "validation": "geometry_only"})


def _positive(spec, key, default):
    value = float(spec.get(key, default))
    if not math.isfinite(value) or value <= 0:
        raise RouteGeometryError(f"{key} must be positive and finite")
    return value


def build_reactive_route(road_map, ego_route, spec, carla, planner=None):
    """Return fixed CARLA world transforms and metadata for one NPC role."""
    kind = spec.get("route_kind", "follow")
    if len(ego_route) < 2:
        raise RouteGeometryError("Reactive route requires a nonempty ego route")
    if kind == "merge":
        return _merge(road_map, ego_route, spec, carla)
    if kind == "crossing":
        if planner is None:
            from agents.navigation.global_route_planner import GlobalRoutePlanner
            planner = GlobalRoutePlanner(road_map, 2.0)
        return _crossing(road_map, ego_route, spec, carla, planner)
    if kind != "follow":
        raise RouteGeometryError(f"Unsupported reactive route_kind {kind!r}")
    # Preserve the original follow fixture's deterministic next-waypoint path.
    waypoint = _advance(road_map.get_waypoint(ego_route[0].location), _positive(spec, "distance", 25.0), carla)
    if waypoint is None:
        raise RouteGeometryError("follow: requested forward lane distance is unavailable")
    if spec.get("side"):
        if spec["side"] not in ("left", "right"):
            raise RouteGeometryError("follow side must be left or right")
        waypoint = _same_direction_adjacent(waypoint, spec["side"], carla)
    route = _walk(waypoint, _positive(spec, "continuation_length", 158.0), carla)
    if len(route) < 2:
        raise RouteGeometryError("follow: lane ends before a controller path can be built")
    return ReactiveRoute([w.transform for w in route], {"route_kind": "follow", "validation": "geometry_only"})
