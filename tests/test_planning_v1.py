"""Numerical raw planning contracts without a planner/model dependency."""

from types import SimpleNamespace as NS
import math
import numpy as np
import pytest
from test_observations import Actor, vector, snapshot
from easycarla.config import validate_config
from easycarla.observations.vehicle_geometry import (
    geometry,
    enrich,
    object_type,
    static_type,
)
from easycarla.observations.frame import actor_state
from easycarla.observations.builder import ObservationBuilder
from easycarla.observations.spec import observation_spec
from easycarla.observations.routes import build_planning_map, describe_route
from easycarla.simulation.resources import ActorRegistry
from easycarla_client.gym_env import numeric_space, project_observation


def measured(actor):
    actor.bounding_box.location = vector(0.5, 0, 0.5)
    actor.bounding_box.rotation = NS(yaw=0)

    def physics():
        y = math.radians(actor.yaw)
        rotation = np.array([[math.cos(y), -math.sin(y)], [math.sin(y), math.cos(y)]])
        positions = np.array(
            [[1.5, 0.7], [1.5, -0.7], [-1.5, 0.7], [-1.5, -0.7]]
        ) @ rotation.T + np.array([actor.position.x, actor.position.y])
        return NS(
            center_of_mass=vector(),
            wheels=[
                NS(
                    position=vector(x * 100, y * 100, actor.position.z * 100),
                    max_steer_angle=40 if i < 2 else 0,
                )
                for i, (x, y) in enumerate(positions)
            ],
        )

    actor.get_physics_control = physics
    return actor


@pytest.mark.parametrize("yaw", [0, 90, -45, 180])
def test_wheel_world_centimeters_converted_to_local_meters(yaw):
    actor = measured(Actor(1, 43, -15, yaw))
    g = geometry(actor)
    assert g["rear_valid"] and g["wheelbase"] == pytest.approx(3)
    np.testing.assert_allclose(g["rear_offset"], [-1.5, 0, 0], atol=1e-10)
    state = enrich(actor, actor, actor_state(actor), g)
    delta = np.array(state["rear_axle"]) - state["position"]
    assert np.linalg.norm(delta) == pytest.approx(1.5)
    assert state["control"][0] == 0.1 and not state["steering_valid"]


def test_missing_or_bad_physics_is_explicit_invalid():
    actor = Actor(1)
    assert not geometry(actor)["rear_valid"] and not geometry(actor)["bbox_valid"]
    measured(actor)
    actor.get_physics_control = lambda: NS(wheels=[])
    assert not geometry(actor)["rear_valid"]


@pytest.mark.parametrize("enabled,active", [(False, True), (None, True), (True, False)])
def test_geometry_never_queries_unsafe_vehicle_physics(enabled, active):
    actor = measured(Actor(1))
    actor.is_active = active
    actor.get_physics_control = lambda: pytest.fail("unsafe vehicle physics RPC issued")
    assert geometry(actor, physics_enabled=enabled)["physics_pending"]


def test_types_do_not_mix_bicycles_and_motorcycles():
    assert object_type(Actor(1, type_id="vehicle.bh.crossbike")) == 3
    assert object_type(Actor(2, type_id="vehicle.kawasaki.ninja")) == 4
    unknown = Actor(3)
    unknown.attributes = {"number_of_wheels": "2"}
    assert object_type(unknown) == 0
    assert [
        static_type(x) for x in ("construction_sign", "barrier", "trafficcone", "box")
    ] == [1, 2, 3, 4]


def test_signal_geometry_cache_keeps_each_tick_state_and_previous_frame():
    from easycarla.observations.frame import capture_frame
    from enum import IntEnum

    class LightState(IntEnum):
        Red = 0
        Green = 2

        def __str__(self):
            return self.name

    reads = []
    waypoint = NS(
        transform=NS(
            location=vector(10),
            get_forward_vector=lambda: vector(1),
            get_right_vector=lambda: vector(y=1),
        ),
        lane_width=4,
        road_id=1,
        section_id=0,
        lane_id=-1,
    )

    def waypoints():
        reads.append(1)
        return [waypoint]

    state = [LightState.Red]
    actor = NS(
        id=4,
        type_id="traffic.traffic_light",
        get_transform=lambda: NS(transform=lambda location: vector(10)),
        trigger_volume=NS(location=vector()),
        get_stop_waypoints=waypoints,
        get_state=lambda: state[0],
    )
    session = NS(world=NS(get_actors=lambda: [actor]))
    cache = {}
    first = capture_frame(session, snapshot(1, 0), signal_cache=cache)
    state[0] = LightState.Green
    second = capture_frame(session, snapshot(2, 0.05), signal_cache=cache)
    assert len(reads) == 1
    assert first.signals[0]["prohibited"] and first.signals[0]["state"] == 0
    assert not second.signals[0]["prohibited"] and second.signals[0]["state"] == 2
    assert first.signals[0]["stop_line"] is second.signals[0]["stop_line"]
    with pytest.raises(TypeError):
        second.signals[0]["state"] = 0


def wp(x, lane=-1, junction=False):
    return NS(
        road_id=1,
        section_id=0,
        lane_id=lane,
        s=x,
        is_junction=junction,
        lane_width=4,
        transform=NS(
            location=vector(x, 0),
            get_right_vector=lambda: vector(0, 1),
            get_forward_vector=lambda: vector(1, 0),
        ),
        next=lambda d: [],
        previous=lambda d: [],
        get_left_lane=lambda: None,
        get_right_lane=lambda: None,
    )


def world_map(points):
    return NS(
        name="FixtureTown",
        to_opendrive=lambda: "<OpenDRIVE/>",
        generate_waypoints=lambda d: points,
        get_waypoint=lambda location: min(points, key=lambda w: abs(w.s - location.x)),
    )


def test_directed_map_segments_route_order_and_unknown_limits():
    points = [wp(0), wp(2), wp(4, junction=True), wp(6, junction=True), wp(8)]
    points[1].next = lambda d: [points[2]]
    points[3].next = lambda d: [points[4]]
    m = world_map(points)
    static = build_planning_map(m)
    np.testing.assert_array_equal(static["offsets"], [0, 2, 4, 5])
    np.testing.assert_array_equal(static["connections"], [[0, 1], [1, 2]])
    assert static["is_junction"].tolist() == [False, True, False]
    assert not static["speed_limit_valid"].any()
    np.testing.assert_allclose(static["left_boundary"][:, 1], 2)
    assert not static["boundary_is_road_mesh"]
    route = [NS(location=vector(x)) for x in (0, 2, 4, 8, 4)]
    description = describe_route(NS(map=m), route, static)
    assert description["ordered_segments"].tolist() == [0, 1, 2, 1]
    assert description["corridor"].tolist() == [True, True, True]
    assert not static["center"].flags.writeable


def test_real_history_missing_masks_current_reference_and_fixed_spec(monkeypatch):
    ego, npc = measured(Actor(1)), measured(Actor(2, 10))
    actors = [ego, npc]
    registry = ActorRegistry()
    registry.register(ego, "ego")
    registry.register(npc, "npc")
    m = world_map([wp(i) for i in range(0, 102, 2)])
    # Legacy map is independently tested; isolate this fixture from CARLA LaneType.
    monkeypatch.setattr(
        "easycarla.observations.builder.build_map", lambda m: {"map_id": "legacy"}
    )
    session = NS(map=m, world=NS(get_actors=lambda: actors), registry=registry)
    episode = NS(ego=ego, route=[NS(location=vector(i)) for i in range(101)])
    cfg = validate_config(
        {
            "observations": {
                "profile": "planning_v1",
                "max_objects": 2,
                "history_frames": 3,
            }
        }
    )
    observer = ObservationBuilder(session, episode, cfg)
    observer.sample(snapshot(10, 0), 0)
    first = observer.build(snapshot(10, 0))
    assert not first["planning"]["history_ready"]
    ego.position.x = 5
    ego.yaw = 90
    npc.velocity = vector(3, 0)
    observer.sample(snapshot(12, 0.1), 2)
    newcomer = measured(Actor(3, 6, type_id="vehicle.bh.crossbike"))
    actors.append(newcomer)
    observer.sample(snapshot(14, 0.2), 4)
    obs = observer.build(snapshot(14, 0.2))
    p = obs["planning"]
    assert p["history_ready"] and p["history_coverage_seconds"] == pytest.approx(0.2)
    assert p["ego_history"]["valid"].tolist() == [True] * 3
    assert p["reference_valid"]
    np.testing.assert_allclose(p["reference_position_world"], [5, 1.5, 0], atol=1e-5)
    j = np.flatnonzero(p["objects"]["ids"] == observer.semantic_id(newcomer))[0]
    assert p["objects"]["valid"][j].tolist() == [False, False, True]
    j = np.flatnonzero(p["objects"]["ids"] == observer.semantic_id(npc))[0]
    np.testing.assert_allclose(p["objects"]["velocity"][j, -1], [0, 3, 0], atol=1e-6)
    assert not p["ego_history"]["acceleration_valid"][0]
    # New reference does not change legacy actor-origin fields.
    np.testing.assert_allclose(obs["ego"]["position_world"], [5, 0, 0])
    spec = observation_spec(cfg)
    space = numeric_space(spec)
    assert space.contains(project_observation(obs, spec))
    actors.remove(npc)
    observer.sample(snapshot(16, 0.3), 6)
    current = observer.build(snapshot(16, 0.3))
    assert observer.semantic_id(npc) not in current["planning"]["objects"]["ids"]
    assert len(observer.history) == 3


def test_static_bbox_center_orientation_and_unknown_signal_association(monkeypatch):
    from easycarla.observations.frame import capture_frame, FrozenMapping
    from easycarla.observations.planning import planning_observation

    ego = measured(Actor(1))
    cone = measured(Actor(10, 4, -2, yaw=90, type_id="static.prop.trafficcone01"))
    registry = ActorRegistry()
    registry.register(ego, "ego")
    registry.register(cone, "cone")
    session = NS(
        world=NS(get_actors=lambda: [ego, cone]),
        registry=registry,
        map=world_map([wp(0), wp(2)]),
    )
    captured = capture_frame(session, snapshot(1, 0), {})
    obstacle = captured.obstacles[0]
    np.testing.assert_allclose(obstacle["center"], [4, -(-2 + 0.5), 0.5], atol=1e-7)
    assert obstacle["type"] == 3 and obstacle["yaw"] == pytest.approx(-math.pi / 2)
    # _signals uses explicitly controlled lane tuples, not ego's encountered light.
    observer = object.__new__(ObservationBuilder)
    observer.config = {"max_signals": 2, "range": 100}
    observer.planning = True
    observer.static = build_planning_map(session.map)
    observer.frame_state = NS(
        signals=[
            {
                "trigger_position": [0, 0, 0],
                "actor_id": 9,
                "position": [0, 0, 0],
                "state": 0,
                "direction": [1, 0, 0],
                "stop_line": [[0, -2, 0], [0, 2, 0]],
                "lane_ids": [999, 0, -1],
            }
        ]
    )
    signals = observer._signals(np.zeros(3), 0)
    assert (
        signals["valid"][0]
        and not signals["association_valid"][0]
        and signals["segment_index"][0] == -1
    )
    observer.frame_state.signals[0]["lane_ids"] = [1, 0, -1]
    signals = observer._signals(np.zeros(3), 0)
    assert signals["association_valid"][0] and signals["segment_index"][0] == 0


def test_route_cache_no_repeated_waypoint_projection():
    from easycarla.observations.routes import ROUTE_CACHE

    ROUTE_CACHE.clear()
    m = world_map([wp(0), wp(2)])
    static = build_planning_map(m)
    route = [NS(location=vector(0)), NS(location=vector(2))]
    first = describe_route(NS(map=m), route, static)
    m.get_waypoint = lambda location: pytest.fail("cached route should not reproject")
    second = describe_route(NS(map=m), route, static)
    assert first["route_id"] == second["route_id"]
    assert not second["ordered_segments"].flags.writeable


def test_control_and_actual_wheel_angle_are_distinct_measurements():
    actor = measured(Actor(1))
    actor.get_wheel_steer_angle = lambda location: 12.0
    carla = NS(VehicleWheelLocation=NS(FL_Wheel=0, FR_Wheel=1))
    state = enrich(actor, actor, actor_state(actor), geometry(actor), carla)
    assert state["steering_valid"] and state["steering_angle"] == pytest.approx(
        -math.radians(12)
    )
    assert state["control"][0] == 0.1


def test_large_map_origin_is_measured_from_same_frame_heartbeat():
    from easycarla.observations.vehicle_geometry import world_origin_from_heartbeat

    actor = measured(Actor(1, 3447, 4788, yaw=-80))
    origin = np.array([3000, -5000, 0])
    packet = NS(frame=4, transform=NS(location=vector(447, -212), rotation=NS(yaw=-80)))
    snap = NS(frame=4, find=lambda aid: actor)
    measured_origin = world_origin_from_heartbeat(snap, 1, packet)
    np.testing.assert_allclose(measured_origin, origin)
    original = actor.get_physics_control

    def rebased():
        p = original()
        for wheel in p.wheels:
            wheel.position.x -= 3000 * 100
            wheel.position.y -= 5000 * 100
        return p

    actor.get_physics_control = rebased
    assert not geometry(actor)["rear_valid"]
    g = geometry(actor, measured_origin)
    assert g["rear_valid"] and g["wheelbase"] == pytest.approx(3)
    np.testing.assert_allclose(g["rear_offset"], [-1.5, 0, 0], atol=1e-8)
    packet.frame = 3
    with pytest.raises(ValueError, match="frames"):
        world_origin_from_heartbeat(snap, 1, packet)


def test_actual_steering_rpc_is_ego_only_on_hot_path():
    from easycarla.observations.frame import capture_frame

    ego, npc = measured(Actor(1)), measured(Actor(2, 10))
    calls = []
    ego.get_wheel_steer_angle = lambda loc: calls.append(("ego", loc)) or 0.0
    npc.get_wheel_steer_angle = lambda loc: pytest.fail(
        "neighbors do not need wheel steering RPCs"
    )
    session = NS(
        world=NS(get_actors=lambda: [ego, npc]),
        registry=ActorRegistry(),
        carla=NS(VehicleWheelLocation=NS(FL_Wheel=0, FR_Wheel=1)),
    )
    captured = capture_frame(session, snapshot(1, 0), {}, ego_id=1)
    assert calls == [("ego", 0), ("ego", 1)]
    assert (
        captured.actors[1]["steering_valid"]
        and not captured.actors[2]["steering_valid"]
    )


def test_mass_center_offset_used_for_planar_point_velocity():
    actor = measured(Actor(1))
    actor.angular_velocity = vector(z=-180)
    physics = actor.get_physics_control

    def shifted():
        p = physics()
        p.center_of_mass = vector(0.3, 0, -0.2)
        return p

    actor.get_physics_control = shifted
    state = enrich(actor, actor, actor_state(actor), geometry(actor))
    assert state["geometry"]["center_of_mass_valid"]
    np.testing.assert_allclose(
        state["rear_velocity"], [0, -math.pi * 1.8, 0], atol=1e-6
    )


def test_disabled_physics_rpc_is_skipped_then_geometry_recovers():
    from easycarla.observations.frame import capture_frame

    ego = measured(Actor(1))
    npc = measured(Actor(2))
    original = npc.get_physics_control
    calls = []

    def physics():
        calls.append(1)
        return original()

    npc.get_physics_control = physics
    registry = ActorRegistry()
    registry.register(ego, "ego")
    registry.register(npc, "npc")
    registry.physics_enabled[2] = False
    session = NS(world=NS(get_actors=lambda: [ego, npc]), registry=registry)
    cache = {}
    first = capture_frame(session, snapshot(1, 0), cache, ego_id=1)
    assert not calls and cache[2]["physics_pending"]
    assert first.actors[2]["geometry"]["bbox_valid"]
    assert not first.actors[2]["geometry"]["center_of_mass_valid"]
    registry.physics_enabled[2] = True
    npc.is_active = False
    capture_frame(session, snapshot(2, 0.05), cache, ego_id=1)
    assert not calls
    npc.is_active = True
    second = capture_frame(session, snapshot(3, 0.1), cache, ego_id=1)
    assert calls == [1] and second.actors[2]["geometry"]["center_of_mass_valid"]
    assert not first.actors[2]["geometry"]["center_of_mass_valid"]
    registry.forget(2)
    assert 2 not in registry.physics_enabled


def test_planning_actor_trajectory_does_not_guess_missing_geometry():
    from easycarla.control.trajectory import TrajectoryTracker

    tracker = TrajectoryTracker()
    trajectory = {
        "reference_frame": 1,
        "reference_point": "actor",
        "times": [0, 0.1],
        "points": [[0, 0, 0, 0], [0, 0, 0, 0]],
    }
    with pytest.raises(ValueError, match="measured vehicle geometry"):
        tracker.start(trajectory, 1, {"geometry": {"rear_valid": False}})
