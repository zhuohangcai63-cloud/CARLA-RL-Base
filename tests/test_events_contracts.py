"""Event lifecycle and replay contracts without a running simulator."""
from dataclasses import FrozenInstanceError
import json
import random
from types import SimpleNamespace

import pytest
carla = pytest.importorskip("carla")

from easycarla.scenarios.context import EventContext
from easycarla.scenarios.events.base import ScriptedEvent
from easycarla.scenarios.events.construction import ConstructionEvent
from easycarla.scenarios.events.crossing import CrossingEvent
from easycarla.scenarios.events.door import DoorEvent
from easycarla.scenarios.events.lead_brake import LeadBrakeEvent
from easycarla.scenarios.events.manager import LongTailEventManager
from easycarla.scenarios.events.parking import ParkingEvent
from easycarla.scenarios.events.state import StateExporter


class Actor:
    def __init__(self, actor_id=10):
        self.id = actor_id
        self.is_alive = True
        self.location = carla.Location()
        self.control = carla.VehicleControl()
        self.opened = []

    def get_location(self): return self.location
    def get_transform(self): return carla.Transform(self.location)
    def get_velocity(self): return carla.Vector3D(x=5)
    def get_control(self): return self.control
    def open_door(self, door): self.opened.append(door)


class Context:
    def __init__(self):
        self.ego = Actor(1)
        self.actor = Actor(10)
        self.world = SimpleNamespace(get_actor=lambda actor_id: self.actor if actor_id == 10 else None,
                                     get_map=lambda: SimpleNamespace(get_waypoint=lambda *a, **k: None))
        self.time_step = 0
        self.dt = .05
        self.tm_port = 8000
        self.town_name = "Town10HD"
        self.commands = []
        self.destroyed = []

    def control(self, actor, control, *, source):
        self.commands.append((actor.id, source, "control", control))

    def set_autopilot(self, actor, enabled, *, source):
        self.commands.append((actor.id, source, "autopilot", enabled))

    def _destroy_actor_ids_batch(self, ids, **kwargs):
        assert not kwargs.get("do_tick")
        self.destroyed.extend(ids)

    _actor_speed = staticmethod(EventContext._actor_speed)
    _world_to_ego_local = EventContext._world_to_ego_local


def component(cls, ctx=None):
    ctx = ctx or Context()
    manager = LongTailEventManager(ctx)
    return cls(ctx, random.Random(7), manager.stats)


def record(kind, state="waiting"):
    return {"type": kind, "name": kind, "state": state, "actor_ids": [10], "vehicle_id": 10,
            "anchor_location": carla.Location(), "anchor_forward": carla.Vector3D(x=1),
            "timeout_step": 20, "timeout_duration_steps": 20, "spawn_step": 0,
            "trigger_location": carla.Location(), "trigger_distance": 5., "reaction_time": 1.,
            "target_location": carla.Location(x=50), "target_road_id": 1, "target_lane_id": -1,
            "target_speed": 6., "door": carla.VehicleDoor.FL, "hold_steps": 2}


def test_manager_composes_independent_components_and_preserves_order(monkeypatch):
    visits = []
    class Event(ScriptedEvent):
        kind = "test"
        def spawn(self, spec):
            self._register_event({"type": self.kind, "state": "waiting", "name": spec["name"], "actor_ids": []})
            return True
        def before_tick(self): visits.append((self.config["name"], "before"))
        def update(self, event): visits.append((self.config["name"], "after"))
    monkeypatch.setattr(LongTailEventManager, "COMPONENTS", {"test": Event})
    manager = LongTailEventManager(Context(), max_active=2, seed=7)
    manager.prepare([{"type": "test", "name": "one"}, {"type": "test", "name": "two"}])
    assert LongTailEventManager.__bases__ == (object,)
    assert len(manager.components) == 2
    assert manager.components[0].events is not manager.components[1].events
    manager.before_tick(); manager.after_tick()
    order = [event["name"] for event in manager.events]
    assert visits == [(name, phase) for phase in ("before", "after") for name in order]
    manager.close(); manager.close()
    assert not manager.components


@pytest.mark.parametrize("spec,reason", [
    ({"type": "unregistered"}, "unsupported_event_type"),
    ({"type": "parking_cut_in", "allowed_towns": ["TownOther"]}, "town_not_allowed"),
])
def test_required_incompatible_event_fails_explicitly(spec, reason):
    manager = LongTailEventManager(Context())
    with pytest.raises(RuntimeError, match=reason):
        manager.prepare([{**spec, "allow_skip_if_incompatible": False}])
    assert manager.info()["long_tail_event_skipped"] == 1


def test_optional_geometric_incompatibility_records_skip():
    manager = LongTailEventManager(Context())
    manager.prepare([{"type": "vehicle_opens_door", "allowed_towns": ["Other"]}])
    assert manager.info()["long_tail_skip_reason"] == "town_not_allowed"


def test_unexpected_spawn_error_is_not_silently_skipped(monkeypatch):
    ctx = Context()
    class Broken(ScriptedEvent):
        kind = "broken"
        def update(self, event): pass
        def spawn(self, spec):
            self._own_actor(10)
            raise ConnectionError("CARLA connection lost")
    monkeypatch.setattr(LongTailEventManager, "COMPONENTS", {"broken": Broken})
    manager = LongTailEventManager(ctx)
    with pytest.raises(ConnectionError): manager.prepare([{"type": "broken"}])
    assert ctx.destroyed == [10]
    assert manager.stats["skipped"] == 0


def test_door_lifecycle_trigger_hold_complete_and_deduplication():
    event = component(DoorEvent)
    event._register_event(record(event.kind))
    event.after_tick()
    assert event.events[0]["state"] == "active"
    assert event.context.actor.opened == [carla.VehicleDoor.FL]
    event.context.time_step = 2
    event.after_tick(); event.after_tick()
    assert event.events[0]["state"] == "done"
    assert event.stats["triggered"] == event.stats["completed"] == 1


def test_parking_control_is_routed_through_context_and_timeout_stops_actor():
    event = component(ParkingEvent)
    event._register_event(record(event.kind))
    event.after_tick(); event.before_tick()
    assert event.events[0]["state"] == "cutting"
    controls = [cmd for cmd in event.context.commands if cmd[2] == "control"]
    assert controls and all(cmd[1] == "parking_cut_in" for cmd in controls)
    event.context.time_step = 20
    event.after_tick(); event.after_tick()
    assert event.events[0]["state"] == "failed"
    assert event.stats["failed"] == 1
    assert event.context.commands[-1][3].brake == 1


def test_construction_pass_completion_has_no_vehicle_controls():
    event = component(ConstructionEvent)
    obstacle = record(event.kind, "active")
    obstacle.pop("vehicle_id")
    event._register_event(obstacle)
    event.context.ego.location = carla.Location(x=16)
    event.after_tick(); event.after_tick()
    assert event.events[0]["state"] == "done"
    assert event.stats["completed"] == 1
    assert not event.context.commands


def test_component_export_has_stable_roles_and_ignores_incidental_cache():
    event = component(DoorEvent)
    event._register_event(record(event.kind))
    first = event.export_state({10: "scenic/door/1"})
    event.debug_cache = object()
    event.events[0]["temporary_cache"] = object()
    assert first == event.export_state({10: "scenic/door/1"})
    assert first["state"][0]["vehicle_id"] == {"role": "scenic/door/1"}
    assert first["schema_version"] == 1
    json.dumps(first, allow_nan=False)
    unresolved = event.export_state({})
    assert unresolved["limitations"]
    assert unresolved["state"][0]["vehicle_id"]["unresolved"]


def test_crossing_timing_is_seeded_and_exports_explicit_config():
    ctx = Context()
    event = CrossingEvent(ctx, {"forced_crossing_trigger_prob": .36}, 17)
    assert event.config.forced_crossing_trigger_prob == pytest.approx(.2)
    assert event.config.forced_crossing_warmup_steps == 100
    assert event.export_state({}) == CrossingEvent(ctx, {"forced_crossing_trigger_prob": .36}, 17).export_state({})
    with pytest.raises(FrozenInstanceError): event.config.forced_crossing_walkers = 5
    with pytest.raises(AttributeError): _ = event.ego
    event.temporary_cache = object()
    json.dumps(event.export_state({}), allow_nan=False)


def test_crossing_completion_and_cleanup_use_owned_walker_only():
    ctx = Context()
    event = CrossingEvent(ctx, {}, 7)
    event.active_forced_crossing_events[0] = {"event_id": 0, "walker_id": 10,
        "spawn_location": carla.Location(), "target_location": carla.Location(x=10),
        "direction": [1., 0.], "speed": 1.5, "path_length": 10., "completion_margin": .5,
        "timeout_step": 50}
    event.forced_crossing_walker_ids = [10]
    event.before_tick()
    assert ctx.commands[-1][:3] == (10, "crossing", "control")
    ctx.actor.location = carla.Location(x=10)
    event.after_tick(); event.after_tick()
    assert event.state.forced_crossing_completed == 1
    assert ctx.destroyed == [10]
    event.close(); event.close()
    assert ctx.destroyed == [10]


def test_lead_brake_switches_ownership_and_completes_once(monkeypatch):
    ctx = Context()
    event = LeadBrakeEvent(ctx, {"lead_brake_enable_prob": 1., "lead_brake_warmup_steps": 0,
        "lead_track_min_steps": 1, "lead_brake_hold_steps": 1}, 5, [ctx.actor])
    metrics = {"vehicle_id": 10, "distance": 12., "relative_speed": 1.}
    monkeypatch.setattr(event, "_find_lead_brake_candidate", lambda: metrics)
    monkeypatch.setattr(event, "_build_lead_brake_candidate_metrics", lambda *args, **kwargs: metrics)
    event.before_tick(); event.after_tick()
    assert event.active_lead_brake_event["state"] == "braking"
    assert (10, "lead_brake", "autopilot", False) in ctx.commands
    exported = event.export_state({10: "traffic/1"})
    assert exported["state"]["event"]["vehicle_id"] == {"role": "traffic/1"}
    assert exported["state"]["lead_brake_last_candidate_id"] == {"role": "traffic/1"}
    json.dumps(exported, allow_nan=False)
    ctx.time_step = 2
    event.after_tick(); event.after_tick()
    assert event.state.lead_brake_completed == 1
    assert ctx.commands[-1] == (10, "lead_brake", "autopilot", True)


def test_context_handoff_is_explicit_and_stable():
    calls = []
    controls = SimpleNamespace(owner=lambda actor_id: "scenic/background", handoff=lambda *args, **kwargs: calls.append(("handoff", args, kwargs)),
        set_autopilot=lambda *args, **kwargs: calls.append(("autopilot", args, kwargs)),
        apply_control=lambda *args, **kwargs: calls.append(("control", args, kwargs)))
    session = SimpleNamespace(map=SimpleNamespace(name="Town10HD"), sim={"physics_dt": .05, "tm_port": 8000}, controls=controls)
    ctx = EventContext(session, Actor(1), [])
    actor = Actor(10)
    ctx.set_autopilot(actor, False, source="lead_brake")
    ctx.control(actor, carla.VehicleControl(brake=1), source="lead_brake")
    assert [c[0] for c in calls] == ["handoff", "autopilot", "control"]
    assert calls[0][2] == {"expected_owner": "scenic/background", "mode": "manual"}
    assert calls[-1][2]["owner"] == "scenic/event/lead_brake"


def test_state_export_rejects_unsupported_objects_and_nonfinite_numbers():
    exporter = StateExporter({})
    with pytest.raises(TypeError): exporter.value(object())
    with pytest.raises(ValueError): exporter.value(float("nan"))


def test_all_prepared_families_share_explicit_runtime_contract():
    from easycarla.scenarios.events.types import EventLifecycle
    ctx = Context()
    for event in (component(ParkingEvent, ctx), component(DoorEvent, ctx), component(ConstructionEvent, ctx),
                  CrossingEvent(ctx, {}, 2), LeadBrakeEvent(ctx, {}, 2, []), LongTailEventManager(ctx)):
        assert isinstance(event, EventLifecycle)


def test_control_conflict_is_never_reported_as_event_failure(monkeypatch):
    from easycarla.control.ownership import ControlConflict
    event = component(ParkingEvent)
    event._register_event(record(event.kind, "cutting"))
    def conflict(*args, **kwargs): raise ControlConflict("another source owns actor")
    monkeypatch.setattr(event.context, "control", conflict)
    with pytest.raises(ControlConflict): event.before_tick()
    assert event.stats["failed"] == 0


@pytest.mark.parametrize("kind", ["parking", "construction"])
def test_global_fallback_respects_event_capacity_not_actor_count(monkeypatch, kind):
    ctx = Context()
    event = component(ParkingEvent if kind == "parking" else ConstructionEvent, ctx)
    assert event.remaining_capacity == 1
    if kind == "parking":
        monkeypatch.setattr(event, "_global_parking_cut_in_candidates", lambda spec: [(None, None)] * 5)
        monkeypatch.setattr(event.geometry, "_selection_spawn_transform", lambda *args: SimpleNamespace(location=None))
        monkeypatch.setattr(event.geometry, "_location_clear", lambda *args: True)
        monkeypatch.setattr(event.geometry, "_try_spawn_vehicle", lambda *args, **kwargs: ctx.actor)
        def register(actor, *args):
            event._register_event({"type": event.kind, "actor_ids": [actor.id], "state": "waiting"})
        monkeypatch.setattr(event, "_register_parking_cut_in_event", register)
        result = event._spawn_global_parking_cut_in_fallback({"global_parking_fallback": True, "global_parking_fallback_count": 5})
    else:
        monkeypatch.setattr(event, "_global_construction_candidates", lambda spec: [None] * 5)
        monkeypatch.setattr(event, "_spawn_construction_props", lambda anchor: [10, 11, 12])
        def register(anchor, actor_ids, *args):
            event._register_event({"type": event.kind, "actor_ids": actor_ids, "state": "active"})
        monkeypatch.setattr(event, "_register_construction_event", register)
        result = event._spawn_global_construction_fallback({"global_construction_fallback": True, "global_construction_fallback_count": 5})
    assert result
    assert len(event.events) == event.stats["spawned"] == 1
    assert len(event.actor_ids) == (1 if kind == "parking" else 3)
    assert event.remaining_capacity == 0
    with pytest.raises(RuntimeError, match="exhausted"):
        event._register_event({"type": event.kind, "actor_ids": [99], "state": "active"})


def test_manager_passes_remaining_budget_to_each_component(monkeypatch):
    capacities = []
    class Event(ScriptedEvent):
        kind = "test"
        def spawn(self, spec):
            capacities.append(self.remaining_capacity)
            self._register_event({"type": self.kind, "state": "waiting", "actor_ids": []})
            return True
        def update(self, event): pass
    monkeypatch.setattr(LongTailEventManager, "COMPONENTS", {"test": Event})
    manager = LongTailEventManager(Context(), max_active=2, seed=1)
    manager.prepare([{"type": "test"}] * 4)
    assert capacities == [2, 1]
    assert manager.stats["requested"] == manager.stats["spawned"] == 2


def test_partial_global_fallback_reports_requested_spawned_and_skipped_instances(monkeypatch):
    ctx = Context()
    manager = LongTailEventManager(ctx, max_active=4)
    event = ConstructionEvent(ctx, random.Random(1), manager.stats, max_events=4)
    manager.stats["requested"] = 1  # The manager initially requests one instance.
    monkeypatch.setattr(event, "_global_construction_candidates", lambda spec: [0, 1, 2, 3])
    monkeypatch.setattr(event, "_spawn_construction_props", lambda anchor: [10, 11, 12] if anchor == 0 else [])
    def register(anchor, actor_ids, *args):
        event._register_event({"type": event.kind, "actor_ids": actor_ids, "state": "active"})
    monkeypatch.setattr(event, "_register_construction_event", register)
    assert event._spawn_global_construction_fallback({"global_construction_fallback": True, "global_construction_fallback_count": 4})
    assert event.stats["requested"] == 4
    assert event.stats["spawned"] == 1
    assert event.stats["skipped"] == 3
    assert event.stats["last_skip_reason"] == "global_fallback_partial_spawn"
    assert len(event.actor_ids) == 3


def test_context_propagates_cleanup_failure_instead_of_forgetting_live_actor():
    from easycarla.errors import EnvironmentFault
    session = SimpleNamespace(map=SimpleNamespace(name="Town10HD"), sim={"physics_dt": .05, "tm_port": 8000},
                              registry=SimpleNamespace(destroy=lambda ids: [{"actor_id": 10, "error": "timeout"}]))
    ctx = EventContext(session, Actor(1), [])
    with pytest.raises(EnvironmentFault, match="Event cleanup failed"):
        ctx._destroy_actor_ids_batch([10], label="crossing")


def test_manager_cleanup_attempts_every_component_and_retains_failed_ones():
    from easycarla.errors import EnvironmentFault
    calls = []
    class Component:
        def __init__(self, kind, fail=False): self.kind, self.fail = kind, fail
        def close(self):
            calls.append(self.kind)
            if self.fail: raise RuntimeError("destroy refused")
    manager = LongTailEventManager(Context())
    failed = Component("first", True)
    manager.components = [failed, Component("second")]
    with pytest.raises(EnvironmentFault): manager.close()
    assert calls == ["first", "second"]
    assert manager.components == [failed]
    failed.fail = False
    manager.close()
    assert not manager.components
