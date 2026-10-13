from __future__ import annotations
import math
import random
from typing import Any
import carla
from easycarla.control.ownership import ControlConflict
from .types import SideSelection
from .base import ScriptedEvent

class ConstructionEvent(ScriptedEvent):
    kind = 'construction_obstacle'
    EVENT_FIELDS = ('source', 'trigger_location')

    def spawn(self, spec: dict[str, Any]) -> bool:
        for anchor in self.geometry._anchor_candidates(spec, "construction_obstacle"):
            actor_ids = self._spawn_construction_props(anchor)
            if not actor_ids:
                continue
            self._register_construction_event(anchor, actor_ids, spec, "route_anchor")
            return True

        if self._spawn_global_construction_fallback(spec):
            return True

        self._mark_skip("construction_obstacle", "no_clear_route_anchor_or_global_straight")
        return False


    def _register_construction_event(
        self,
        anchor: carla.Waypoint,
        actor_ids: list[int],
        spec: dict[str, Any],
        source: str,
    ):
        event = self._base_event("construction_obstacle", spec, anchor, actor_ids)
        event.update({
            "state": "active",
            "source": source,
            "trigger_location": anchor.transform.location,
        })
        self._register_event(event)
        self.stats["triggered"] += 1
        self._set_last(event, "active", source)


    def _spawn_global_construction_fallback(self, spec: dict[str, Any]) -> bool:
        defaults = self.DEFAULTS["construction_obstacle"]
        if not self.geometry._bool(
            spec,
            "global_construction_fallback",
            bool(defaults["global_construction_fallback"]),
        ):
            return False

        target_count = max(1, self.geometry._int(
            spec,
            "global_construction_fallback_count",
            int(defaults["global_construction_fallback_count"]),
        ))
        target_count = min(target_count, self.remaining_capacity)
        self._request_fallback_instances(target_count)
        candidates = self._global_construction_candidates(spec)
        spawned = 0
        for anchor in candidates:
            if spawned >= target_count:
                break
            actor_ids = self._spawn_construction_props(anchor)
            if not actor_ids:
                continue
            self._register_construction_event(anchor, actor_ids, spec, "global_straight_fallback")
            spawned += 1
        if 0 < spawned < target_count:
            self._mark_skip("construction_obstacle", "global_fallback_partial_spawn")
        return spawned > 0


    def _global_construction_candidates(self, spec: dict[str, Any]) -> list[carla.Waypoint]:
        defaults = self.DEFAULTS["construction_obstacle"]
        world_map = self.context.world.get_map()
        spacing = max(4.0, self.geometry._float(
            spec,
            "global_construction_spacing",
            defaults["global_construction_spacing"],
        ))
        min_ego_distance = max(0.0, self.geometry._float(
            spec,
            "global_construction_min_ego_distance",
            defaults["global_construction_min_ego_distance"],
        ))
        yaw_tolerance = max(0.0, self.geometry._float(
            spec,
            "global_construction_straight_yaw_tolerance",
            defaults["global_construction_straight_yaw_tolerance"],
        ))
        check_distance = max(2.0, self.geometry._float(
            spec,
            "global_construction_straight_check_distance",
            defaults["global_construction_straight_check_distance"],
        ))
        ego = getattr(self.context, "ego", None)
        ego_location = ego.get_location() if ego is not None else None

        try:
            waypoints = world_map.generate_waypoints(spacing)
        except ControlConflict:
            raise
        except RuntimeError:
            return []

        candidates = []
        seen = set()
        for waypoint in waypoints:
            if waypoint is None or waypoint.is_junction:
                continue
            if not self.geometry._lane_type_matches(waypoint, {carla.LaneType.Driving}):
                continue
            if ego_location is not None and waypoint.transform.location.distance(ego_location) < min_ego_distance:
                continue
            if not self.geometry._straight_driving_segment(waypoint, check_distance, yaw_tolerance):
                continue
            key = (
                waypoint.road_id,
                waypoint.lane_id,
                round(float(getattr(waypoint, "s", waypoint.transform.location.x)) / spacing),
            )
            if key in seen:
                continue
            seen.add(key)
            candidates.append(waypoint)

        self.rng.shuffle(candidates)
        return candidates


    def update(self, event: dict[str, Any]):
        current_step = int(getattr(self.context, "time_step", 0))
        if current_step >= event["timeout_step"]:
            self._fail_event(event, "event_timeout")
            return
        if self.geometry._ego_passed_anchor(event, margin=15.0):
            self._complete_event(event, "ego_passed_obstacle")


    def _spawn_construction_props(self, anchor: carla.Waypoint) -> list[int]:
        library = self.context.world.get_blueprint_library()
        cone_bp = self.geometry._find_blueprint(library, "static.prop.constructioncone")
        warning_bp = self.geometry._find_blueprint(library, "static.prop.trafficwarning")
        debris_bp = self.geometry._find_blueprint(library, "static.prop.dirtdebris02")
        if cone_bp is None:
            return []

        yaw = anchor.transform.rotation.yaw
        forward = anchor.transform.get_forward_vector()
        right = anchor.transform.get_right_vector()
        center = anchor.transform.location
        lane_width = max(2.8, float(anchor.lane_width))
        actor_ids = []

        placements = []
        for idx, lateral in enumerate((-0.35, -0.1, 0.15, 0.4)):
            placements.append((
                cone_bp,
                carla.Location(
                    x=center.x + forward.x * (idx * 1.8) + right.x * lateral * lane_width,
                    y=center.y + forward.y * (idx * 1.8) + right.y * lateral * lane_width,
                    z=center.z + 0.15,
                ),
                yaw,
            ))
        if warning_bp is not None:
            placements.append((
                warning_bp,
                carla.Location(
                    x=center.x - forward.x * 5.0 - right.x * lane_width * 0.35,
                    y=center.y - forward.y * 5.0 - right.y * lane_width * 0.35,
                    z=center.z + 0.2,
                ),
                yaw + 180.0,
            ))
        if debris_bp is not None:
            placements.append((
                debris_bp,
                carla.Location(
                    x=center.x + forward.x * 4.0,
                    y=center.y + forward.y * 4.0,
                    z=center.z + 0.2,
                ),
                yaw,
            ))

        for bp, location, prop_yaw in placements:
            if not self.geometry._location_clear(location, 2.0):
                continue
            actor = self.context.world.try_spawn_actor(
                bp,
                carla.Transform(location=location, rotation=carla.Rotation(yaw=prop_yaw)),
            )
            if actor is None:
                continue
            self._own_actor(actor.id)
            try:
                actor.set_simulate_physics(False)
            except ControlConflict:
                raise
            except RuntimeError:
                pass
            actor_ids.append(actor.id)

        return actor_ids


