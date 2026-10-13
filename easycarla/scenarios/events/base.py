"""Shared lifecycle mechanics for independent scripted event components."""
from __future__ import annotations
from typing import Any
from abc import ABC, abstractmethod
import carla
from easycarla.control.ownership import ControlConflict
from .defaults import DEFAULTS
from .geometry import EventGeometry
from .state import StateExporter

BASE_EVENT_FIELDS = (
    "type", "name", "state", "actor_ids", "anchor_location", "anchor_forward",
    "anchor_lane_id", "anchor_road_id", "spawn_step", "timeout_duration_steps",
    "timeout_step", "trigger_step",
)

class ScriptedEvent(ABC):
    """One event family's owned state; manager supplies only sampling and statistics."""
    kind: str
    EVENT_FIELDS: tuple[str, ...] = ()
    DEFAULTS = DEFAULTS

    def __init__(self, context, rng, stats, max_events=1):
        self.context = context
        self.rng = rng
        self.stats = stats
        self.max_events = max(0, int(max_events))
        self.requested_instances = 1
        self.skipped_instances = 0
        self.events = []
        self.actor_ids = []
        self.geometry = EventGeometry(context, rng, stats, self.DEFAULTS, self._own_actor)
        self.config = {}

    @property
    def remaining_capacity(self):
        return max(0, self.max_events - len(self.events))

    def _request_fallback_instances(self, count):
        desired = max(1, int(count))
        self.stats["requested"] += desired - self.requested_instances
        self.requested_instances = desired

    def _own_actor(self, actor_id):
        if actor_id not in self.actor_ids:
            self.actor_ids.append(actor_id)

    def prepare(self, spec):
        self.config = dict(spec)
        return self.spawn(spec)

    @abstractmethod
    def spawn(self, spec):
        """Create this family's owned actors; return False for incompatibility."""

    @abstractmethod
    def update(self, event):
        """Advance an existing event's state machine after a physical tick."""

    def before_tick(self):
        pass

    def after_tick(self):
        for event in list(self.events):
            if event["state"] not in {"done", "failed"}:
                self.update(event)

    def close(self):
        self.context._destroy_actor_ids_batch(self.actor_ids, label=self.kind, do_tick=False)
        self.actor_ids.clear()
        self.events.clear()

    def info(self):
        return {"type": self.kind, "active": sum(e["state"] not in {"done", "failed"} for e in self.events)}

    def export_state(self, actor_roles):
        exporter = StateExporter(actor_roles)
        return {
            "schema_version": 1, "kind": self.kind, "max_events": self.max_events,
            "config": exporter.value(self.config),
            "state": [exporter.fields(event, BASE_EVENT_FIELDS + self.EVENT_FIELDS) for event in self.events],
            "rng_state": exporter.value(self.rng.getstate()),
            "limitations": exporter.limitations,
        }

    def _base_event(
        self,
        event_type: str,
        spec: dict[str, Any],
        anchor: carla.Waypoint,
        actor_ids: list[int],
    ) -> dict[str, Any]:
        timeout_steps = self.geometry._int(
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
            "spawn_step": int(getattr(self.context, "time_step", 0)),
            "timeout_duration_steps": max(1, timeout_steps),
            "timeout_step": int(getattr(self.context, "time_step", 0)) + max(1, timeout_steps),
        }

    def _register_event(self, event: dict[str, Any]):
        if self.remaining_capacity <= 0:
            raise RuntimeError(f"{self.kind} exhausted its event capacity {self.max_events}")
        self.events.append(event)
        for actor_id in event.get("actor_ids", []):
            self._own_actor(actor_id)
        self.stats["spawned"] += 1
        self._set_last(event, event.get("state", "waiting"), "spawned")

    def _complete_event(self, event: dict[str, Any], reason: str):
        if event.get("state") in {"done", "failed"}:
            return
        event["state"] = "done"
        self.stats["completed"] += 1
        self._set_last(event, "done", reason)
        vehicle = self.geometry._event_vehicle(event)
        if vehicle is not None and event.get("type") == "parking_cut_in":
            try:
                self.context.set_autopilot(vehicle, True, source=self.kind)
            except ControlConflict:
                raise
            except RuntimeError:
                pass

    def _fail_event(self, event: dict[str, Any], reason: str):
        if event.get("state") in {"done", "failed"}:
            return
        event["state"] = "failed"
        self.stats["failed"] += 1
        self._set_last(event, "failed", reason)
        vehicle = self.geometry._event_vehicle(event)
        if vehicle is not None and vehicle.is_alive:
            self.context.set_autopilot(vehicle, False, source=self.kind)
            self.context.control(vehicle, carla.VehicleControl(brake=1.0), source=self.kind)

    def _mark_skip(self, event_type: str, reason: str):
        missing = max(0, self.requested_instances - len(self.events) - self.skipped_instances)
        self.skipped_instances += missing
        self.stats["skipped"] += missing
        self.stats["last_type"] = event_type
        self.stats["last_state"] = "skipped"
        self.stats["last_reason"] = reason
        self.stats["last_skip_reason"] = reason

    def _set_last(self, event: dict[str, Any], state: str, reason: str):
        self.stats["last_type"] = str(event.get("type", ""))
        self.stats["last_state"] = state
        self.stats["last_reason"] = reason

