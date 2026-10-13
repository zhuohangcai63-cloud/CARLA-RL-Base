from __future__ import annotations
import math
import random
from typing import Any
import carla
from easycarla.control.ownership import ControlConflict
from .types import SideSelection

class EventGeometry:
    """Geometry and sampling services with explicit dependencies, without event state."""
    def __init__(self, context, rng, stats, defaults, on_spawn=None):
        self.context = context
        self.rng = rng
        self.stats = stats
        self.DEFAULTS = defaults
        self.on_spawn = on_spawn

    def _straight_driving_segment(
        self,
        waypoint: carla.Waypoint,
        distance: float,
        yaw_tolerance: float,
    ) -> bool:
        previous_options = waypoint.previous(float(distance)) if hasattr(waypoint, "previous") else []
        previous_wp = min(
            previous_options,
            key=lambda wp: self._yaw_diff_abs(wp.transform.rotation.yaw, waypoint.transform.rotation.yaw),
        ) if previous_options else None
        next_wp = self._advance(waypoint, distance)
        if previous_wp is None or next_wp is None:
            return False
        for candidate in (previous_wp, next_wp):
            if candidate.is_junction:
                return False
            if not self._lane_type_matches(candidate, {carla.LaneType.Driving}):
                return False
            if candidate.road_id != waypoint.road_id or candidate.lane_id != waypoint.lane_id:
                return False
            if self._yaw_diff_abs(waypoint.transform.rotation.yaw, candidate.transform.rotation.yaw) > yaw_tolerance:
                return False
        return True


    def _anchor_candidates(self, spec: dict[str, Any], event_type: str) -> list[carla.Waypoint]:
        defaults = self.DEFAULTS[event_type]
        distance_min = max(0.0, self._float(spec, "event_distance_min", defaults["event_distance_min"]))
        distance_max = max(distance_min, self._float(spec, "event_distance_max", defaults["event_distance_max"]))
        route = list(getattr(self.context, "global_route", []) or [])
        ego = getattr(self.context, "ego", None)
        if ego is None:
            return []

        candidates: list[carla.Waypoint] = []
        world_map = self.context.world.get_map()
        ego_transform = ego.get_transform()
        for transform in route:
            local_x, local_y = self._world_to_ego_local(ego_transform, transform.location)
            if local_x < distance_min or local_x > distance_max or abs(local_y) > 8.0:
                continue
            waypoint = world_map.get_waypoint(
                transform.location, project_to_road=True, lane_type=carla.LaneType.Driving
            )
            if self._valid_anchor(waypoint):
                candidates.append(waypoint)

        if not candidates and event_type == "construction_obstacle":
            for transform in route:
                local_x, local_y = self._world_to_ego_local(ego_transform, transform.location)
                if local_x < 15.0 or local_x > 140.0 or abs(local_y) > 10.0:
                    continue
                waypoint = world_map.get_waypoint(
                    transform.location, project_to_road=True, lane_type=carla.LaneType.Driving
                )
                if self._valid_anchor(waypoint):
                    candidates.append(waypoint)

        if not candidates:
            ego_wp = world_map.get_waypoint(
                ego.get_location(), project_to_road=True, lane_type=carla.LaneType.Driving
            )
            for distance in (distance_min, (distance_min + distance_max) * 0.5, distance_max):
                waypoint = self._advance(ego_wp, distance)
                if self._valid_anchor(waypoint):
                    candidates.append(waypoint)

        deduped = []
        seen = set()
        for waypoint in candidates:
            key = (
                waypoint.road_id,
                waypoint.lane_id,
                round(waypoint.transform.location.x, 1),
                round(waypoint.transform.location.y, 1),
            )
            if key not in seen:
                seen.add(key)
                deduped.append(waypoint)
        self.rng.shuffle(deduped)
        return deduped[:20]


    def _valid_anchor(self, waypoint: carla.Waypoint | None) -> bool:
        if waypoint is None:
            return False
        if waypoint.is_junction:
            return False
        if not self._lane_type_matches(waypoint, {carla.LaneType.Driving}):
            return False
        left = waypoint.get_left_lane()
        right = waypoint.get_right_lane()
        self.stats["anchor_lane_id"] = waypoint.lane_id
        self.stats["anchor_has_left_lane"] = left is not None
        self.stats["anchor_has_right_lane"] = right is not None
        return True


    def _select_side(
        self,
        anchor: carla.Waypoint,
        spec: dict[str, Any],
        event_type: str,
    ) -> SideSelection | None:
        sides = self._ordered_sides(spec)
        allowed_passes = self._side_lane_allowed_passes(event_type, spec)

        for allowed in allowed_passes:
            for side in sides:
                if side not in {"left", "right"}:
                    continue
                side_wp = anchor.get_left_lane() if side == "left" else anchor.get_right_lane()
                if side_wp is None:
                    continue
                if not self._lane_type_matches(side_wp, allowed):
                    continue
                if self._yaw_diff_abs(anchor.transform.rotation.yaw, side_wp.transform.rotation.yaw) > 35.0:
                    continue
                return SideSelection(side=side, waypoint=side_wp)
        return None


    def _ordered_sides(self, spec: dict[str, Any]) -> list[str]:
        direction = str(spec.get("direction", "auto")).strip().lower()
        sides = ["left", "right"] if direction in {"auto", "random", ""} else [direction]
        if direction in {"auto", "random", ""}:
            self.rng.shuffle(sides)
        return sides


    def _side_lane_allowed_passes(self, event_type: str, spec: dict[str, Any]) -> list[set[Any]]:
        driving_or_parking = {carla.LaneType.Driving}
        parking_type = getattr(carla.LaneType, "Parking", None)
        if parking_type is not None:
            driving_or_parking.add(parking_type)

        if (
            event_type == "parking_cut_in"
            and parking_type is not None
            and self._bool(
                spec,
                "prefer_parking_lane",
                bool(self.DEFAULTS["parking_cut_in"]["prefer_parking_lane"]),
            )
        ):
            return [{parking_type}, driving_or_parking]

        return [driving_or_parking]


    def _try_spawn_vehicle(
        self,
        transform: carla.Transform,
        role_name: str,
        require_dynamic_doors: bool = False,
    ) -> carla.Vehicle | None:
        blueprints = self._vehicle_blueprints(require_dynamic_doors=require_dynamic_doors)
        if not blueprints:
            return None
        self.rng.shuffle(blueprints)
        for blueprint in blueprints[:12]:
            if blueprint.has_attribute("color"):
                colors = list(blueprint.get_attribute("color").recommended_values)
                if colors:
                    blueprint.set_attribute("color", self.rng.choice(colors))
            if blueprint.has_attribute("role_name"):
                blueprint.set_attribute("role_name", role_name)
            actor = self.context.world.try_spawn_actor(blueprint, transform)
            if actor is not None:
                if self.on_spawn is not None:
                    self.on_spawn(actor.id)
                return actor
        return None


    def _vehicle_blueprints(self, require_dynamic_doors: bool = False) -> list[carla.ActorBlueprint]:
        blueprints = []
        blacklist = getattr(self.context, "npc_vehicle_blacklist", set())
        for bp in self.context.world.get_blueprint_library().filter("vehicle.*"):
            if bp.id.lower() in blacklist:
                continue
            if bp.has_attribute("number_of_wheels"):
                try:
                    if int(bp.get_attribute("number_of_wheels")) != 4:
                        continue
                except (TypeError, ValueError):
                    continue
            if require_dynamic_doors:
                if not bp.has_attribute("has_dynamic_doors"):
                    continue
                raw_value = str(bp.get_attribute("has_dynamic_doors")).lower()
                if "true" not in raw_value and "1" not in raw_value:
                    continue
            blueprints.append(bp)
        return blueprints


    @staticmethod
    def _find_blueprint(library: carla.BlueprintLibrary, blueprint_id: str):
        matches = library.filter(blueprint_id)
        return matches[0] if matches else None


    def _event_vehicle(self, event: dict[str, Any]):
        vehicle_id = event.get("vehicle_id")
        if vehicle_id is None:
            return None
        return self.context.world.get_actor(vehicle_id)


    def _should_trigger(self, event: dict[str, Any]) -> bool:
        ego = getattr(self.context, "ego", None)
        if ego is None:
            return False
        trigger_location = event["trigger_location"]
        distance = ego.get_location().distance(trigger_location)
        if distance <= float(event.get("trigger_distance", 10.0)):
            return True
        speed = max(0.1, self._actor_speed(ego))
        time_to_arrival = distance / speed
        return time_to_arrival <= float(event.get("reaction_time", 2.5))


    def _ego_passed_anchor(self, event: dict[str, Any], margin: float) -> bool:
        ego = getattr(self.context, "ego", None)
        if ego is None:
            return False
        anchor = event["anchor_location"]
        forward = event["anchor_forward"]
        ego_loc = ego.get_location()
        progress = (
            (ego_loc.x - anchor.x) * forward.x
            + (ego_loc.y - anchor.y) * forward.y
            + (ego_loc.z - anchor.z) * forward.z
        )
        return progress > float(margin)


    def _sample_trigger_distance(self, spec: dict[str, Any], event_type: str) -> float:
        defaults = self.DEFAULTS[event_type]
        d_min = self._float(spec, "trigger_distance_min", defaults["trigger_distance_min"])
        d_max = self._float(spec, "trigger_distance_max", defaults["trigger_distance_max"])
        if d_max < d_min:
            d_max = d_min
        return self.rng.uniform(d_min, d_max)


    def _sample_speed(self, spec: dict[str, Any], event_type: str) -> float:
        defaults = self.DEFAULTS[event_type]
        s_min = max(0.0, self._float(spec, "actor_speed_min", defaults["actor_speed_min"]))
        s_max = self._float(spec, "actor_speed_max", defaults["actor_speed_max"])
        if s_max < s_min:
            s_max = s_min
        return self.rng.uniform(s_min, s_max)


    def _town_allowed(self, spec: dict[str, Any]) -> bool:
        allowed = spec.get("allowed_towns")
        if allowed is None:
            return True
        if isinstance(allowed, str):
            towns = {item.strip() for item in allowed.split(",") if item.strip()}
        else:
            towns = {str(item).strip() for item in allowed if str(item).strip()}
        return not towns or str(getattr(self.context, "town_name", "")) in towns


    def _location_clear(self, location: carla.Location, min_distance: float) -> bool:
        for pattern in ("vehicle.*", "walker.*"):
            for actor in self.context.world.get_actors().filter(pattern):
                ego = getattr(self.context, "ego", None)
                if ego is not None and actor.id == ego.id:
                    continue
                try:
                    if actor.get_location().distance(location) < min_distance:
                        return False
                except ControlConflict:
                    raise
                except RuntimeError:
                    continue
        return True


    def _advance(self, waypoint: carla.Waypoint | None, distance: float) -> carla.Waypoint | None:
        if waypoint is None:
            return None
        return self.context._advance_waypoint_along_lane(waypoint, distance)


    def _selection_spawn_transform(self, side: SideSelection, z_offset: float) -> carla.Transform:
        transform = side.spawn_transform or side.waypoint.transform
        return self._raised_transform(transform, z_offset)


    @staticmethod
    def _raised_transform(transform: carla.Transform, z_offset: float) -> carla.Transform:
        return carla.Transform(
            transform.location + carla.Location(z=z_offset),
            transform.rotation,
        )


    @staticmethod
    def _world_to_ego_local(ego_transform: carla.Transform, location: carla.Location) -> tuple[float, float]:
        yaw = math.radians(ego_transform.rotation.yaw)
        rel_x = location.x - ego_transform.location.x
        rel_y = location.y - ego_transform.location.y
        local_x = math.cos(-yaw) * rel_x - math.sin(-yaw) * rel_y
        local_y = math.sin(-yaw) * rel_x + math.cos(-yaw) * rel_y
        return local_x, local_y


    @staticmethod
    def _actor_speed(actor: carla.Actor) -> float:
        velocity = actor.get_velocity()
        return float(math.sqrt(velocity.x ** 2 + velocity.y ** 2 + velocity.z ** 2))


    @staticmethod
    def _yaw_diff_abs(yaw_a: float, yaw_b: float) -> float:
        return abs((yaw_a - yaw_b + 180.0) % 360.0 - 180.0)


    @staticmethod
    def _yaw_diff_signed(target_yaw: float, current_yaw: float) -> float:
        return (target_yaw - current_yaw + 180.0) % 360.0 - 180.0


    @staticmethod
    def _lane_type_matches(waypoint: carla.Waypoint, allowed: set[Any]) -> bool:
        for lane_type in allowed:
            try:
                if waypoint.lane_type == lane_type or (waypoint.lane_type & lane_type):
                    return True
            except TypeError:
                if waypoint.lane_type == lane_type:
                    return True
        return False


    @staticmethod
    def _float(spec: dict[str, Any], key: str, default: float) -> float:
        try:
            value = spec.get(key, default)
            if value is None:
                return float(default)
            return float(value)
        except (TypeError, ValueError):
            return float(default)


    @staticmethod
    def _int(spec: dict[str, Any], key: str, default: int) -> int:
        try:
            value = spec.get(key, default)
            if value is None:
                return int(default)
            return int(round(float(value)))
        except (TypeError, ValueError):
            return int(default)


    @staticmethod
    def _bool(spec: dict[str, Any], key: str, default: bool) -> bool:
        value = spec.get(key, default)
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"1", "true", "yes", "on"}:
                return True
            if normalized in {"0", "false", "no", "off"}:
                return False
        return bool(default)


