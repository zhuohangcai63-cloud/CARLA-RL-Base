"""Source integration checks that do not require a running CARLA server."""
import ast
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from easycarla.scenarios.bench2drive.adapter import Bench2DriveAdapter, ClientBridge, WorldBridge
from easycarla.scenarios.bench2drive.native_control import control_scope, native_actor_call
from easycarla.scenarios.bench2drive.statistics import event_data
from easycarla.scenarios.manifests import (
    assert_evaluation_isolation, audit_overlap, load_manifest, route_content, select_entry, verify_entry,
)

ROOT = Path(__file__).resolve().parents[1]
MANIFESTS = ROOT / "configs/manifests"


def manifest(name):
    return load_manifest(MANIFESTS / f"{name}.json")


def test_explicit_split_and_selection():
    with pytest.raises(ValueError, match="explicit manifest"):
        Bench2DriveAdapter({})
    with pytest.raises(ValueError, match="purpose"):
        Bench2DriveAdapter({"manifest": str(MANIFESTS / "v004_eval220.json"), "purpose": "train"})
    data = manifest("town12_long_train78")
    assert len(data["entries"]) == 78
    assert select_entry(data, 9, {}) == select_entry(data, 9, {})
    with pytest.raises(ValueError, match="absent"):
        select_entry(data, 0, {"route_id": "26537"})
    with pytest.raises(ValueError, match="outside"):
        select_entry(data, 0, {"scenario_index": -1})


def test_reserved_evaluation_isolation_and_honest_dev_split():
    train = manifest("town12_long_train78")
    dev = manifest("town12_long_dev12")
    assert len(dev["entries"]) == 12
    report = audit_overlap(train, dev)
    assert report["exact_route_overlap"] == 0
    assert report["exact_trigger_overlap"] == 0
    assert_evaluation_isolation(train, dev)
    with pytest.raises(ValueError, match="overlap"):
        assert_evaluation_isolation(train, train)
    for name in ("v004_eval220", "legacy_eval220", "legacy_dev10_reserved"):
        assert assert_evaluation_isolation(train, manifest(name))["exact_trigger_overlap"] == 0
    reserved = audit_overlap(manifest("legacy_eval220"), manifest("legacy_dev10_reserved"))
    assert reserved["exact_route_overlap"] == 10


def test_fingerprints_ignore_names_but_include_geometry():
    from xml.etree.ElementTree import fromstring
    xml = '<route town="Town01" id="1"><waypoints><position x="0" y="1" z="2"/></waypoints><scenarios><scenario type="ParkingCutIn" name="a"><trigger_point x="1" y="2" z="3" yaw="90"/></scenario></scenarios></route>'
    one = fromstring(xml)
    two = fromstring(xml.replace('id="1"', 'id="999"').replace('name="a"', 'name="b"'))
    assert route_content(one) == route_content(two)
    two.find("./scenarios/scenario/trigger_point").set("x", "4")
    assert route_content(one)["trigger_fingerprints"] != route_content(two)["trigger_fingerprints"]


def test_runtime_hidden_ticks_do_not_advance_world():
    snapshot = SimpleNamespace(frame=99)
    calls = []
    episode = SimpleNamespace(initializing=False, deferred_tick_requests=0, provider=None,
                              session=SimpleNamespace(world=SimpleNamespace(get_snapshot=lambda: snapshot),
                                                      tick=lambda **kw: calls.append(kw) or snapshot))
    bridge = WorldBridge(episode)
    assert bridge.tick() == 99
    assert bridge.wait_for_tick() is snapshot
    assert not calls
    assert episode.deferred_tick_requests == 2
    episode.initializing = True
    assert bridge.tick() == 99
    assert calls == [{"phase": "initialization"}]


def test_scenario_progress_retains_actual_completion_after_tree_invalidation(monkeypatch):
    py_trees = pytest.importorskip("py_trees")
    from easycarla.scenarios.bench2drive.adapter import Bench2DriveEpisode
    from easycarla.scenarios.bench2drive import progress
    observed = {"activation_observed": True, "completion_observed": True}
    monkeypatch.setattr(progress, "scenario_progress", lambda scenario: dict(observed))
    scenario = SimpleNamespace(config=SimpleNamespace(name="cut_in", type="ParkingCutIn"),
                               behavior_tree=SimpleNamespace(status=py_trees.common.Status.RUNNING))
    episode = Bench2DriveEpisode.__new__(Bench2DriveEpisode)
    episode.scene = SimpleNamespace(get_criteria=lambda: [], timeout_node=None,
        scenario_tree=SimpleNamespace(status=py_trees.common.Status.RUNNING),
        list_scenarios=[scenario], missing_scenario_configurations=[])
    episode._scenario_progress, episode.state = {}, {"suppressed": {}}
    episode.deferred_tick_requests = episode.deferred_batch_ticks = 0
    episode.statistics = None
    assert episode.info()["native_scenarios"][0]["completion_observed"] is True
    observed.update(activation_observed=False, completion_observed=False)
    info = episode.info()
    assert info["native_scenario_progress_schema"] == 1
    assert info["native_scenarios"][0]["activation_observed"] is True
    assert info["native_scenarios"][0]["completion_observed"] is True


def test_batch_never_forwards_tick_cue():
    calls = []
    episode = SimpleNamespace(session=SimpleNamespace(client=SimpleNamespace(
        apply_batch_sync=lambda batch, tick: calls.append((batch, tick)) or [])), deferred_batch_ticks=0)
    command = type("SpawnActor", (), {})()
    ClientBridge(episode).apply_batch_sync([command], True)
    assert calls == [([command], False)]
    assert episode.deferred_batch_ticks == 1


def test_spawn_roles_are_unique_stable_and_repeated_registration_is_idempotent():
    registered = {}
    def register(actor, role):
        registered[actor.id] = role
    episode = SimpleNamespace(owned_ids=set(), semantic_roles={}, spawn_count=0,
                              session=SimpleNamespace(registry=SimpleNamespace(register=register),
                                                      controls=SimpleNamespace(claim=lambda *a, **kw: None)))
    bridge = WorldBridge(episode)
    one = SimpleNamespace(id=100, type_id="vehicle.test", attributes={"role_name": "background"})
    two = SimpleNamespace(id=101, type_id="vehicle.test", attributes={"role_name": "background"})
    bridge._register(one)
    bridge._register(two)
    first_role = registered[one.id]
    bridge._register(one)
    assert registered[one.id] == first_role
    assert registered[one.id] != registered[two.id]
    assert episode.spawn_count == 2


def test_native_ego_transform_is_captured_and_npc_ownership_is_exclusive():
    calls = []
    ego = SimpleNamespace(id=1, apply_control=lambda c: calls.append((1, c)))
    npc = SimpleNamespace(id=2, apply_control=lambda c: calls.append((2, c)),
                          set_autopilot=lambda value: calls.append((2, value)))
    state = {"ego_id": 1, "leased": {2}, "suppressed": {}, "capture_ego": True, "world": object()}
    with control_scope(state):
        native_actor_call(ego, "apply_control", "noisy control")
        native_actor_call(npc, "apply_control", "native control")
        native_actor_call(npc, "set_autopilot", True)
        assert native_actor_call(ego, "get_world") is state["world"]
    assert calls == []
    assert state["ego_control"] == "noisy control"
    assert state["suppressed"] == {2: 2}
    # Outside upstream source, the actual controller still submits one action.
    npc.apply_control("research control")
    assert calls == [(2, "research control")]


def test_native_destruction_retires_ownership_even_for_taken_over_actor():
    forgotten = []
    actor = SimpleNamespace(id=2, destroy=lambda: True)
    state = {"ego_id": 1, "leased": {2}, "suppressed": {}, "capture_ego": False,
             "world": SimpleNamespace(forget=forgotten.append)}
    with control_scope(state):
        assert native_actor_call(actor, "destroy") is True
    assert forgotten == [2]
    assert state["suppressed"] == {}


def test_native_collision_events_export_facts_without_carla_handles():
    payload = {"location": SimpleNamespace(x=1, y=-2, z=0.5),
               "other_actor": SimpleNamespace(id=42, type_id="vehicle.test")}
    converted = event_data(payload)
    assert converted == {"location": {"x": 1.0, "y": -2.0, "z": 0.5},
                         "other_actor": {"actor_id": 42, "type_id": "vehicle.test"}}
    assert json.loads(json.dumps(converted)) == converted


def test_compatibility_patch_preserves_future_imports_and_routes_control_calls():
    spec = importlib.util.spec_from_file_location("prepare", ROOT / "scripts/prepare_bench2drive.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    source = '''"""模块 docstring"""\nfrom __future__ import annotations\ndef update(self):\n    self.actor.apply_control(control)\n    self.actor.set_autopilot(True, 8000)\n    return self.actor.get_world()\n'''
    patched, count = module.patch_controls(source)
    assert count == 3
    ast.parse(patched)
    assert "_easycarla_native_call(self.actor, 'apply_control', control)" in patched
    assert patched.index("from __future__") < patched.index("from easycarla")


def test_verify_entry_rejects_path_escape_and_changed_xml(tmp_path):
    entry = manifest("v004_eval220")["entries"][0]
    with pytest.raises(ValueError, match="belong"):
        verify_entry({**entry, "file": "../outside.xml"}, tmp_path)
    path = tmp_path / entry["file"]
    path.parent.mkdir(parents=True)
    path.write_text("<changed/>")
    with pytest.raises(ValueError, match="digest changed"):
        verify_entry(entry, tmp_path)


def test_route_batch_stops_after_unknown_step_without_replay(tmp_path, monkeypatch):
    import numpy as np
    from easycarla_client.errors import UnknownOutcomeError
    spec = importlib.util.spec_from_file_location("validate_routes", ROOT / "scripts/validate_routes.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    calls = []
    class Client:
        def __init__(self, **kwargs):
            pass
        def reset(self, seed, options):
            calls.append(("reset", options["scenario_id"]))
            return {"frame": 10, "ego": {"speed": 0},
                    "route": {"points": np.array([[5, 0, 0]]), "valid": np.array([True]), "length": 100, "progress": 0}}, {
                        "scenario": {"source": "bench2drive", "scenario_id": options["scenario_id"]}}
        def capabilities(self):
            return {}
        def step(self, action):
            calls.append(("step", action))
            raise UnknownOutcomeError("service died after submission")
        def close(self):
            calls.append(("close", None))
    monkeypatch.setattr(module, "CarlaEnvClient", Client)
    args = SimpleNamespace(manifest=MANIFESTS / "v004_eval220.json", instance="test", route=[], limit=2,
                           output=tmp_path / "report.json", seed=123, steps=10, timeout=1, continue_on_error=True)
    report = module.run(args)
    assert report["status"] == "stopped"
    assert [r["execution"] for r in report["results"][:2]] == ["unknown_outcome", "not_run"]
    assert [c[0] for c in calls] == ["reset", "step", "close"]
    assert json.loads(args.output.read_text())["results"][0]["execution"] == "unknown_outcome"


def test_native_mutations_enter_common_control_gateway():
    calls = []
    class Gateway:
        def apply_control(self, *args, **kwargs): calls.append(('apply_control', args, kwargs))
        def set_autopilot(self, *args, **kwargs): calls.append(('set_autopilot', args, kwargs))
        def set_target_velocity(self, *args, **kwargs): calls.append(('set_target_velocity', args, kwargs))
        def set_target_angular_velocity(self, *args, **kwargs): calls.append(('set_target_angular_velocity', args, kwargs))
    actor = SimpleNamespace(id=10)
    state = {'leased': set(), 'suppressed': {}, 'capture_ego': False,
             'controls': Gateway(), 'owner': 'bench2drive/native'}
    with control_scope(state):
        native_actor_call(actor, 'apply_control', 'braking')
        native_actor_call(actor, 'set_autopilot', False, 8100)
        native_actor_call(actor, 'set_target_velocity', 'linear')
        native_actor_call(actor, 'set_target_angular_velocity', 'angular')
    assert [c[0] for c in calls] == ['apply_control', 'set_autopilot', 'set_target_velocity', 'set_target_angular_velocity']
    assert all(c[2]['owner'] == 'bench2drive/native' for c in calls)
    assert calls[0][2]['allow_combined'] is True
    assert calls[1][2]['port'] == 8100


def test_native_existing_control_batch_cannot_bypass_gateway():
    calls = []
    episode = SimpleNamespace(session=SimpleNamespace(client=SimpleNamespace(
        apply_batch_sync=lambda *args: calls.append(args))))
    command = type('ApplyVehicleControl', (), {})()
    with pytest.raises(RuntimeError, match='Unsupported native batch'):
        ClientBridge(episode).apply_batch_sync([command])
    assert calls == []


def test_native_constant_velocity_is_owned_and_suppressed_after_takeover():
    calls = []
    gateway = SimpleNamespace(enable_constant_velocity=lambda actor, value, **kw: calls.append(('enable', actor.id, value, kw)),
                              disable_constant_velocity=lambda actor, **kw: calls.append(('disable', actor.id, kw)))
    actor = SimpleNamespace(id=5)
    state = {'leased': set(), 'suppressed': {}, 'capture_ego': False,
             'controls': gateway, 'owner': 'bench2drive/native'}
    with control_scope(state):
        native_actor_call(actor, 'enable_constant_velocity', 'velocity')
        native_actor_call(actor, 'disable_constant_velocity')
        state['leased'].add(5)
        native_actor_call(actor, 'enable_constant_velocity', 'old native speed')
    assert [c[0] for c in calls] == ['enable', 'disable']
    assert state['suppressed'] == {5: 1}


def test_prepared_copy_routes_nested_constant_velocity_agent():
    spec = importlib.util.spec_from_file_location('prepare_native', ROOT / 'scripts/prepare_bench2drive.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.PATCH_VERSION == 6
    source = 'def update(actor):\n    actor.enable_constant_velocity(velocity)\n    actor.disable_constant_velocity()\n'
    patched, count = module.patch_controls(source)
    assert count == 2
    assert "_easycarla_native_call(actor, 'enable_constant_velocity'" in patched
    assert "_easycarla_native_call(actor, 'disable_constant_velocity'" in patched


def test_native_takeover_reports_specific_handoff_phases(monkeypatch):
    from easycarla.scenarios.bench2drive.adapter import Bench2DriveEpisode
    from easycarla.scenarios.bench2drive import progress
    monkeypatch.setattr(progress, 'scenario_progress', lambda scene: {
        'extraction': 'pinned_basic_scenario_route_wrapper_v1'})
    actor = SimpleNamespace(id=3)
    placement = type('ActorTransformSetter', (), {})()
    placement._actor, placement.status = actor, 'Status.SUCCESS'
    sync = type('SyncArrivalWithAgent', (), {})()
    sync._actor, sync.status = actor, 'Status.INVALID'
    cut_in = type('CutIn', (), {})()
    cut_in._actor, cut_in.status = actor, 'Status.INVALID'
    nodes = [placement, sync, cut_in]
    scenario = SimpleNamespace(config=SimpleNamespace(type='HighwayCutIn'),
                               behavior_tree=SimpleNamespace(iterate=lambda: iter(nodes)))
    assert Bench2DriveEpisode._takeover_phase(scenario, actor) == 'waiting_for_trigger'
    sync.status = 'Status.RUNNING'
    assert Bench2DriveEpisode._takeover_phase(scenario, actor) == 'synchronizing_arrival'
    cut_in.status = 'Status.RUNNING'
    assert Bench2DriveEpisode._takeover_phase(scenario, actor) == 'native_maneuver_started'


@pytest.mark.parametrize('kind,elevation', [('HighwayCutIn', 0), ('HighwayCutIn', 360),
                                          ('StaticCutIn', 0), ('StaticCutIn', 600)])
def test_takeover_requires_native_placement_at_any_elevation(monkeypatch, kind, elevation):
    from easycarla.scenarios.bench2drive.adapter import Bench2DriveEpisode
    from easycarla.scenarios.bench2drive import progress
    monkeypatch.setattr(progress, 'scenario_progress', lambda scene: {
        'extraction': 'pinned_basic_scenario_route_wrapper_v1'})
    offset = 100 if kind == 'HighwayCutIn' else 500
    actor = SimpleNamespace(id=3, get_location=lambda: SimpleNamespace(z=elevation-offset))
    names = ('SyncArrivalWithAgent', 'CutIn') if kind == 'HighwayCutIn' else ('CutIn', 'BasicAgentBehavior')
    nodes = []
    for name in names:
        node = type(name, (), {})()
        node._actor, node.status = actor, 'Status.INVALID'
        nodes.append(node)
    placement = type('ActorTransformSetter' if kind == 'HighwayCutIn' else 'BatchActorTransformSetter', (), {})()
    placement._actor = actor
    placement._actor_transform_list = [(actor, object())]
    placement.status = 'Status.INVALID'
    nodes.append(placement)
    scenario = SimpleNamespace(config=SimpleNamespace(type=kind),
                               behavior_tree=SimpleNamespace(iterate=lambda: iter(nodes)))
    assert Bench2DriveEpisode._takeover_phase(scenario, actor) == 'awaiting_native_placement'
    placement.status = 'Status.RUNNING'
    assert Bench2DriveEpisode._takeover_phase(scenario, actor) == 'awaiting_native_placement'
    actor.get_location = lambda: SimpleNamespace(z=elevation)
    placement.status = 'Status.SUCCESS'
    assert Bench2DriveEpisode._takeover_phase(scenario, actor) == 'waiting_for_trigger'
    nodes.remove(placement)
    assert Bench2DriveEpisode._takeover_phase(scenario, actor) == 'unsupported_native_layout'
    monkeypatch.setattr(progress, 'scenario_progress', lambda scene: {'extraction': 'unsupported'})
    assert Bench2DriveEpisode._takeover_phase(scenario, actor) == 'unsupported_native_layout'


def test_newborn_tm_velocity_uses_one_tick_manual_handoff():
    calls = []
    actor = SimpleNamespace(id=10)
    gateway = SimpleNamespace(set_autopilot=lambda *a, **kw: calls.append(('tm', a, kw)),
                              handoff=lambda *a, **kw: calls.append(('handoff', a, kw)),
                              set_target_velocity=lambda *a, **kw: calls.append(('velocity', a, kw)))
    state = {'leased': set(), 'suppressed': {}, 'controls': gateway, 'owner': 'bench2drive/native',
             'world': SimpleNamespace(get_snapshot=lambda: SimpleNamespace(find=lambda actor_id: None))}
    with control_scope(state):
        native_actor_call(actor, 'set_autopilot', True, 8100)
        native_actor_call(actor, 'set_target_velocity', 'seed speed')
        native_actor_call(actor, 'set_target_velocity', 'updated seed speed')
    assert [c[0] for c in calls] == ['tm', 'handoff', 'velocity', 'velocity']
    assert state['resume_autopilot'] == {10: actor}
    assert state['spawn_velocity_handoffs'] == 1
    assert 10 not in state['autopilot']


def test_route_planner_reuses_only_topology_not_routing_state():
    from easycarla.scenarios.bench2drive.planner import cached_planner, clear_cache
    clear_cache()
    builds = []
    class Planner:
        def __init__(self, world_map, resolution):
            builds.append((world_map.to_opendrive(), resolution))
            self.graph = {'path': [0, 1, 2]}
            self.previous_decision = 'unset'
        def route(self):
            previous = self.previous_decision
            self.previous_decision = 'left'
            return self.graph['path'], previous
    world_map = SimpleNamespace(to_opendrive=lambda: 'map-one')
    first = cached_planner(world_map, 2, Planner)
    second = cached_planner(world_map, 2, Planner)
    assert first is not second and first.graph is second.graph
    assert first.route() == second.route() == Planner(world_map, 2).route()
    assert len(builds) == 2  # cache constructor plus independent reference
    cached_planner(world_map, 1, Planner)
    cached_planner(SimpleNamespace(to_opendrive=lambda: 'map-two'), 1, Planner)
    cached_planner(world_map, 2, Planner)
    assert len(builds) == 5  # one-entry bound evicted both older cache keys
    clear_cache()


def test_native_destruction_releases_replacement_without_extending_lifetime():
    forgotten = []
    episode = SimpleNamespace(owned_ids={9}, state={'leased': {9}, 'autopilot': {9}, 'resume_autopilot': {}},
                              controllers={'adversary': ('actor', 'controller')}, _lease_roles={'adversary': 9},
                              metadata={'mechanism_replacements': []},
                              session=SimpleNamespace(registry=SimpleNamespace(forget=forgotten.append),
                                                      world=SimpleNamespace(get_snapshot=lambda: SimpleNamespace(frame=100))))
    WorldBridge(episode).forget(9)
    assert forgotten == [9]
    assert not episode.controllers and not episode._lease_roles and not episode.state['leased']
    assert episode.metadata['mechanism_replacements'][-1]['event'] == 'native_lifetime_ended'


def test_newborn_constant_velocity_takes_permanent_native_ownership():
    calls = []
    actor = SimpleNamespace(id=12)
    gateway = SimpleNamespace(handoff=lambda *a, **kw: calls.append('handoff'),
                              enable_constant_velocity=lambda *a, **kw: calls.append('constant_velocity'))
    state = {'leased': set(), 'suppressed': {}, 'controls': gateway, 'owner': 'bench2drive/native',
             'autopilot': {12}, 'resume_autopilot': {12: actor},
             'world': SimpleNamespace(get_snapshot=lambda: SimpleNamespace(find=lambda actor_id: None))}
    with control_scope(state):
        native_actor_call(actor, 'enable_constant_velocity', 'speed')
    assert calls == ['handoff', 'constant_velocity']
    assert not state['autopilot'] and not state['resume_autopilot']
    assert state['constant_velocity_handoffs'] == 1


def test_native_matrix_keeps_blocked_routes_and_stops_on_unknown_child(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / 'scripts'))
    import validate_native_matrix as matrix
    source = tmp_path / 'manifest.json'
    source.write_text(json.dumps({'entries': [
        {'route_id': '1', 'town': 'Town01', 'scenario_types': ['One']},
        {'route_id': '2', 'town': 'Town02', 'scenario_types': ['Two']},
        {'route_id': '3', 'town': 'Town13', 'scenario_types': ['Three']}]}))
    calls = []
    def child(command, **kwargs):
        calls.append(command)
        output = Path(command[command.index('--output') + 1])
        output.write_text(json.dumps({'status': 'stopped_unknown_outcome', 'results': [
            {'route_id': '1', 'execution': 'unknown_outcome', 'error': 'RPC result unknown'}]}))
        return SimpleNamespace(returncode=1)
    monkeypatch.setattr(matrix.subprocess, 'run', child)
    args = SimpleNamespace(manifest=source, output=tmp_path/'matrix.json', ticks=10,
                           no_rendering=True, spectator_as_ego=False, map_layers='none', tile_stream_distance=500,
                           actor_active_distance=500, exclude_town=['Town13'], exclude_reason='recorded native crash',
                           port=2100, tm_port=8100, timeout=1, reload=True)
    result = matrix.run(args)
    assert len(calls) == 1
    assert result['status'] == 'stopped_uncertain_child'
    assert [row['execution'] for row in result['results']] == ['unknown_outcome', 'not_run', 'blocked']
    assert json.loads(args.output.read_text())['results'] == result['results']


def test_explicit_native_velocity_impulse_respects_leases_and_cancels_resume():
    calls = []
    actor = SimpleNamespace(id=15)
    gateway = SimpleNamespace(handoff=lambda *a, **kw: calls.append('handoff'),
                              set_target_velocity=lambda *a, **kw: calls.append('velocity'),
                              set_autopilot=lambda *a, **kw: calls.append('autopilot'))
    state = {'leased': set(), 'suppressed': {}, 'controls': gateway, 'owner': 'bench2drive/native',
             'autopilot': {15}}
    with control_scope(state):
        native_actor_call(actor, 'apply_native_velocity_impulse', 'zero')
        assert state['resume_autopilot'] == {15: actor}
        native_actor_call(actor, 'set_autopilot', False)
        assert not state['resume_autopilot']
        state['leased'].add(15)
        native_actor_call(actor, 'apply_native_velocity_impulse', 'zero')
    assert calls == ['handoff', 'velocity', 'autopilot']
    assert state['suppressed'] == {15: 1}


def test_background_patch_orders_stop_and_names_crossing_impulse():
    spec = importlib.util.spec_from_file_location('prepare_native_order', ROOT / 'scripts/prepare_bench2drive.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    source = """if condition:
    if next_wps:
                        _easycarla_native_call(actor, 'set_target_velocity', carla.Vector3D(0, 0, 0))
                        actor.set_light_state(carla.VehicleLightState(lights))
                        _easycarla_native_call(actor, 'set_autopilot', False, self._tm_port)
_easycarla_native_call(actor, 'set_target_velocity', carla.Vector3D())
"""
    patched = module.patch_background_ownership(source)
    ast.parse(patched)
    assert patched.index("'set_autopilot'") < patched.index("'set_target_velocity'")
    assert "'apply_native_velocity_impulse'" in patched
    with pytest.raises(ValueError, match='no longer matches'):
        module.patch_background_ownership('changed upstream')


def test_named_transform_reset_restores_only_existing_tm_ownership():
    calls=[];actor=SimpleNamespace(id=17)
    gateway=SimpleNamespace(handoff=lambda *args,**kwargs:calls.append('handoff'),
                            set_target_velocity=lambda *args,**kwargs:calls.append('velocity'))
    state={'controls':gateway,'owner':'bench2drive/native','leased':set(),'suppressed':{},'autopilot':{17}}
    with control_scope(state):
        native_actor_call(actor,'apply_native_transform_reset','zero')
        assert state['resume_autopilot']=={17:actor} and not state['autopilot']
        assert state['transform_reset_handoffs']==1
        native_actor_call(actor,'apply_native_transform_reset','zero')
        state['leased'].add(17)
        native_actor_call(actor,'apply_native_transform_reset','zero')
    assert calls==['handoff','velocity','velocity'] and state['suppressed']=={17:1}
    manual={'controls':gateway,'owner':'bench2drive/native','leased':set(),'suppressed':{}}
    with control_scope(manual):native_actor_call(actor,'apply_native_transform_reset','zero')
    assert 'resume_autopilot' not in manual


def test_transform_reset_patch_is_class_scoped():
    spec=importlib.util.spec_from_file_location('prepare_transform',ROOT/'scripts/prepare_bench2drive.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    source="""class ActorTransformSetter:
    def initialise(self):
        _easycarla_native_call(self._actor, 'set_target_velocity', carla.Vector3D(0, 0, 0))
        _easycarla_native_call(self._actor, 'set_target_angular_velocity', carla.Vector3D(0, 0, 0))
        self._actor.set_transform(self._transform)
class Other:
    def initialise(self):
        _easycarla_native_call(self._actor, 'set_target_velocity', carla.Vector3D(0, 0, 0))
"""
    patched=module.patch_transform_reset(source);ast.parse(patched)
    assert patched.count("'apply_native_transform_reset'")==1
    assert patched.count("'set_target_velocity'")==1
    with pytest.raises(ValueError):module.patch_transform_reset('class Changed: pass\n')


def test_native_physics_lifecycle_is_witnessed_even_for_leased_actor():
    from easycarla.simulation.resources import ActorRegistry
    registry = ActorRegistry()
    calls = []
    actor = SimpleNamespace(id=1, set_simulate_physics=lambda *a, **kw: calls.append((a, kw)))
    state = {'leased': {1}, 'suppressed': {},
             'controls': SimpleNamespace(session=SimpleNamespace(registry=registry))}
    with control_scope(state):
        native_actor_call(actor, 'set_simulate_physics', False)
        assert registry.physics_enabled[1] is False
        native_actor_call(actor, 'set_simulate_physics', enabled=True)
    assert registry.physics_enabled[1] is True
    assert len(calls) == 2 and not state['suppressed']


def test_opaque_spawn_physics_is_witnessed_without_changing_command():
    from easycarla.scenarios.bench2drive.native_control import mark_spawn_physics
    from easycarla.simulation.resources import ActorRegistry
    command = type('SpawnActor', (), {})()
    physics_command = object()
    actor = SimpleNamespace(id=9)
    registry = ActorRegistry()
    state = {'spawn_physics': {}}
    with control_scope(state):
        assert mark_spawn_physics(command, physics_command, False) is physics_command
    episode = SimpleNamespace(state=state, deferred_batch_ticks=0,
        session=SimpleNamespace(registry=registry,
            client=SimpleNamespace(apply_batch_sync=lambda batch, tick: [SimpleNamespace(actor_id=9, error='')]),
            world=SimpleNamespace(get_actor=lambda actor_id: actor)),
        world=SimpleNamespace(_register=lambda actor: None))
    ClientBridge(episode).apply_batch_sync([command])
    assert registry.physics_enabled[9] is False and not state['spawn_physics']


@pytest.mark.parametrize("patch_version,expected", [(4, None), (5, None), (6, True)])
def test_native_registration_keeps_unwitnessed_physics_unknown(patch_version, expected):
    from easycarla.simulation.resources import ActorRegistry
    registry = ActorRegistry()
    actor = SimpleNamespace(id=9, type_id="vehicle.fixture", attributes={})
    episode = SimpleNamespace(owned_ids=set(), semantic_roles={}, spawn_count=0,
        metadata={"patch_version": patch_version},
        session=SimpleNamespace(registry=registry, controls=SimpleNamespace(claim=lambda *a, **kw: None)))
    assert WorldBridge(episode)._register(actor) is actor
    assert registry.physics_enabled[9] is expected
