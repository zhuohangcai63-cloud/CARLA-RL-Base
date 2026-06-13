from __future__ import annotations

from dataclasses import dataclass
import math
import random
from typing import Any

import carla


@dataclass
class SideSelection:
    side: str
    waypoint: carla.Waypoint


class LongTailEventManager:
    """Lightweight Bench2Drive-style events for the Gym CARLA environment."""

    SIDE_DEPENDENT_TYPES = {"side_cut_in", "parking_cut_in", "vehicle_opens_door"}
    SUPPORTED_TYPES = SIDE_DEPENDENT_TYPES | {"construction_obstacle"}

    DEFAULTS: dict[str, dict[str, Any]] = {
        "side_cut_in": {
            "event_distance_min": 25.0,
            "event_distance_max": 60.0,
            "trigger_distance_min": 12.0,
            "trigger_distance_max": 24.0,
            "reaction_time": 2.5,
            "actor_speed_min": 6.0,
            "actor_speed_max": 12.0,
            "event_timeout_steps": 220,
        },
        "parking_cut_in": {
            "event_distance_min": 28.0,
            "event_distance_max": 45.0,
            "trigger_distance_min": 8.0,
            "trigger_distance_max": 14.0,
            "reaction_time": 2.35,
            "actor_speed_min": 4.0,
            "actor_speed_max": 8.0,
            "event_timeout_steps": 220,
        },
        "vehicle_opens_door": {
            "event_distance_min": 35.0,
            "event_distance_max": 65.0,
            "trigger_distance_min": 8.0,
            "trigger_distance_max": 14.0,
            "reaction_time": 3.0,
            "hold_seconds": 5.0,
            "event_timeout_steps": 260,
        },
        "construction_obstacle": {
            "event_distance_min": 35.0,
            "event_distance_max": 80.0,
            "event_timeout_steps": 800,
        },
    }

    def __init__(self, env: Any, max_active: int = 1, seed: int | None = None):
        self.env = env
        self.max_active = max(0, int(max_active))
        self.rng = random.Random(seed)
        self.events: list[dict[str, Any]] = []
        self.actor_ids: list[int] = []
        self._reset_stats()

    def _reset_stats(self):
        self.stats: dict[str, Any] = {
            "configured": [],
            "requested": 0,
            "spawned": 0,
            "skipped": 0,
            "triggered": 0,
            "completed": 0,
            "failed": 0,
            "last_type": "",
            "last_state": "idle",
            "last_reason": "",
            "last_skip_reason": "",
            "anchor_town": getattr(self.env, "town_name", None),
            "anchor_lane_id": None,
            "anchor_has_left_lane": False,
            "anchor_has_right_lane": False,
        }

    def destroy(self, do_tick: bool = False):
        ids = list(self.actor_ids)
        for event in self.events:
            ids.extend(event.get("actor_ids", []))
        if ids:
            self.env._destroy_actor_ids_batch(ids, label="long-tail actors", do_tick=do_tick)
        self.actor_ids = []
        self.events = []

    def setup(self, event_specs: list[dict[str, Any]] | None):
        self.destroy(do_tick=False)
        self._reset_stats()

        specs = self._expand_event_specs(event_specs or [])
        self.stats["configured"] = [str(spec.get("type", "")) for spec in specs]
        if self.max_active <= 0 or not specs:
            return

        candidates = []
        for spec in specs:
            probability = self._float(spec, "enabled_prob", 1.0)
            if probability >= 1.0 or self.rng.random() < max(0.0, probability):
                candidates.append(spec)
        self.rng.shuffle(candidates)

        for spec in candidates:
            if self.stats["spawned"] >= self.max_active:
                break
            self.stats["requested"] += 1
            event_type = str(spec.get("type", "")).strip().lower()
            if event_type not in self.SUPPORTED_TYPES:
                self._mark_skip(event_type, "unsupported_event_type")
                continue

            if not self._town_allowed(spec):
                self._mark_skip(event_type, "town_not_allowed")
                continue

            try:
                spawned = self._spawn_by_type(event_type, spec)
            except Exception as exc:
                self._mark_skip(event_type, f"spawn_exception:{exc}")
                spawned = False

            if not spawned and not bool(spec.get("allow_skip_if_incompatible", True)):
                print(f"Warning: long-tail event {event_type} could not be spawned")

    def apply_before_tick(self):
        for event in list(self.events):
            if event.get("state") not in {"active", "cutting"}:
                continue
            if event.get("type") in {"side_cut_in", "parking_cut_in"}:
                self._apply_merge_control(event)

    def update_after_tick(self):
        for event in list(self.events):
            state = event.get("state")
            if state in {"done", "failed"}:
                continue

            event_type = event.get("type")
            if event_type in {"side_cut_in", "parking_cut_in"}:
                self._update_merge_event(event)
            elif event_type == "vehicle_opens_door":
                self._update_open_door_event(event)
            elif event_type == "construction_obstacle":
                self._update_construction_event(event)

    def build_info(self) -> dict[str, Any]:
        active = sum(1 for event in self.events if event.get("state") not in {"done", "failed"})
        return {
            "long_tail_events_configured": list(self.stats["configured"]),
            "long_tail_events_active": active,
            "long_tail_events_triggered": self.stats["triggered"],
            "long_tail_events_completed": self.stats["completed"],
            "long_tail_events_failed": self.stats["failed"],
            "long_tail_event_requested": self.stats["requested"],
            "long_tail_event_spawned": self.stats["spawned"],
            "long_tail_event_skipped": self.stats["skipped"],
            "long_tail_skip_reason": self.stats["last_skip_reason"],
            "long_tail_last_event_type": self.stats["last_type"],
            "long_tail_last_event_state": self.stats["last_state"],
            "long_tail_last_event_reason": self.stats["last_reason"],
            "long_tail_anchor_town": self.stats["anchor_town"],
            "long_tail_anchor_lane_id": self.stats["anchor_lane_id"],
            "long_tail_anchor_has_left_lane": self.stats["anchor_has_left_lane"],
            "long_tail_anchor_has_right_lane": self.stats["anchor_has_right_lane"],
        }

    def _expand_event_specs(self, raw_specs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        expanded = []
        for raw in raw_specs:
            if not isinstance(raw, dict):
                continue
            event_type = str(raw.get("type", "")).strip().lower()
            if event_type == "mixed_long_tail":
                for child_type in (
                    "construction_obstacle",
                    "side_cut_in",
                    "parking_cut_in",
                    "vehicle_opens_door",
                ):
                    child = dict(raw)
                    child["type"] = child_type
                    child.setdefault("enabled_prob", 0.25)
                    expanded.append(child)
            else:
                expanded.append(dict(raw))
        return expanded

    def _spawn_by_type(self, event_type: str, spec: dict[str, Any]) -> bool:
        if event_type == "side_cut_in":
            return self._spawn_side_cut_in(spec)
        if event_type == "parking_cut_in":
            return self._spawn_parking_cut_in(spec)
        if event_type == "vehicle_opens_door":
            return self._spawn_vehicle_opens_door(spec)
        if event_type == "construction_obstacle":
            return self._spawn_construction_obstacle(spec)
        return False

    def _spawn_side_cut_in(self, spec: dict[str, Any]) -> bool:
        for anchor in self._anchor_candidates(spec, "side_cut_in"):
            side = self._select_side(anchor, spec, "side_cut_in")
            if side is None:
                continue
            spawn_transform = self._raised_transform(side.waypoint.transform, 0.35)
            if not self._location_clear(spawn_transform.location, 8.0):
                continue
            vehicle = self._try_spawn_vehicle(spawn_transform, role_name="long_tail_side_cut_in")
            if vehicle is None:
                continue
            vehicle.apply_control(carla.VehicleControl(hand_brake=True))
            target_wp = self._advance(anchor, 22.0) or anchor
            event = self._base_event("side_cut_in", spec, anchor, [vehicle.id])
            event.update({
                "state": "waiting",
                "vehicle_id": vehicle.id,
                "side": side.side,
                "target_location": target_wp.transform.location,
                "target_lane_id": anchor.lane_id,
                "target_road_id": anchor.road_id,
                "target_speed": self._sample_speed(spec, "side_cut_in"),
                "trigger_location": anchor.transform.location,
                "trigger_distance": self._sample_trigger_distance(spec, "side_cut_in"),
                "reaction_time": self._float(spec, "reaction_time", self.DEFAULTS["side_cut_in"]["reaction_time"]),
            })
            self._register_event(event)
            return True
        self._mark_skip("side_cut_in", "no_same_direction_adjacent_lane")
        return False

    def _spawn_parking_cut_in(self, spec: dict[str, Any]) -> bool:
        for anchor in self._anchor_candidates(spec, "parking_cut_in"):
            side = self._select_side(anchor, spec, "parking_cut_in")
            if side is None:
                continue
            spawn_transform = self._raised_transform(side.waypoint.transform, 0.35)
            if not self._location_clear(spawn_transform.location, 8.0):
                continue
            vehicle = self._try_spawn_vehicle(spawn_transform, role_name="long_tail_parking_cut_in")
            if vehicle is None:
                continue
            self._move_parked_actor_to_outer_edge(vehicle, side.waypoint, side.side)
            vehicle.apply_control(carla.VehicleControl(hand_brake=True))
            target_wp = self._advance(anchor, 18.0) or anchor
            event = self._base_event("parking_cut_in", spec, anchor, [vehicle.id])
            event.update({
                "state": "waiting",
                "vehicle_id": vehicle.id,
                "side": side.side,
                "target_location": target_wp.transform.location,
                "target_lane_id": anchor.lane_id,
                "target_road_id": anchor.road_id,
                "target_speed": self._sample_speed(spec, "parking_cut_in"),
                "trigger_location": anchor.transform.location,
                "trigger_distance": self._sample_trigger_distance(spec, "parking_cut_in"),
                "reaction_time": self._float(
                    spec, "reaction_time", self.DEFAULTS["parking_cut_in"]["reaction_time"]
                ),
            })
            self._register_event(event)
            return True
        self._mark_skip("parking_cut_in", "no_side_lane")
        return False

    def _spawn_vehicle_opens_door(self, spec: dict[str, Any]) -> bool:
        for anchor in self._anchor_candidates(spec, "vehicle_opens_door"):
            side = self._select_side(anchor, spec, "vehicle_opens_door")
            if side is None:
                continue
            spawn_transform = self._raised_transform(side.waypoint.transform, 0.35)
            if not self._location_clear(spawn_transform.location, 8.0):
                continue
            vehicle = self._try_spawn_vehicle(
                spawn_transform,
                role_name="long_tail_vehicle_opens_door",
                require_dynamic_doors=True,
            )
            if vehicle is None:
                self._mark_skip("vehicle_opens_door", "no_dynamic_door_vehicle")
                return False
            self._move_door_actor_toward_ego_lane(vehicle, side.waypoint, side.side)
            vehicle.apply_control(carla.VehicleControl(hand_brake=True))
            event = self._base_event("vehicle_opens_door", spec, anchor, [vehicle.id])
            hold_steps = max(1, int(self._float(
                spec, "hold_seconds", self.DEFAULTS["vehicle_opens_door"]["hold_seconds"]
            ) / max(float(getattr(self.env, "dt", 0.1)), 1e-3)))
            event.update({
                "state": "waiting",
                "vehicle_id": vehicle.id,
                "side": side.side,
                "door": carla.VehicleDoor.FR if side.side == "left" else carla.VehicleDoor.FL,
                "trigger_location": anchor.transform.location,
                "trigger_distance": self._sample_trigger_distance(spec, "vehicle_opens_door"),
                "reaction_time": self._float(
                    spec, "reaction_time", self.DEFAULTS["vehicle_opens_door"]["reaction_time"]
                ),
                "hold_steps": hold_steps,
            })
            self._register_event(event)
            return True
        self._mark_skip("vehicle_opens_door", "no_side_lane")
        return False

    def _spawn_construction_obstacle(self, spec: dict[str, Any]) -> bool:
        for anchor in self._anchor_candidates(spec, "construction_obstacle"):
            actor_ids = self._spawn_construction_props(anchor)
            if not actor_ids:
                continue
            event = self._base_event("construction_obstacle", spec, anchor, actor_ids)
            event.update({
                "state": "active",
                "trigger_location": anchor.transform.location,
            })
            self._register_event(event)
            self.stats["triggered"] += 1
            self._set_last(event, "active", "spawned")
            return True
        self._mark_skip("construction_obstacle", "no_clear_route_anchor")
        return False

    def _base_event(
        self,
        event_type: str,
        spec: dict[str, Any],
        anchor: carla.Waypoint,
        actor_ids: list[int],
    ) -> dict[str, Any]:
        timeout_steps = self._int(
            spec,
            "event_timeout_steps",
            int(self.DEFAULTS[event_type]["event_timeout_steps"]),
        )
        return {
            "type": event_type,
            "name": spec.get("name", event_type),
            "state": "waiting",
            "actor_ids": list(actor_ids),
            "anchor_location": anchor.transform.location,
            "anchor_forward": anchor.transform.get_forward_vector(),
            "anchor_lane_id": anchor.lane_id,
            "anchor_road_id": anchor.road_id,
            "spawn_step": int(getattr(self.env, "time_step", 0)),
            "timeout_step": int(getattr(self.env, "time_step", 0)) + max(1, timeout_steps),
        }

    def _register_event(self, event: dict[str, Any]):
        self.events.append(event)
        self.actor_ids.extend(event.get("actor_ids", []))
        self.stats["spawned"] += 1
        self._set_last(event, event.get("state", "waiting"), "spawned")

    def _update_merge_event(self, event: dict[str, Any]):
        vehicle = self._event_vehicle(event)
        if vehicle is None:
            self._fail_event(event, "vehicle_missing")
            return

        if event["state"] == "waiting" and self._should_trigger(event):
            event["state"] = "cutting"
            event["trigger_step"] = int(getattr(self.env, "time_step", 0))
            self.stats["triggered"] += 1
            self._set_last(event, "triggered", "trigger_condition_met")
            try:
                vehicle.apply_control(carla.VehicleControl(hand_brake=False))
            except RuntimeError:
                pass

        if event["state"] != "cutting":
            return

        current_step = int(getattr(self.env, "time_step", 0))
        if current_step >= event["timeout_step"]:
            self._fail_event(event, "event_timeout")
            return

        waypoint = self.env.world.get_map().get_waypoint(
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

    def _update_open_door_event(self, event: dict[str, Any]):
        vehicle = self._event_vehicle(event)
        if vehicle is None:
            self._fail_event(event, "vehicle_missing")
            return

        if event["state"] == "waiting" and self._should_trigger(event):
            try:
                vehicle.open_door(event["door"])
            except RuntimeError as exc:
                self._fail_event(event, f"open_door_failed:{exc}")
                return
            event["state"] = "active"
            event["trigger_step"] = int(getattr(self.env, "time_step", 0))
            self.stats["triggered"] += 1
            self._set_last(event, "triggered", "door_opened")

        if event["state"] != "active":
            return

        current_step = int(getattr(self.env, "time_step", 0))
        if current_step >= event["timeout_step"]:
            self._fail_event(event, "event_timeout")
            return
        if current_step - event.get("trigger_step", current_step) >= event["hold_steps"]:
            self._complete_event(event, "door_hold_complete")
            return

        if self._ego_passed_anchor(event, margin=8.0):
            self._complete_event(event, "ego_passed_open_door")

    def _update_construction_event(self, event: dict[str, Any]):
        current_step = int(getattr(self.env, "time_step", 0))
        if current_step >= event["timeout_step"]:
            self._fail_event(event, "event_timeout")
            return
        if self._ego_passed_anchor(event, margin=15.0):
            self._complete_event(event, "ego_passed_obstacle")

    def _apply_merge_control(self, event: dict[str, Any]):
        vehicle = self._event_vehicle(event)
        if vehicle is None:
            return
        target = event["target_location"]
        transform = vehicle.get_transform()
        location = transform.location
        dx = target.x - location.x
        dy = target.y - location.y
        distance = max(0.1, math.hypot(dx, dy))
        desired_yaw = math.degrees(math.atan2(dy, dx))
        yaw_error = self._yaw_diff_signed(desired_yaw, transform.rotation.yaw)
        speed = self._actor_speed(vehicle)
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
            vehicle.apply_control(carla.VehicleControl(throttle=throttle, steer=steer, brake=brake))
        except RuntimeError as exc:
            self._fail_event(event, f"control_failed:{exc}")

    def _complete_event(self, event: dict[str, Any], reason: str):
        if event.get("state") in {"done", "failed"}:
            return
        event["state"] = "done"
        self.stats["completed"] += 1
        self._set_last(event, "done", reason)
        vehicle = self._event_vehicle(event)
        if vehicle is not None and event.get("type") in {"side_cut_in", "parking_cut_in"}:
            try:
                vehicle.set_autopilot(True)
            except RuntimeError:
                pass

    def _fail_event(self, event: dict[str, Any], reason: str):
        if event.get("state") in {"done", "failed"}:
            return
        event["state"] = "failed"
        self.stats["failed"] += 1
        self._set_last(event, "failed", reason)

    def _mark_skip(self, event_type: str, reason: str):
        self.stats["skipped"] += 1
        self.stats["last_type"] = event_type
        self.stats["last_state"] = "skipped"
        self.stats["last_reason"] = reason
        self.stats["last_skip_reason"] = reason

    def _set_last(self, event: dict[str, Any], state: str, reason: str):
        self.stats["last_type"] = str(event.get("type", ""))
        self.stats["last_state"] = state
        self.stats["last_reason"] = reason

    def _anchor_candidates(self, spec: dict[str, Any], event_type: str) -> list[carla.Waypoint]:
        defaults = self.DEFAULTS[event_type]
        distance_min = max(0.0, self._float(spec, "event_distance_min", defaults["event_distance_min"]))
        distance_max = max(distance_min, self._float(spec, "event_distance_max", defaults["event_distance_max"]))
        route = list(getattr(self.env, "global_route", []) or [])
        ego = getattr(self.env, "ego", None)
        if ego is None:
            return []

        candidates: list[carla.Waypoint] = []
        world_map = self.env.world.get_map()
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
        direction = str(spec.get("direction", "auto")).strip().lower()
        sides = ["left", "right"] if direction in {"auto", "random", ""} else [direction]
        if direction in {"auto", "random", ""}:
            self.rng.shuffle(sides)

        for side in sides:
            if side not in {"left", "right"}:
                continue
            side_wp = anchor.get_left_lane() if side == "left" else anchor.get_right_lane()
            if side_wp is None:
                continue
            if event_type == "side_cut_in":
                if not self._lane_type_matches(side_wp, {carla.LaneType.Driving}):
                    continue
            else:
                allowed = {carla.LaneType.Driving}
                if hasattr(carla.LaneType, "Parking"):
                    allowed.add(carla.LaneType.Parking)
                if not self._lane_type_matches(side_wp, allowed):
                    continue
            if self._yaw_diff_abs(anchor.transform.rotation.yaw, side_wp.transform.rotation.yaw) > 35.0:
                continue
            return SideSelection(side=side, waypoint=side_wp)
        return None

    def _spawn_construction_props(self, anchor: carla.Waypoint) -> list[int]:
        library = self.env.world.get_blueprint_library()
        cone_bp = self._find_blueprint(library, "static.prop.constructioncone")
        warning_bp = self._find_blueprint(library, "static.prop.trafficwarning")
        debris_bp = self._find_blueprint(library, "static.prop.dirtdebris02")
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
            if not self._location_clear(location, 2.0):
                continue
            actor = self.env.world.try_spawn_actor(
                bp,
                carla.Transform(location=location, rotation=carla.Rotation(yaw=prop_yaw)),
            )
            if actor is None:
                continue
            try:
                actor.set_simulate_physics(False)
            except RuntimeError:
                pass
            actor_ids.append(actor.id)

        return actor_ids

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
            actor = self.env.world.try_spawn_actor(blueprint, transform)
            if actor is not None:
                return actor
        return None

    def _vehicle_blueprints(self, require_dynamic_doors: bool = False) -> list[carla.ActorBlueprint]:
        blueprints = []
        blacklist = getattr(self.env, "npc_vehicle_blacklist", set())
        for bp in self.env.world.get_blueprint_library().filter("vehicle.*"):
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
        return self.env.world.get_actor(vehicle_id)

    def _should_trigger(self, event: dict[str, Any]) -> bool:
        ego = getattr(self.env, "ego", None)
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
        ego = getattr(self.env, "ego", None)
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

    def _move_door_actor_toward_ego_lane(self, actor: carla.Actor, wp: carla.Waypoint, side: str):
        displacement = max(0.2, (float(wp.lane_width) - float(actor.bounding_box.extent.y)) / 4.0)
        vector = wp.transform.get_right_vector()
        if side == "right":
            displacement *= -1.0
        location = wp.transform.location + carla.Location(
            x=vector.x * displacement,
            y=vector.y * displacement,
            z=0.05,
        )
        actor.set_location(location)

    def _town_allowed(self, spec: dict[str, Any]) -> bool:
        allowed = spec.get("allowed_towns")
        if allowed is None:
            return True
        if isinstance(allowed, str):
            towns = {item.strip() for item in allowed.split(",") if item.strip()}
        else:
            towns = {str(item).strip() for item in allowed if str(item).strip()}
        return not towns or str(getattr(self.env, "town_name", "")) in towns

    def _location_clear(self, location: carla.Location, min_distance: float) -> bool:
        for pattern in ("vehicle.*", "walker.*"):
            for actor in self.env.world.get_actors().filter(pattern):
                ego = getattr(self.env, "ego", None)
                if ego is not None and actor.id == ego.id:
                    continue
                try:
                    if actor.get_location().distance(location) < min_distance:
                        return False
                except RuntimeError:
                    continue
        return True

    def _advance(self, waypoint: carla.Waypoint | None, distance: float) -> carla.Waypoint | None:
        if waypoint is None:
            return None
        if hasattr(self.env, "_advance_waypoint_along_lane"):
            return self.env._advance_waypoint_along_lane(waypoint, distance)
        next_wps = waypoint.next(float(distance))
        return next_wps[0] if next_wps else None

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
