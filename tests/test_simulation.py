"""Resource and frame-order contracts without a CARLA server."""
from collections import Counter
from types import SimpleNamespace as NS
import threading
import time

import numpy as np
import pytest

from easycarla.simulation.resources import ActorRegistry
from easycarla.simulation.sensors import (CollisionBarrier, LateSensorEvent, SensorSuite,
                                           SensorSynchronizer, SensorTimeout)
from easycarla.simulation.session import WorldSession


def test_map_asset_resolution_preserves_exact_town_and_refuses_ambiguity():
    from easycarla.simulation.session import resolve_map_asset
    client = NS(get_available_maps=lambda: ["/Game/Maps/Town11/Town11", "/Game/Maps/Town11_Opt"])
    assert resolve_map_asset(client, "Town11") == "/Game/Maps/Town11/Town11"
    assert resolve_map_asset(client, "/Game/Maps/Town11_Opt") == "/Game/Maps/Town11_Opt"
    with pytest.raises(ValueError, match="0 installed"):
        resolve_map_asset(client, "Town12")
    client.get_available_maps = lambda: ["/Game/A/Town11", "/Game/B/Town11"]
    with pytest.raises(ValueError, match="2 installed"):
        resolve_map_asset(client, "Town11")


def test_loaded_map_visibility_wait_is_read_only_and_bounded():
    from easycarla.simulation.session import await_loaded_world
    old = NS(get_map=lambda: NS(name="/Game/Old"))
    new = NS(get_map=lambda: NS(name="/Game/Town11/Town11"))
    client = NS(get_world=lambda: new)
    assert await_loaded_world(client, old, "Town11", .1) is new
    with pytest.raises(RuntimeError, match="requested 'Town11'"):
        await_loaded_world(client, old, "Town11", 0)


class Actor:
    def __init__(self, actor_id, type_id="vehicle.fake", events=None):
        self.id, self.type_id = actor_id, type_id
        self.is_alive = True
        self.events = events if events is not None else []
        self.fail = False

    def stop(self):
        self.events.append((self.id, "stop"))

    def destroy(self):
        self.events.append((self.id, "destroy"))
        if self.fail:
            raise RuntimeError("temporary destroy failure")
        self.is_alive = False
        return True


def test_only_owned_actors_are_destroyed_and_sensors_stop_first():
    events = []
    owned = Actor(1, events=events)
    foreign = Actor(99, events=events)
    sensor = Actor(2, "sensor.camera.rgb", events)
    controller = Actor(3, "controller.ai.walker", events)
    registry = ActorRegistry()
    registry.register(owned, "ego")
    registry.register(sensor, "sensor/rgb")
    registry.register(controller, "walker/controller")
    assert registry.destroy([foreign.id]) == []
    assert foreign.is_alive
    assert registry.clear() == []
    assert events.index((2, "stop")) < events.index((2, "destroy")) < events.index((1, "destroy"))
    assert events.index((3, "stop")) < events.index((3, "destroy")) < events.index((1, "destroy"))
    assert foreign.is_alive and not registry.actors and not registry.roles
    assert registry.clear() == []


def test_cleanup_failure_retains_actor_ownership_for_retry():
    actor = Actor(1)
    actor.fail = True
    registry = ActorRegistry()
    registry.register(actor, "ego")
    assert registry.clear()[0]["actor_id"] == actor.id
    assert registry.actors[1] is actor and registry.roles[1] == "ego"
    actor.fail = False
    assert registry.clear() == []
    assert not actor.is_alive and not registry.actors


def packet(frame, value):
    return NS(frame=frame, value=value)


def test_frame_barrier_collects_exact_frame_despite_out_of_order_delivery():
    sync = SensorSynchronizer(["rgb", "lidar"], timeout=.1, generation="episode-2")
    sync.push("rgb", packet(11, "future-rgb"), "episode-2")
    sync.push("lidar", packet(10, "target-lidar"), "episode-2")
    sync.push("rgb", packet(9, "obsolete-rgb"), "episode-2")
    sync.push("rgb", packet(10, "target-rgb"), "episode-2")
    result = sync.collect(10)
    assert {p.frame for p in result.values()} == {10}
    assert result["rgb"].value == "target-rgb"
    assert 9 not in sync.frames and 11 in sync.frames
    sync.push("lidar", packet(11, "future-lidar"), "episode-2")
    assert {p.frame for p in sync.collect(11).values()} == {11}


def test_old_episode_callback_is_rejected_and_missing_frame_times_out():
    sync = SensorSynchronizer(["rgb"], timeout=.01, generation="new")
    sync.push("rgb", packet(20, "old-episode"), "old")
    sync.push("rgb", packet(21, "wrong-frame"), "new")
    with pytest.raises(SensorTimeout, match="20"):
        sync.collect(20)
    sync.push("rgb", packet(20, "new-episode"), "new")
    assert sync.collect(20)["rgb"].value == "new-episode"


def test_frame_barrier_can_wait_for_sensor_without_advancing_world():
    sync = SensorSynchronizer(["rgb"], timeout=.5)
    delivered = threading.Event()
    def produce():
        time.sleep(.01)
        sync.push("rgb", packet(3, "ready"))
        delivered.set()
    worker = threading.Thread(target=produce)
    worker.start()
    assert sync.collect(3)["rgb"].value == "ready"
    worker.join(.5)
    assert delivered.is_set()


def test_close_interrupts_sensor_wait():
    sync = SensorSynchronizer(["rgb"], timeout=2)
    outcome = []
    def consume():
        try:
            sync.collect(4)
        except SensorTimeout:
            outcome.append("closed")
    worker = threading.Thread(target=consume)
    worker.start()
    sync.close()
    worker.join(.5)
    assert outcome == ["closed"]
    sync.push("rgb", packet(4, "late"))
    assert not any(sync.frames.values())


def test_sensor_pixel_and_lidar_coordinate_conversion_and_capacity():
    points = np.array([[1, 2, 3, .4], [4, 5, 6, .7]], np.float32)
    pixels = np.array([[[10, 20, 30, 255], [40, 50, 60, 255]]], np.uint8)
    data = {"lidar": NS(raw_data=points.tobytes(), frame=7, timestamp=.35),
            "rgb": NS(raw_data=pixels.tobytes(), height=1, width=2, timestamp=.35),
            "bev": NS(raw_data=pixels.tobytes(), height=1, width=2, timestamp=.35)}
    suite = SensorSuite.__new__(SensorSuite)
    suite.sync = NS(collect=lambda frame: data)
    suite.config = {"lidar_points": 1}
    suite.ego = NS(get_transform=lambda: NS(location=NS(x=0, y=0, z=0), rotation=NS(yaw=0)))
    mount = np.eye(4)
    mount[2, 3] = 2.4
    for packet in data.values():
        packet.transform = NS(get_matrix=lambda: mount)
    suite.collision_barrier = CollisionBarrier("test", settle_seconds=0)
    result = suite.collect(7)
    np.testing.assert_allclose(result["lidar"]["points"], [[1, -2, 5.4, .4]])
    assert result["lidar"]["count"] == 1 and result["lidar"]["truncated"] == 1
    np.testing.assert_array_equal(result["rgb"]["data"], [[[30, 20, 10], [60, 50, 40]]])
    np.testing.assert_array_equal(result["bev"]["data"], [[30, 60]])
    assert all(item["frame"] == 7 for item in result.values())


class TickWorld:
    def __init__(self, frame=10, increment=1):
        self.frame = frame
        self.increment = increment
        self.calls = 0

    def get_snapshot(self):
        return NS(frame=self.frame)

    def tick(self, timeout):
        self.calls += 1
        self.frame += self.increment
        return self.frame


def session_with(world):
    session = WorldSession.__new__(WorldSession)
    session.world = world
    session.controls = NS(flush=lambda: None, after_tick=lambda: None)
    session.last_frame = world.frame
    session.sim = {"timeout": .1}
    session.ticks = Counter()
    session.timings = Counter()
    return session


def test_tick_owner_requires_contiguous_world_frames():
    world = TickWorld()
    session = session_with(world)
    assert session.tick("initialization").frame == 11
    assert session.tick().frame == 12
    assert session.ticks == {"initialization": 1, "step": 1}
    world.frame += 1
    with pytest.raises(RuntimeError, match="another client"):
        session.tick()
    assert world.calls == 2


def test_hidden_double_tick_is_a_fault():
    world = TickWorld(increment=2)
    session = session_with(world)
    with pytest.raises(RuntimeError, match="Non-contiguous"):
        session.tick()
    assert session.ticks["step"] == 0


def traffic_frame(x, y=0, speed=0, kind="traffic.traffic_light"):
    return NS(actors={1: {"position": [x, y, 0], "velocity": [speed, 0, 0]}},
              signals=[{"actor_id": 9, "type": kind, "position": [0, 0, 0],
                        "direction": [1, 0, 0], "right": [0, -1, 0],
                        "lane_width": 4, "lane_ids": [1, 0, -1], "prohibited": True}])


def test_red_light_waiting_is_safe_and_crossing_is_single_event():
    from easycarla.simulation.traffic import TrafficRules
    rules = TrafficRules(None, NS(id=1))
    assert rules.update(traffic_frame(-5)) == []
    assert rules.update(traffic_frame(-1, speed=2)) == []
    assert rules.update(traffic_frame(1, speed=2)) == ["9:1:-1"]
    assert rules.update(traffic_frame(2, speed=2)) == []


def test_adjacent_lane_does_not_trigger_signal_violation():
    from easycarla.simulation.traffic import TrafficRules
    rules = TrafficRules(None, NS(id=1))
    assert rules.update(traffic_frame(-1, y=5, speed=2)) == []
    assert rules.update(traffic_frame(1, y=5, speed=2)) == []


def test_stop_sign_requires_a_stop_before_crossing():
    from easycarla.simulation.traffic import TrafficRules
    stopped, rolling = TrafficRules(None, NS(id=1)), TrafficRules(None, NS(id=1))
    stopped.update(traffic_frame(-2, kind="traffic.stop"))
    rolling.update(traffic_frame(-2, speed=2, kind="traffic.stop"))
    assert stopped.update(traffic_frame(1, speed=2, kind="traffic.stop")) == []
    assert rolling.update(traffic_frame(1, speed=2, kind="traffic.stop")) == ["9:1:-1"]


def test_collision_before_frame_seal_is_visible_and_commit_is_explicit():
    barrier = CollisionBarrier("episode", settle_seconds=0)
    barrier.push(12, 99, "episode")
    barrier.collect(12)
    assert barrier.snapshot() == [(12, 99)]
    assert barrier.published_frame == -1
    barrier.commit(12)
    assert barrier.published_frame == 12


def test_late_collision_invalidates_affected_frame_instead_of_later_reward():
    barrier = CollisionBarrier("episode", settle_seconds=0)
    barrier.collect(12)
    barrier.commit(12)
    barrier.push(12, 99, "episode")
    for operation in (barrier.assert_healthy, barrier.snapshot, lambda: barrier.collect(13), lambda: barrier.commit(12)):
        with pytest.raises(LateSensorEvent) as caught:
            operation()
        assert caught.value.details == {"sensor": "collision", "event_frame": 12,
                                         "sealed_frame": 12, "published_frame": 12, "actor_id": 99}
        assert "frame 12" in str(caught.value)
    assert barrier.events == [], "A late collision must not enter a later frame's reward input"


def test_collision_after_collect_before_publication_fails_commit():
    barrier = CollisionBarrier("episode", settle_seconds=0)
    barrier.collect(5)
    barrier.push(5, 3, "episode")
    with pytest.raises(LateSensorEvent) as caught:
        barrier.commit(5)
    assert caught.value.details["published_frame"] == -1


def test_collision_generation_change_and_close_reject_old_callbacks():
    barrier = CollisionBarrier("new-world", settle_seconds=0)
    barrier.collect(100)
    barrier.commit(100)
    barrier.push(1, 3, "old-world")
    barrier.assert_healthy()
    assert barrier.snapshot() == []
    barrier.close()
    barrier.push(100, 3, "new-world")
    assert barrier.events == [] and barrier.late is None


def test_bounded_settle_includes_event_arriving_after_heartbeat():
    barrier = CollisionBarrier("episode", settle_seconds=.05)
    waiting = threading.Event()
    original_wait = barrier.condition.wait
    def wait(timeout=None):
        waiting.set()
        return original_wait(timeout)
    barrier.condition.wait = wait
    outcome = []
    worker = threading.Thread(target=lambda: (barrier.collect(20), outcome.append("sealed")))
    worker.start()
    assert waiting.wait(.5)
    barrier.push(20, 7, "episode")
    worker.join(.5)
    assert outcome == ["sealed"] and barrier.snapshot() == [(20, 7)]


def test_close_interrupts_collision_settle():
    barrier = CollisionBarrier("episode", settle_seconds=.1)
    waiting = threading.Event()
    original_wait = barrier.condition.wait
    def wait(timeout=None):
        waiting.set()
        return original_wait(timeout)
    barrier.condition.wait = wait
    outcome = []
    def collect():
        try:
            barrier.collect(20)
        except SensorTimeout:
            outcome.append("closed")
    worker = threading.Thread(target=collect)
    worker.start()
    assert waiting.wait(.5)
    barrier.close()
    worker.join(.5)
    assert outcome == ["closed"]


@pytest.fixture
def heartbeat_suite():
    class Blueprint:
        def __init__(self, name):
            self.id, self.attributes = name, {}
        def set_attribute(self, key, value):
            self.attributes[key] = value
    class Sensor(Actor):
        def listen(self, callback):
            self.callback = callback
        def emit(self, packet):
            self.callback(packet)
    spawned = []
    def spawn(bp, transform, attach_to):
        actor = Sensor(100 + len(spawned), bp.id)
        actor.blueprint = bp
        spawned.append(actor)
        return actor
    world = NS(get_blueprint_library=lambda: NS(find=Blueprint), spawn_actor=spawn)
    session = NS(world=world, registry=ActorRegistry(), sim={"physics_dt": .05},
                 carla=NS(Transform=lambda *a, **k: NS()))
    suite = SensorSuite(session, NS(id=1), {"rgb": False, "bev": False, "lidar": False,
                        "sensor_timeout": .01, "collision_settle_seconds": 0}, "new")
    yield suite, spawned
    suite.close()


def test_cpu_heartbeat_is_required_even_without_observation_sensors(heartbeat_suite):
    suite, spawned = heartbeat_suite
    assert {a.type_id for a in spawned} == {"sensor.other.collision", "sensor.other.imu"}
    imu = next(a for a in spawned if a.type_id == "sensor.other.imu")
    assert imu.blueprint.attributes["sensor_tick"] == "0.0"
    with pytest.raises(SensorTimeout, match="heartbeat"):
        suite.collect(7)
    imu.emit(NS(frame=7))
    assert suite.collect(7) == {}, "The internal heartbeat must not enter policy observations"
    suite.commit(7)


def test_suite_late_callback_is_a_preflight_fault_and_close_disables_callbacks(heartbeat_suite):
    suite, spawned = heartbeat_suite
    imu = next(a for a in spawned if a.type_id == "sensor.other.imu")
    collision = next(a for a in spawned if a.type_id == "sensor.other.collision")
    imu.emit(NS(frame=9))
    suite.collect(9)
    suite.commit(9)
    collision.emit(NS(frame=9, other_actor=NS(id=3)))
    with pytest.raises(LateSensorEvent, match="published frame 9"):
        suite.assert_healthy()
    with pytest.raises(LateSensorEvent):
        suite.collect(10)
    suite.close()
    before = suite.collision_barrier.late.copy()
    collision.emit(NS(frame=8, other_actor=NS(id=4)))
    imu.emit(NS(frame=10))
    assert suite.collision_barrier.late == before
    assert not suite.sync.frames


def test_lidar_full_pose_handles_bank_and_slope_in_levelled_ego_frame():
    # Sensor has yaw 90 deg plus nonzero pitch and roll, attached 2.4 m above ego.
    # The packet pose is authoritative; a fixed z translation alone is incorrect.
    q, c = np.sqrt(.5), np.sqrt(.75)
    rotation = np.array([[0, -q, q], [c, .5*q, .5*q], [-.5, c*q, c*q]])
    ego_lh = np.array([5., 10., 2.])
    matrix = np.eye(4)
    matrix[:3, :3] = rotation
    matrix[:3, 3] = ego_lh + rotation @ [0, 0, 2.4]
    points = np.array([[1, 2, 3, .8]], np.float32)
    packet = NS(raw_data=points.tobytes(), frame=12, timestamp=.6,
                transform=NS(get_matrix=lambda: matrix))
    suite = SensorSuite.__new__(SensorSuite)
    suite.sync = NS(collect=lambda frame: {"lidar": packet})
    suite.config = {"lidar_points": 100}
    suite.collision_barrier = CollisionBarrier("test", settle_seconds=0)
    suite.ego = NS(get_transform=lambda: NS(location=NS(x=5, y=10, z=2),
                                           rotation=NS(yaw=90, pitch=-30, roll=45)))
    lidar = suite.collect(12)["lidar"]
    expected = np.array([c + 3.7*q, 3.4*q, -.5 + 7.4*c*q])
    np.testing.assert_allclose(lidar["points"][0, :3], expected, rtol=1e-6)
    assert lidar["points"][0, 3] == pytest.approx(.8)
    assert lidar["coordinate_frame"] == "ego_flu" and lidar["sensor_frame"] == "sensor_flu"
    np.testing.assert_allclose((lidar["sensor_to_ego"] @ [1, -2, 3, 1])[:3], expected, rtol=1e-6)
    np.testing.assert_allclose(lidar["sensor_to_world"][:3, 3], matrix[:3, 3] * [1, -1, 1])
    assert lidar["sensor_to_world"].dtype == np.float64


def test_world_lease_cannot_be_bypassed_by_localhost_alias(monkeypatch, tmp_path):
    import sys
    from types import ModuleType
    from easycarla.config import validate_config
    calls = []
    class Client:
        def __init__(self, host, port):
            calls.append((host, port))
        def set_timeout(self, timeout):
            self.timeout = timeout
    fake_carla = ModuleType("carla")
    fake_carla.Client = Client
    monkeypatch.setitem(sys.modules, "carla", fake_carla)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    config = validate_config({"simulation": {"host": "localhost", "port": 32123,
                                              "carla_root": str(tmp_path)}})
    first = WorldSession(config)
    try:
        alias = validate_config({"simulation": {"host": "127.0.0.1", "port": 32123,
                                                 "carla_root": str(tmp_path)}})
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "another-conda"))
        with pytest.raises(RuntimeError, match="tick owner"):
            WorldSession(alias)
        assert calls == [("localhost", 32123)], "Conflicting owner must fail before constructing a CARLA client"
        other = WorldSession(validate_config({"simulation": {"host": "127.0.0.1", "port": 32124,
                                                              "carla_root": str(tmp_path)}}))
        other.close()
    finally:
        first.close()
    # Releasing the first lease permits either alias to own that same port.
    replacement = WorldSession(alias)
    replacement.close()
    assert calls[-1] == ("127.0.0.1", 32123)
