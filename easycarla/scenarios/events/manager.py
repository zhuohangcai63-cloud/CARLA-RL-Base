"""Scenic event orchestration: selection, bounded preparation and lifecycle dispatch."""
from __future__ import annotations
import random
from typing import Any
from easycarla.errors import EnvironmentFault
from .parking import ParkingEvent
from .door import DoorEvent
from .construction import ConstructionEvent
from .geometry import EventGeometry
from .defaults import DEFAULTS
from .state import StateExporter

class LongTailEventManager:
    COMPONENTS = {
        "parking_cut_in": ParkingEvent,
        "vehicle_opens_door": DoorEvent,
        "construction_obstacle": ConstructionEvent,
    }
    SUPPORTED_TYPES = frozenset(COMPONENTS)

    def __init__(self, env, max_active=1, seed=None):
        self.context = env
        self.max_active = max(0, int(max_active))
        self.rng = random.Random(seed)
        self.components = []
        self._reset_stats()

    @property
    def events(self):
        return [event for component in self.components for event in component.events]

    @property
    def actor_ids(self):
        return [actor for component in self.components for actor in component.actor_ids]

    def prepare(self, event_specs):
        self.close()
        self._reset_stats()
        geometry = EventGeometry(self.context, self.rng, self.stats, DEFAULTS)
        specs = self._expand_event_specs(event_specs or [])
        self.stats["configured"] = [str(spec.get("type", "")) for spec in specs]
        if self.max_active <= 0 or not specs:
            return
        candidates = []
        for spec in specs:
            probability = geometry._float(spec, "enabled_prob", 1.0)
            if probability >= 1.0 or self.rng.random() < max(0.0, probability):
                candidates.append(spec)
        self.rng.shuffle(candidates)
        for spec in candidates:
            if self.stats["spawned"] >= self.max_active:
                break
            self.stats["requested_specs"] += 1
            self.stats["requested"] += 1
            kind = str(spec.get("type", "")).strip().lower()
            if kind not in self.COMPONENTS:
                self._mark_skip(kind, "unsupported_event_type")
                self._require_spawned(spec, kind, False)
                continue
            if not geometry._town_allowed(spec):
                self._mark_skip(kind, "town_not_allowed")
                self._require_spawned(spec, kind, False)
                continue
            component = self.COMPONENTS[kind](self.context, self.rng, self.stats,
                                               max_events=self.max_active - self.stats["spawned"])
            self.components.append(component)
            try:
                spawned = component.prepare(spec)
            except Exception:
                component.close()
                # API/control failures are faults, not geometric incompatibility.
                # An optional event may return False, but may not hide exceptions.
                raise
            self._require_spawned(spec, kind, spawned)

    def _require_spawned(self, spec, kind, spawned):
        if not spawned and not bool(spec.get("allow_skip_if_incompatible", True)):
            raise RuntimeError(f"Required long-tail event {kind} could not spawn: {self.stats['last_skip_reason']}")

    def before_tick(self):
        for component in self.components:
            component.before_tick()

    def after_tick(self):
        for component in self.components:
            component.after_tick()

    def close(self):
        failures, remaining = [], []
        for component in self.components:
            try:
                component.close()
            except Exception as exc:
                failures.append(f"{component.kind}: {exc}")
                remaining.append(component)
        self.components = remaining
        if failures:
            raise EnvironmentFault(f"Long-tail event cleanup failed: {failures}")

    def export_state(self, actor_roles):
        exporter = StateExporter(actor_roles)
        children = [component.export_state(actor_roles) for component in self.components]
        return {"schema_version": 1, "kind": "long_tail", "max_active": self.max_active,
                "stats": exporter.value(self.stats), "components": children,
                "rng_state": exporter.value(self.rng.getstate()),
                "limitations": sorted({reason for child in children for reason in child["limitations"]})}

    # Temporary method aliases preserve callers while holding one implementation.
    setup = prepare
    apply_before_tick = before_tick
    update_after_tick = after_tick

    def destroy(self, do_tick=False):
        if do_tick:
            raise RuntimeError("Event cleanup must not tick")
        self.close()

    def _reset_stats(self):
        self.stats: dict[str, Any] = {
            "configured": [],
            "requested": 0,
            "requested_specs": 0,
            "spawned": 0,
            "skipped": 0,
            "triggered": 0,
            "completed": 0,
            "failed": 0,
            "last_type": "",
            "last_state": "idle",
            "last_reason": "",
            "last_skip_reason": "",
            "anchor_town": getattr(self.context, "town_name", None),
            "anchor_lane_id": None,
            "anchor_has_left_lane": False,
            "anchor_has_right_lane": False,
        }

    def info(self) -> dict[str, Any]:
        active = sum(1 for event in self.events if event.get("state") not in {"done", "failed"})
        return {
            "long_tail_events_configured": list(self.stats["configured"]),
            "long_tail_events_active": active,
            "long_tail_events_triggered": self.stats["triggered"],
            "long_tail_events_completed": self.stats["completed"],
            "long_tail_events_failed": self.stats["failed"],
            "long_tail_event_requested": self.stats["requested"],
            "long_tail_specs_requested": self.stats["requested_specs"],
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

    def _mark_skip(self, event_type: str, reason: str):
        self.stats["skipped"] += 1
        self.stats["last_type"] = event_type
        self.stats["last_state"] = "skipped"
        self.stats["last_reason"] = reason
        self.stats["last_skip_reason"] = reason

    build_info = info
