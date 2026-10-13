from __future__ import annotations
import math
import random
from typing import Any
import carla
from easycarla.control.ownership import ControlConflict
from .types import SideSelection
from .base import ScriptedEvent

class DoorEvent(ScriptedEvent):
    kind = 'vehicle_opens_door'
    EVENT_FIELDS = ('vehicle_id', 'side', 'door', 'trigger_location', 'trigger_distance', 'reaction_time', 'hold_steps')

    def spawn(self, spec: dict[str, Any]) -> bool:
        for anchor in self.geometry._anchor_candidates(spec, "vehicle_opens_door"):
            side = self.geometry._select_side(anchor, spec, "vehicle_opens_door")
            if side is None:
                continue
            spawn_transform = self.geometry._raised_transform(side.waypoint.transform, 0.35)
            if not self.geometry._location_clear(spawn_transform.location, 8.0):
                continue
            vehicle = self.geometry._try_spawn_vehicle(
                spawn_transform,
                role_name="long_tail_vehicle_opens_door",
                require_dynamic_doors=True,
            )
            if vehicle is None:
                self._mark_skip("vehicle_opens_door", "no_dynamic_door_vehicle")
                return False
            self._move_door_actor_toward_ego_lane(vehicle, side.waypoint, side.side)
            self.context.control(vehicle, carla.VehicleControl(hand_brake=True), source=self.kind)
            event = self._base_event("vehicle_opens_door", spec, anchor, [vehicle.id])
            hold_steps = max(1, int(self.geometry._float(
                spec, "hold_seconds", self.DEFAULTS["vehicle_opens_door"]["hold_seconds"]
            ) / max(float(getattr(self.context, "dt", 0.1)), 1e-3)))
            event.update({
                "state": "waiting",
                "vehicle_id": vehicle.id,
                "side": side.side,
                "door": carla.VehicleDoor.FR if side.side == "left" else carla.VehicleDoor.FL,
                "trigger_location": anchor.transform.location,
                "trigger_distance": self.geometry._sample_trigger_distance(spec, "vehicle_opens_door"),
                "reaction_time": self.geometry._float(
                    spec, "reaction_time", self.DEFAULTS["vehicle_opens_door"]["reaction_time"]
                ),
                "hold_steps": hold_steps,
            })
            self._register_event(event)
            return True
        self._mark_skip("vehicle_opens_door", "no_side_lane")
        return False


    def update(self, event: dict[str, Any]):
        vehicle = self.geometry._event_vehicle(event)
        if vehicle is None:
            self._fail_event(event, "vehicle_missing")
            return

        if event["state"] == "waiting" and self.geometry._should_trigger(event):
            try:
                vehicle.open_door(event["door"])
            except ControlConflict:
                raise
            except RuntimeError as exc:
                self._fail_event(event, f"open_door_failed:{exc}")
                return
            current_step = int(getattr(self.context, "time_step", 0))
            event["state"] = "active"
            event["trigger_step"] = current_step
            event["timeout_step"] = current_step + int(event.get("timeout_duration_steps", 1))
            self.stats["triggered"] += 1
            self._set_last(event, "triggered", "door_opened")

        if event["state"] != "active":
            return

        current_step = int(getattr(self.context, "time_step", 0))
        if current_step >= event["timeout_step"]:
            self._fail_event(event, "event_timeout")
            return
        if current_step - event.get("trigger_step", current_step) >= event["hold_steps"]:
            self._complete_event(event, "door_hold_complete")
            return

        if self.geometry._ego_passed_anchor(event, margin=8.0):
            self._complete_event(event, "ego_passed_open_door")


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


