"""Numerical coordinate/history tests independent of CARLA Python bindings."""
from types import SimpleNamespace as NS
import math

import numpy as np
import pytest

from easycarla.config import validate_config
from easycarla.observations.builder import ObservationBuilder, actor_state
from easycarla.observations.geometry import local, pose, wrap, xyz
from easycarla.observations.maps import build_map
from easycarla.simulation.resources import ActorRegistry


def vector(x=0, y=0, z=0):
    return NS(x=x, y=y, z=z)


class Actor:
    def __init__(self, actor_id, x=0, y=0, yaw=0, type_id="vehicle.fixture"):
        self.id, self.type_id = actor_id, type_id
        self.position = vector(x, y)
        self.yaw = yaw
        self.velocity = vector()
        self.acceleration = vector()
        self.angular_velocity = vector()
        self.bounding_box = NS(extent=vector(2, 1, .75))
        self.is_alive = True

    def get_transform(self):
        return NS(location=self.position, rotation=NS(yaw=self.yaw))

    def get_location(self):
        return self.position

    def get_velocity(self):
        return self.velocity

    def get_acceleration(self):
        return self.acceleration

    def get_angular_velocity(self):
        return self.angular_velocity

    def get_control(self):
        return NS(steer=.1, throttle=.2, brake=0)

    def get_speed_limit(self):
        return 36


def snapshot(frame, time):
    return NS(frame=frame, timestamp=NS(elapsed_seconds=time))


@pytest.fixture
def observer_factory(monkeypatch):
    monkeypatch.setattr("easycarla.observations.builder.build_map", lambda _: {"map_id": "fixture-content-id"})
    def make(actors, *, max_objects=2, history_frames=3):
        registry = ActorRegistry()
        registry.register(actors[0], "ego")
        for index, actor in enumerate(actors[1:]):
            registry.register(actor, f"npc/{index}")
        session = NS(world=NS(get_actors=lambda: actors), map=object(), registry=registry)
        episode = NS(ego=actors[0], route=[NS(location=vector(i, 0)) for i in range(101)])
        cfg = validate_config({"observations": {"max_objects": max_objects, "history_frames": history_frames}})
        return ObservationBuilder(session, episode, cfg)
    return make


def test_left_handed_carla_to_right_handed_world_and_ego_coordinates():
    np.testing.assert_array_equal(xyz(vector(1, 2, 3)), [1, -2, 3])
    location, yaw = pose(NS(location=vector(1, 2, 3), rotation=NS(yaw=90)))
    assert yaw == pytest.approx(-math.pi/2)
    np.testing.assert_allclose(local([1, -12, 3], location, yaw), [10, 0, 0], atol=1e-6)
    np.testing.assert_allclose(local([4, -2, 3], location, yaw), [0, 3, 0], atol=1e-6)
    np.testing.assert_allclose(local([0, -2, 0], location, yaw, vector=True), [2, 0, 0], atol=1e-6)
    assert wrap(3 * math.pi) == pytest.approx(-math.pi)


def test_actor_state_has_si_angular_units_and_dimensions():
    actor = Actor(1, x=1, y=2, yaw=90)
    actor.angular_velocity = vector(z=180)
    actor.velocity = vector(3, 4, 0)
    result = actor_state(actor)
    assert result["yaw"] == pytest.approx(-math.pi/2)
    assert result["yaw_rate"] == pytest.approx(-math.pi)
    assert result["speed"] == pytest.approx(5)
    assert result["size"] == [4, 2, 1.5]
    assert result["velocity"] == [3, -4, 0]


def test_history_is_compensated_to_current_ego_pose_with_masks(observer_factory):
    ego, npc = Actor(1), Actor(2, x=10)
    actors = [ego, npc]
    observer = observer_factory(actors)
    npc.velocity = vector(1, 0)
    observer.sample(snapshot(10, 0.0), 0)
    first = observer.build(snapshot(10, 0.0))
    assert first["objects"]["valid"].sum() == 1
    assert first["objects"]["position"].shape == (3, 2, 3)
    npc.position.x = 11
    ego.position.x = 5
    ego.yaw = 90
    observer.sample(snapshot(12, .1), 2)
    obs = observer.build(snapshot(12, .1))
    objects = obs["objects"]
    np.testing.assert_allclose(objects["position"][-2:, 0], [[0, 5, 0], [0, 6, 0]], atol=1e-6)
    np.testing.assert_allclose(objects["velocity"][-2:, 0], [[0, 1, 0], [0, 1, 0]], atol=1e-6)
    np.testing.assert_allclose(objects["yaw"][-2:, 0], [math.pi/2, math.pi/2])
    np.testing.assert_array_equal(objects["valid"][:, 0], [False, True, True])
    assert not objects["valid"][:, 1].any()
    np.testing.assert_allclose(objects["timestamps"], [0, 0, .1])
    assert objects["ids"][0] == first["objects"]["ids"][0]
    assert obs["ego"]["speed_limit"] == 10.0
    assert obs["coordinate_frame"] == "ego_flu"
    assert "mechanism" not in obs and "controller" not in obs


def test_history_sampling_interval_and_capacity_are_explicit(observer_factory):
    ego, npc = Actor(1), Actor(2, x=10)
    observer = observer_factory([ego, npc], history_frames=2)
    for tick in range(5):
        observer.sample(snapshot(100 + tick, tick * .05), tick)
    obs = observer.build(snapshot(104, .2))
    np.testing.assert_allclose(obs["objects"]["timestamps"], [.1, .2])
    assert obs["objects"]["valid"][:, 0].all()


def test_capacity_truncation_and_stable_ids_ignore_iteration_order(observer_factory):
    ego = Actor(1)
    actors = [ego, Actor(2, 3), Actor(3, 1, type_id="walker.pedestrian.fixture"), Actor(4, 10)]
    observer = observer_factory(actors, max_objects=2)
    observer.sample(snapshot(1, 0), 0)
    first = observer.build(snapshot(1, 0))
    assert first["objects"]["count"] == 2 and first["objects"]["truncated"] == 1
    assert first["objects"]["type"][-1, 0] == 2
    ids = first["objects"]["ids"].copy()
    actors.reverse()
    observer.sample(snapshot(3, .1), 2)
    second = observer.build(snapshot(3, .1))
    np.testing.assert_array_equal(second["objects"]["ids"], ids)


def test_new_actor_has_no_fabricated_history(observer_factory):
    ego, npc = Actor(1), Actor(2, 3)
    actors = [ego, npc]
    observer = observer_factory(actors)
    observer.sample(snapshot(1, 0), 0)
    newcomer = Actor(3, 1)
    actors.append(newcomer)
    observer.sample(snapshot(3, .1), 2)
    obs = observer.build(snapshot(3, .1))
    newcomer_index = list(obs["objects"]["ids"]).index(observer.semantic_id(newcomer))
    np.testing.assert_array_equal(obs["objects"]["valid"][:, newcomer_index], [False, False, True])


def test_static_map_boundaries_use_left_positive_and_content_id():
    class Transform:
        location = vector(1, 2)
        def get_right_vector(self):
            return vector(0, 1)
    wp = NS(road_id=1, section_id=0, lane_id=-1, s=0,
            transform=Transform(), lane_width=4, lane_type=1, lane_change=0, next=lambda distance: [])
    world_map = NS(name="FixtureTown", to_opendrive=lambda: "<OpenDRIVE/>",
                   generate_waypoints=lambda distance: [wp], get_topology=lambda: [(wp, wp)])
    static = build_map(world_map)
    np.testing.assert_allclose(static["center"], [[1, -2, 0]])
    np.testing.assert_allclose(static["left_boundary"], [[1, 0, 0]])
    np.testing.assert_allclose(static["right_boundary"], [[1, -4, 0]])
    assert static["coordinate_frame"] == "world_rh" and len(static["map_id"]) == 64
    assert static["map_id"] == build_map(world_map)["map_id"]


def test_signal_stopline_and_lane_association_use_ego_coordinates(observer_factory):
    transform = NS(location=vector(10, 0), get_forward_vector=lambda: vector(1, 0),
                   get_right_vector=lambda: vector(0, 1), transform=lambda point: vector(10, 0))
    wp = NS(transform=transform, lane_width=4, road_id=3, section_id=2, lane_id=-1)
    signal = NS(id=8, type_id="traffic.traffic_light", get_transform=lambda: transform,
                trigger_volume=NS(location=vector()), get_state=lambda: 0,
                get_stop_waypoints=lambda: [wp])
    ego = Actor(1, x=5)
    observer = observer_factory([ego, signal])
    observer.sample(snapshot(1, 0), 0)
    obs = observer.build(snapshot(1, 0))
    signals = obs["signals"]
    assert signals["valid"].sum() == 1
    np.testing.assert_allclose(signals["position"][0], [5, 0, 0])
    np.testing.assert_allclose(signals["stop_line"][0], [[5, 2, 0], [5, -2, 0]])
    np.testing.assert_array_equal(signals["lane_ids"][0], [3, 2, -1])
    assert signals["ids"][0] == 8


def test_map_speed_defaults_have_explicit_units_and_unknowns():
    from easycarla.observations.maps import road_speed_records
    xml = '''<OpenDRIVE><road id="1"><type s="0"><speed max="36" unit="km/h"/></type>
      <type s="20"><speed max="20" unit="mph"/></type></road>
      <road id="2"><type s="0"><speed max="no limit" unit="m/s"/></type></road></OpenDRIVE>'''
    records = road_speed_records(xml)
    assert records[1] == [(0, 10), (20, 8.9408)]
    assert records[2] == [(0, None)]


def test_frame_capture_is_immutable_and_shared_by_repeat_builds(observer_factory):
    from easycarla.observations.frame import FrameStateError
    ego, npc = Actor(1), Actor(2, 10)
    observer = observer_factory([ego, npc])
    count = {"world": 0, "ego": 0, "npc": 0}
    def actors():
        count["world"] += 1
        return [ego, npc]
    observer.session.world.get_actors = actors
    for label, actor in (("ego", ego), ("npc", npc)):
        original = actor.get_transform
        def counted(original=original, label=label):
            count[label] += 1
            return original()
        actor.get_transform = counted
    observer.sample(snapshot(1, 0), 0)
    before = dict(count)
    ego.position.x = 99
    first = observer.build(snapshot(1, 0))
    second = observer.build(snapshot(1, 0))
    observer.sample(snapshot(1, 0), 0)
    assert count == before == {"world": 1, "ego": 1, "npc": 1}
    assert len(observer.history) == 1
    assert first["ego"]["position_world"][0] == second["ego"]["position_world"][0] == 0
    with pytest.raises(TypeError): observer.get_state(1)["speed"] = 100
    with pytest.raises(TypeError): observer.get_state(1)["position"][0] = 100
    with pytest.raises(TypeError): observer.frame_state.actors._data = {}
    with pytest.raises(FrameStateError, match="Requested frame"): observer.get_state(1, frame=2)
    with pytest.raises(FrameStateError, match="two timestamps"): observer.sample(snapshot(1, 2), 0)
    with pytest.raises(FrameStateError, match="timestamp differs"): observer.build(snapshot(1, 2))
    with pytest.raises(FrameStateError, match="backward"): observer.sample(snapshot(0, 0), 0)


def test_capture_uses_world_snapshot_kinematics_and_rejects_missing_ego(observer_factory):
    from easycarla.observations.frame import FrameStateError
    ego = Actor(1, x=90)
    observed = Actor(1, x=7, yaw=30)
    observer = observer_factory([ego])
    committed = snapshot(15, .75)
    committed.find = lambda actor_id: observed
    observer.sample(committed, 0)
    assert observer.get_state(ego)["position"][0] == 7
    assert observer.build(committed)["ego"]["position_world"][0] == 7
    absent = snapshot(16, .8)
    absent.find = lambda actor_id: None
    with pytest.raises(FrameStateError, match="Ego actor is absent"):
        observer.sample(absent, 1)
    assert observer.frame_state.frame == 15


def test_sample_only_actors_present_in_committed_snapshot(observer_factory):
    ego, npc = Actor(1), Actor(2, x=5)
    observer = observer_factory([ego, npc])
    committed = snapshot(10, .5)
    committed.find = lambda actor_id: ego if actor_id == 1 else None
    observer.sample(committed, 0)
    assert set(observer.frame_state.actors) == {1}
    assert observer.build(committed)["objects"]["count"] == 0


def test_map_cache_reuses_geometry_with_detached_metadata_and_immutable_arrays():
    from easycarla.observations.maps import StaticMapCache
    cache = StaticMapCache(max_entries=2, max_bytes=1024)
    calls = []
    world_map = NS(name="CanonicalTown", to_opendrive=lambda: "<OpenDRIVE/>",
                   generate_waypoints=lambda distance: calls.append(distance) or [], get_topology=lambda: [])
    first = build_map(world_map, cache=cache)
    world_map.name = "TownAlias"
    second = build_map(world_map, cache=cache)
    assert calls == [2.]
    assert first["map_id"] == second["map_id"]
    assert first["name"] == "CanonicalTown" and second["name"] == "TownAlias"
    assert first["center"] is second["center"]
    first["schema_version"] = 999
    assert build_map(world_map, cache=cache)["schema_version"] == 1
    with pytest.raises(ValueError): second["center"].setflags(write=True)
    assert cache.info()["hits"] == 2 and cache.info()["misses"] == 1


def test_map_cache_lru_has_entry_and_byte_bounds():
    from easycarla.observations.maps import StaticMapCache
    cache = StaticMapCache(max_entries=2, max_bytes=160)
    cache.put("one", {"array": np.zeros(10)})
    cache.put("two", {"array": np.zeros(10)})
    assert cache.get("one") is not None
    cache.put("three", {"array": np.zeros(10)})
    assert cache.get("two") is None
    assert cache.get("one") is not None and cache.get("three") is not None
    assert cache.info()["entries"] == 2 and cache.info()["bytes"] == 160
    too_big = cache.put("large", {"array": np.zeros(21)})
    assert not too_big["array"].flags.writeable
    assert cache.get("large") is None
    assert cache.info()["entries"] == 2
    cache.clear()
    assert cache.info()["entries"] == cache.info()["bytes"] == 0
    with pytest.raises(ValueError): StaticMapCache(max_bytes=0)
