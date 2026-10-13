from __future__ import annotations
import math
import random
from typing import Any
import carla
from easycarla.control.ownership import ControlConflict
from .types import SideSelection
from .base import ScriptedEvent

class ParkingEvent(ScriptedEvent):
    kind = 'parking_cut_in'
    EVENT_FIELDS = ('vehicle_id', 'side', 'source', 'target_location', 'target_lane_id', 'target_road_id', 'target_speed', 'trigger_location', 'trigger_distance', 'reaction_time')

    def before_tick(self):
        for event in list(self.events):
            if event["state"] in {"active", "cutting"}:
                self._apply_merge_control(event)

    def spawn(self, spec: dict[str, Any]) -> bool:
        for anchor in self.geometry._anchor_candidates(spec, "parking_cut_in"):
            side = self.geometry._select_side(anchor, spec, "parking_cut_in")
            if side is None:
                continue
            spawn_transform = self.geometry._selection_spawn_transform(side, 0.35)
            if not self.geometry._location_clear(spawn_transform.location, 8.0):
                continue
            vehicle = self.geometry._try_spawn_vehicle(spawn_transform, role_name="long_tail_parking_cut_in")
            if vehicle is None:
                continue
            self._register_parking_cut_in_event(vehicle, side, anchor, spec, "route_side_lane")
            return True

        if self._spawn_global_parking_cut_in_fallback(spec):
            return True

        self._mark_skip("parking_cut_in", "no_side_lane_or_global_parking")
        return False


    def _register_parking_cut_in_event(
        self,
        vehicle: carla.Vehicle,
        side: SideSelection,
        anchor: carla.Waypoint,
        spec: dict[str, Any],
        source: str,
    ):
        if side.spawn_transform is None:
            self._move_parked_actor_to_outer_edge(vehicle, side.waypoint, side.side)
        try:
            self.context.control(vehicle, carla.VehicleControl(hand_brake=True), source=self.kind)
        except ControlConflict:
            raise
        except RuntimeError:
            pass

        target_distance = self.geometry._float(
            spec,
            "merge_target_distance",
            self.DEFAULTS["parking_cut_in"]["merge_target_distance"],
        )
        target_wp = self.geometry._advance(anchor, target_distance) or anchor
        event = self._base_event("parking_cut_in", spec, anchor, [vehicle.id])
        event.update({
            "state": "waiting",
            "vehicle_id": vehicle.id,
            "side": side.side,
            "source": source,
            "target_location": target_wp.transform.location,
            "target_lane_id": anchor.lane_id,
            "target_road_id": anchor.road_id,
            "target_speed": self.geometry._sample_speed(spec, "parking_cut_in"),
            "trigger_location": anchor.transform.location,
            "trigger_distance": self.geometry._sample_trigger_distance(spec, "parking_cut_in"),
            "reaction_time": self.geometry._float(
                spec, "reaction_time", self.DEFAULTS["parking_cut_in"]["reaction_time"]
            ),
        })
        self._register_event(event)
        self._set_last(event, "waiting", source)


    def _spawn_global_parking_cut_in_fallback(self, spec: dict[str, Any]) -> bool:
        defaults = self.DEFAULTS["parking_cut_in"]
        if not self.geometry._bool(spec, "global_parking_fallback", bool(defaults["global_parking_fallback"])):
            return False

        target_count = max(1, self.geometry._int(
            spec,
            "global_parking_fallback_count",
            int(defaults["global_parking_fallback_count"]),
        ))
        target_count = min(target_count, self.remaining_capacity)
        self._request_fallback_instances(target_count)
        candidates = self._global_parking_cut_in_candidates(spec)
        spawned = 0
        for anchor, side in candidates:
            if spawned >= target_count:
                break
            spawn_transform = self.geometry._selection_spawn_transform(side, 0.35)
            if not self.geometry._location_clear(spawn_transform.location, 7.0):
                continue
            vehicle = self.geometry._try_spawn_vehicle(spawn_transform, role_name="long_tail_parking_cut_in")
            if vehicle is None:
                continue
            self._register_parking_cut_in_event(vehicle, side, anchor, spec, "global_parking_fallback")
            spawned += 1
        if 0 < spawned < target_count:
            self._mark_skip("parking_cut_in", "global_fallback_partial_spawn")
        return spawned > 0


    def _global_parking_cut_in_candidates(
        self,
        spec: dict[str, Any],
    ) -> list[tuple[carla.Waypoint, SideSelection]]:
        if not hasattr(carla.LaneType, "Parking"):
            return []

        world_map = self.context.world.get_map()
        defaults = self.DEFAULTS["parking_cut_in"]
        spacing = max(2.0, self.geometry._float(spec, "global_parking_spacing", defaults["global_parking_spacing"]))
        min_ego_distance = max(0.0, self.geometry._float(
            spec,
            "global_parking_min_ego_distance",
            defaults["global_parking_min_ego_distance"],
        ))
        ego = getattr(self.context, "ego", None)
        ego_location = ego.get_location() if ego is not None else None

        try:
            waypoints = world_map.generate_waypoints(spacing)
        except ControlConflict:
            raise
        except RuntimeError:
            return []

        parking_type = getattr(carla.LaneType, "Parking")
        parking_candidates: list[tuple[carla.Waypoint, SideSelection]] = []
        roadside_candidates: list[tuple[carla.Waypoint, SideSelection]] = []
        seen = set()
        for anchor in waypoints:
            if anchor is None or anchor.is_junction:
                continue
            if not self.geometry._lane_type_matches(anchor, {carla.LaneType.Driving}):
                continue
            for side in self.geometry._ordered_sides(spec):
                side_wp = anchor.get_left_lane() if side == "left" else anchor.get_right_lane()
                if side_wp is None:
                    continue
                if not self.geometry._lane_type_matches(side_wp, {parking_type}):
                    continue
                if self.geometry._yaw_diff_abs(anchor.transform.rotation.yaw, side_wp.transform.rotation.yaw) > 35.0:
                    continue
                if ego_location is not None and side_wp.transform.location.distance(ego_location) < min_ego_distance:
                    continue
                key = (
                    side_wp.road_id,
                    side_wp.lane_id,
                    round(float(getattr(side_wp, "s", side_wp.transform.location.x)) / spacing),
                )
                if key in seen:
                    continue
                seen.add(key)
                parking_candidates.append((anchor, SideSelection(side=side, waypoint=side_wp)))

        if parking_candidates:
            self.rng.shuffle(parking_candidates)
            return parking_candidates

        seen.clear()
        for anchor in waypoints:
            if anchor is None or anchor.is_junction:
                continue
            if not self.geometry._lane_type_matches(anchor, {carla.LaneType.Driving}):
                continue
            for side in self.geometry._ordered_sides(spec):
                selection = self._roadside_parking_selection(anchor, side)
                if selection is None:
                    continue
                location = selection.spawn_transform.location
                if ego_location is not None and location.distance(ego_location) < min_ego_distance:
                    continue
                key = (
                    anchor.road_id,
                    anchor.lane_id,
                    side,
                    round(float(getattr(anchor, "s", anchor.transform.location.x)) / spacing),
                )
                if key in seen:
                    continue
                seen.add(key)
                roadside_candidates.append((anchor, selection))

        self.rng.shuffle(roadside_candidates)
        return roadside_candidates


    def _roadside_parking_selection(self, anchor: carla.Waypoint, side: str) -> SideSelection | None:
        if side not in {"left", "right"}:
            return None

        side_wp = anchor.get_left_lane() if side == "left" else anchor.get_right_lane()
        shoulder_type = getattr(carla.LaneType, "Shoulder", None)
        if shoulder_type is not None and side_wp is not None and self.geometry._lane_type_matches(side_wp, {shoulder_type}):
            if self.geometry._yaw_diff_abs(anchor.transform.rotation.yaw, side_wp.transform.rotation.yaw) <= 35.0:
                return SideSelection(
                    side=side,
                    waypoint=side_wp,
                    spawn_transform=side_wp.transform,
                )

        if side_wp is not None and self.geometry._lane_type_matches(side_wp, {carla.LaneType.Driving}):
            return None

        right = anchor.transform.get_right_vector()
        sign = -1.0 if side == "left" else 1.0
        lateral_offset = max(2.8, float(anchor.lane_width) * 0.5 + 1.2)
        location = anchor.transform.location + carla.Location(
            x=right.x * sign * lateral_offset,
            y=right.y * sign * lateral_offset,
            z=0.05,
        )
        transform = carla.Transform(location=location, rotation=anchor.transform.rotation)
        return SideSelection(side=side, waypoint=anchor, spawn_transform=transform)


    def update(self, event: dict[str, Any]):
        vehicle = self.geometry._event_vehicle(event)
        if vehicle is None:
            self._fail_event(event, "vehicle_missing")
            return

        if event["state"] == "waiting" and self.geometry._should_trigger(event):
            current_step = int(getattr(self.context, "time_step", 0))
            event["state"] = "cutting"
            event["trigger_step"] = current_step
            event["timeout_step"] = current_step + int(event.get("timeout_duration_steps", 1))
            self.stats["triggered"] += 1
            self._set_last(event, "triggered", "trigger_condition_met")
            try:
                self.context.control(vehicle, carla.VehicleControl(hand_brake=False), source=self.kind)
            except ControlConflict:
                raise
            except RuntimeError:
                pass

        if event["state"] != "cutting":
            return

        current_step = int(getattr(self.context, "time_step", 0))
        if current_step >= event["timeout_step"]:
            self._fail_event(event, "event_timeout")
            return

        waypoint = self.context.world.get_map().get_waypoint(
            vehicle.get_location(), project_to_road=True, lane_type=carla.LaneType.Driving
        )
        target_location = event["target_location"]
        distance_to_target = vehicle.get_location().distance(target_location)
        lane_reached = (
            waypoint is not None
            and waypoint.road_id == event["target_road_id"]
            and waypoint.lane_id == event["target_lane_id"]
        )
        if lane_reached and distance_to_target < 8.0:
            self._complete_event(event, "merged_into_ego_lane")


    def _apply_merge_control(self, event: dict[str, Any]):
        vehicle = self.geometry._event_vehicle(event)
        if vehicle is None:
            return
        target = event["target_location"]
        transform = vehicle.get_transform()
        location = transform.location
        dx = target.x - location.x
        dy = target.y - location.y
        distance = max(0.1, math.hypot(dx, dy))
        desired_yaw = math.degrees(math.atan2(dy, dx))
        yaw_error = self.geometry._yaw_diff_signed(desired_yaw, transform.rotation.yaw)
        speed = self.geometry._actor_speed(vehicle)
        target_speed = float(event.get("target_speed", 8.0))

        steer = max(-0.8, min(0.8, yaw_error / 55.0))
        throttle = 0.25
        brake = 0.0
        if speed < target_speed:
            throttle = min(0.75, 0.35 + 0.05 * (target_speed - speed))
        elif speed > target_speed + 2.0:
            throttle = 0.0
            brake = 0.25

        if distance < 5.0:
            steer *= 0.5
            throttle = min(throttle, 0.25)

        try:
            self.context.control(vehicle, carla.VehicleControl(throttle=throttle, steer=steer, brake=brake), source=self.kind)
        except ControlConflict:
            raise
        except RuntimeError as exc:
            self._fail_event(event, f"control_failed:{exc}")


    def _move_parked_actor_to_outer_edge(self, actor: carla.Actor, wp: carla.Waypoint, side: str):
        displacement = max(0.2, (float(wp.lane_width) - float(actor.bounding_box.extent.y)) / 4.0)
        vector = wp.transform.get_right_vector()
        if side == "left":
            displacement *= -1.0
        location = wp.transform.location + carla.Location(
            x=vector.x * displacement,
            y=vector.y * displacement,
            z=0.05,
        )
        actor.set_location(location)


