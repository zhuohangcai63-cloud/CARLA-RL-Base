"""Immutable, content-addressed map geometry and topology."""
from collections import OrderedDict
from threading import RLock
import hashlib
import math
import xml.etree.ElementTree as ET
import numpy as np
from .geometry import xyz


def road_speed_records(opendrive):
    """OpenDRIVE road-level defaults; dynamic signs remain in ego telemetry."""
    records = {}
    for road in ET.fromstring(opendrive).findall("road"):
        values = []
        for kind in road.findall("type"):
            speed = kind.find("speed")
            if speed is None:
                continue
            limit = None
            try:
                value = float(speed.get("max"))
                unit = speed.get("unit", "m/s")
                factor = {"m/s": 1.0, "km/h": 1 / 3.6, "mph": .44704}.get(unit)
                if factor is not None and math.isfinite(value) and value > 0:
                    limit = value * factor
            except (TypeError, ValueError):
                pass  # "no limit" and "undefined" are represented by validity.
            values.append((float(kind.get("s", 0)), limit))
        records[int(road.attrib["id"])] = sorted(values)
    return records


class StaticMapCache:
    """Process-local LRU bounded by both map count and retained NumPy bytes."""
    def __init__(self, max_entries=4, max_bytes=256 * 1024 * 1024):
        if max_entries < 1 or max_bytes < 1:
            raise ValueError("Static map cache bounds must be positive")
        self.max_entries, self.max_bytes = int(max_entries), int(max_bytes)
        self._maps = OrderedDict()
        self._bytes = 0
        self._lock = RLock()
        self.hits = self.misses = 0

    def get(self, map_id):
        with self._lock:
            if map_id not in self._maps:
                self.misses += 1
                return None
            self.hits += 1
            self._maps.move_to_end(map_id)
            return dict(self._maps[map_id][0])

    def put(self, map_id, value):
        # Use immutable byte backing, so setflags(write=True) cannot bypass the
        # read-only view and corrupt other episodes' cached geometry.
        frozen = {key: np.frombuffer(item.tobytes(), dtype=item.dtype).reshape(item.shape)
                  if isinstance(item, np.ndarray) else item for key, item in value.items()}
        size = sum(item.nbytes for item in frozen.values() if isinstance(item, np.ndarray))
        with self._lock:
            previous = self._maps.pop(map_id, None)
            if previous is not None:
                self._bytes -= previous[1]
            if size <= self.max_bytes:
                while self._maps and (len(self._maps) >= self.max_entries or self._bytes + size > self.max_bytes):
                    _, (_, evicted_size) = self._maps.popitem(last=False)
                    self._bytes -= evicted_size
                self._maps[map_id] = (frozen, size)
                self._bytes += size
        return dict(frozen)

    def clear(self):
        with self._lock:
            self._maps.clear()
            self._bytes = self.hits = self.misses = 0

    def info(self):
        with self._lock:
            return {"entries": len(self._maps), "bytes": self._bytes,
                    "max_entries": self.max_entries, "max_bytes": self.max_bytes,
                    "hits": self.hits, "misses": self.misses}


DEFAULT_MAP_CACHE = StaticMapCache()


def build_map(world_map, *, cache=DEFAULT_MAP_CACHE):
    """Reuse geometry by its OpenDRIVE/schema digest across episode rebuilds.

    Pass cache=None to disable retention. Returned metadata is a distinct dict;
    retained arrays are immutable and safe to share across episodes.
    """
    opendrive = world_map.to_opendrive()
    map_id = hashlib.sha256(b"easycarla-map-v1:" + opendrive.encode()).hexdigest()
    cached = cache.get(map_id) if cache is not None else None
    if cached is not None:
        cached["name"] = world_map.name
        return cached
    result = _build_map(world_map, opendrive, map_id)
    return cache.put(map_id, result) if cache is not None else result


def _build_map(world_map, opendrive, map_id):
    speeds = road_speed_records(opendrive)
    groups = {}
    for wp in world_map.generate_waypoints(2.0):
        groups.setdefault((wp.road_id, wp.section_id, wp.lane_id), []).append(wp)
    points, left, right, offsets, ids, widths, lane_types, changes = [], [], [], [0], [], [], [], []
    group_indices = {}
    speed_limits, speed_valid = [], []
    for index, (key, wps) in enumerate(sorted(groups.items())):
        group_indices[key] = index
        # OpenDRIVE positive lanes normally travel toward decreasing s.
        wps.sort(key=lambda wp: wp.s, reverse=key[2] > 0)
        ids.append(key)
        for wp in wps:
            p = xyz(wp.transform.location)
            r = xyz(wp.transform.get_right_vector()) * wp.lane_width / 2
            points.append(p); left.append(p - r); right.append(p + r)
            widths.append(wp.lane_width)
            lane_types.append(int(wp.lane_type))
            changes.append(int(wp.lane_change))
            candidates = [value for s, value in speeds.get(wp.road_id, []) if s <= wp.s]
            known = bool(candidates) and candidates[-1] is not None
            speed_limits.append(candidates[-1] if known else 0.0)
            speed_valid.append(known)
        offsets.append(len(points))
    edges = []
    for start, end in world_map.get_topology():
        a = group_indices.get((start.road_id, start.section_id, start.lane_id))
        b = group_indices.get((end.road_id, end.section_id, end.lane_id))
        if a is not None and b is not None:
            edges.append([a, b])
        # CARLA topology segments often begin and end within one lane; include
        # the continuation beyond each endpoint to retain inter-road links.
        for following in end.next(2.0):
            target = group_indices.get((following.road_id, following.section_id, following.lane_id))
            if b is not None and target is not None:
                edges.append([b, target])
    edges = sorted(set(map(tuple, edges)))
    return {"map_id": map_id, "name": world_map.name, "coordinate_frame": "world_rh",
            "center": np.asarray(points, np.float32).reshape(-1, 3),
            "left_boundary": np.asarray(left, np.float32).reshape(-1, 3),
            "right_boundary": np.asarray(right, np.float32).reshape(-1, 3),
            "offsets": np.asarray(offsets, np.int32), "lane_ids": np.asarray(ids, np.int32).reshape(-1, 3),
            "lane_width": np.asarray(widths, np.float32), "lane_type": np.asarray(lane_types, np.int32),
            "lane_change": np.asarray(changes, np.int32),
            "speed_limit": np.asarray(speed_limits, np.float32),
            "speed_limit_valid": np.asarray(speed_valid, bool),
            "connections": np.asarray(edges, np.int32).reshape(-1, 2),
            "sampling_distance_m": 2.0, "schema_version": 1,
            "speed_limit_source": "opendrive_road_default; ego.speed_limit includes CARLA signs"}
