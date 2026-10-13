"""Runtime contracts using deterministic fake world/sensors; no CARLA physics."""
from collections import Counter
from types import SimpleNamespace as NS
import time

import numpy as np
import pytest

import easycarla.runtime as runtime_module
from easycarla.runtime import EnvironmentFault, EnvironmentRuntime
from easycarla.simulation.resources import ActorRegistry
from easycarla.tasks import DrivingTask
from easycarla.config import validate_config
from easycarla.control.gateway import ControlGateway
from easycarla.scenarios.contracts import ResolvedControl, ScenarioFacts


def control(**kwargs):
    return NS(**dict({"steer": 0.0, "throttle": 0.0, "brake": 0.0}, **kwargs))


def vector(x=0.0, y=0.0, z=0.0):
    return NS(x=x, y=y, z=z)


class Ego:
    id = 10
    type_id = "vehicle.fake"
    is_alive = True
    bounding_box = NS(extent=vector(2, 1, .75))

    def __init__(self):
        self.control = control()
        self.writes = []

    def get_transform(self):
        return NS(location=vector(), rotation=NS(yaw=0))

    def get_velocity(self):
        return vector()

    def get_acceleration(self):
        return vector()

    def get_angular_velocity(self):
        return vector()

    def get_control(self):
        return self.control

    def get_speed_limit(self):
        return 36

    def apply_control(self, value):
        self.control = value
        self.writes.append(value)

    def disable_constant_velocity(self):
        pass

    def destroy(self):
        self.is_alive = False


class FakeSession:
    def __init__(self, config):
        self.sim = config["simulation"]
        self.effective_world_settings = {}
        self.carla = NS(VehicleControl=control, command=NS(
            SetAutopilot=lambda actor_id, *args: ("autopilot", actor_id, args),
            ApplyVehicleControl=lambda actor_id, value: ("control", actor_id, value)))
        self.client = NS(apply_batch_sync=self.batch)
        self.controls = ControlGateway(self)
        self.registry = ActorRegistry(on_forget=self.controls.forget)
        self.ticks = Counter()
        self.timings = Counter()
        self.frame = 100
        self.load_count = 0
        self.closed = False
        self.fail_tick = False

    def batch(self, commands, do_tick):
        assert do_tick is False
        for kind, actor_id, value in commands:
            if kind == "control":
                self.registry.actors[actor_id].apply_control(value)
        return [NS(error="") for _ in commands]

    def load(self, town, seed):
        self.controls.clear()
        self.frame = 100
        self.ticks.clear()
        self.timings.clear()
        self.load_count += 1

    def tick(self, phase="step"):
        self.controls.flush()
        self.controls.after_tick()
        self.frame += 1
        self.ticks[phase] += 1
        self.timings["simulation_seconds"] += .001
        if self.fail_tick:
            raise RuntimeError("Injected world fault after physical advancement")
        return NS(frame=self.frame, timestamp=NS(elapsed_seconds=self.frame * .05))

    def close(self):
        self.registry.clear()
        self.closed = True


class FakeEpisode:
    task_profiles = frozenset({"driving", "bench2drive_native"})
    route = ["fake-route"]

    def resolve_control(self, requested):
        return ResolvedControl(requested)

    def scenario_facts(self):
        return ScenarioFacts()

    def export_state(self, roles):
        return {"schema_version": 1}

    def takeover(self, role):
        raise ValueError(role)

    def __init__(self, session, seed):
        self.ego = session.registry.register(Ego(), "ego")
        self.metadata = {"source": "fake_fixture", "scenario_id": "unit", "seed": seed}
        self.controllers = {}
        self.before_count = 0
        self.after_count = 0
        self.close_count = 0
        self.native = {}

    def before_tick(self):
        self.before_count += 1

    def after_tick(self, snapshot):
        self.after_count += 1

    def info(self):
        return dict(self.native)

    def capabilities(self):
        return {"fake": True}

    def close(self):
        self.close_count += 1


class FakeAdapter:
    def town(self, seed, options):
        return "FakeTown"

    def prepare(self, session, seed, options):
        return FakeEpisode(session, seed)


class FakeSensors:
    def __init__(self, session, ego, config, generation):
        self.collisions = []
        self.frames = []
        self.fail = False
        self.closed = False

    def collect(self, frame):
        if self.fail:
            raise RuntimeError("Injected missing sensor frame")
        self.frames.append(frame)
        return {"fixture": {"frame": frame}}

    def assert_healthy(self):
        if self.fail:
            raise RuntimeError("Injected missing sensor frame")

    def commit(self, frame):
        self.assert_healthy()

    def close(self):
        self.closed = True


class FakeObserver:
    def __init__(self, session, episode, config):
        self.static = {"map_id": "fake-map"}
        self.samples = []
        self.episode = episode
        self.frame_state = NS(actors={})

    def get_state(self, actor, **kwargs):
        from easycarla.observations import actor_state
        return actor_state(actor)

    def sample(self, snapshot, tick):
        self.samples.append((snapshot.frame, tick))

    def build(self, snapshot, sensors):
        return {"frame": snapshot.frame, "timestamp": snapshot.timestamp.elapsed_seconds,
                "sensors": sensors, "route": {"progress": float(snapshot.frame - 100)},
                "ego": {"acceleration": np.zeros(3), "speed": 0.0, "speed_limit": 10.0}}


@pytest.fixture
def runtime_factory(monkeypatch):
    monkeypatch.setattr(runtime_module, "WorldSession", FakeSession)
    monkeypatch.setattr(runtime_module, "TrafficRules", lambda *args: NS(update=lambda frame: {}))
    monkeypatch.setattr(runtime_module, "SensorSuite", FakeSensors)
    monkeypatch.setattr(runtime_module, "ObservationBuilder", FakeObserver)
    monkeypatch.setattr(runtime_module, "select_source", lambda cfg, seed, opts: (FakeAdapter(), {"source": cfg["source"]}))
    runtimes = []
    def create(config=None):
        env = EnvironmentRuntime(config or {})
        env._facts = lambda: {}
        runtimes.append(env)
        return env
    yield create
    for env in runtimes:
        env.shutdown()


@pytest.mark.parametrize("action", [[0, 0], [2, 0, 0], [0, -.1, 0], [0, .1, .1], [float("nan"), 0, 0]])
def test_invalid_action_does_not_advance(runtime_factory, action):
    env = runtime_factory()
    env.reset(seed=1)
    before = env.session.frame
    with pytest.raises(ValueError):
        env.step(action)
    assert env.session.frame == before
    assert env.state == "ready"
    assert env.physical_ticks == 0


@pytest.mark.parametrize("kwargs", [{"options": []}, {"options": "route"}, {"seed": True}, {"seed": 1.5}])
def test_invalid_reset_preserves_the_active_episode(runtime_factory, kwargs):
    env = runtime_factory()
    obs, _ = env.reset(seed=1)
    episode = env.episode
    with pytest.raises(runtime_module.RequestRejected):
        env.reset(**kwargs)
    assert env.episode is episode and env.state == "ready"
    assert env.session.frame == obs["frame"]


@pytest.mark.parametrize("error", [ValueError("bad selection"), TypeError("bad option"), KeyError("route")])
def test_reset_source_preflight_errors_are_explicit_rejections(runtime_factory, monkeypatch, error):
    env = runtime_factory()
    obs, _ = env.reset(seed=1)
    episode = env.episode

    def reject(*args):
        raise error

    monkeypatch.setattr(runtime_module, "select_source", reject)
    with pytest.raises(runtime_module.RequestRejected):
        env.reset(seed=2)
    assert env.episode is episode and env.state == "ready" and env.session.frame == obs["frame"]


def test_four_ticks_per_decision_and_idle_queries_do_not_tick(runtime_factory):
    env = runtime_factory()
    first, info = env.reset(seed=7)
    assert info["tick_counts"] == {"initialization": 1}
    frame = first["frame"]
    time.sleep(.005)
    env.capabilities()
    env.get_static()
    assert env.session.frame == frame
    obs, reward, terminated, truncated, info = env.step([.1, .2, 0])
    assert obs["frame"] == frame + 4
    assert info["physical_ticks"] == 4 and info["executed_seconds"] == pytest.approx(.2)
    assert env.elapsed == pytest.approx(.2)
    assert info["tick_counts"] == {"initialization": 1, "step": 4}
    assert info["timing"]["simulation_seconds"] == pytest.approx(.004)
    assert len(info["actual_controls"]) == 4
    assert env.sensors.frames == list(range(frame, frame + 5))
    assert env.episode.before_count == 4
    assert not terminated and not truncated
    assert first["frame"] == frame


def test_midwindow_terminal_returns_final_frame_without_reset(runtime_factory):
    env = runtime_factory()
    first, _ = env.reset()
    episode = env.episode
    env._facts = lambda: {"collision": env.physical_ticks >= 2}
    obs, reward, terminated, truncated, info = env.step([0, .2, 0])
    assert terminated and not truncated
    assert obs["frame"] == first["frame"] + 2 == info["final_frame"]
    assert obs["sensors"]["fixture"]["frame"] == obs["frame"]
    assert info["executed_seconds"] == pytest.approx(.1)
    assert info["physical_ticks"] == 2
    assert info["cost_components"]["collision"] == 1
    assert env.state == "done" and env.episode is episode and env.session.load_count == 1
    with pytest.raises(RuntimeError, match="reset"):
        env.step([0, 0, 0])
    assert env.session.frame == obs["frame"]


def test_natural_termination_takes_priority_on_timeout_tick(runtime_factory):
    env = runtime_factory({"task": {"max_seconds": .1}})
    env.reset()
    env._facts = lambda: {"goal": env.physical_ticks == 2}
    obs, reward, terminated, truncated, info = env.step([0, .2, 0])
    assert terminated and not truncated
    assert info["termination_reason"] == "goal"
    assert info["reward_components"]["goal"] == 10
    assert info["executed_seconds"] == pytest.approx(.1)


def test_timeout_has_final_observation_and_is_only_truncation(runtime_factory):
    env = runtime_factory({"task": {"max_seconds": .15}})
    initial, _ = env.reset()
    obs, _, terminated, truncated, info = env.step([0, 0, 1])
    assert truncated and not terminated
    assert obs["frame"] == initial["frame"] + 3
    assert info["termination_reason"] == "timeout"


@pytest.mark.parametrize("failure", ["world", "sensor", "ego"])
def test_failures_never_become_training_transitions(runtime_factory, failure):
    env = runtime_factory()
    env.reset()
    if failure == "world":
        env.session.fail_tick = True
    elif failure == "sensor":
        env.sensors.fail = True
    else:
        env.episode.ego.is_alive = False
    with pytest.raises(EnvironmentFault):
        env.step([0, .2, 0])
    assert env.state == "faulted"
    with pytest.raises(RuntimeError):
        env.step([0, 0, 0])


def test_cleanup_and_reset_share_one_runtime(runtime_factory):
    env = runtime_factory()
    env.reset()
    episode, sensors, session = env.episode, env.sensors, env.session
    env.close_episode()
    assert episode.close_count == 1 and sensors.closed
    assert not session.closed and env.state == "idle"
    env.reset(seed=9)
    assert env.session is session and session.load_count == 2
    env.shutdown()
    assert session.closed and env.session is None


def test_sensor_cleanup_failure_does_not_skip_episode_and_actor_cleanup(runtime_factory):
    env = runtime_factory()
    env.reset()
    episode = env.episode
    def fail_close():
        raise RuntimeError("sensor close refused")
    env.sensors.close = fail_close
    with pytest.raises(EnvironmentFault, match="sensor close refused"):
        env.close_episode()
    assert episode.close_count == 1
    assert not env.session.registry.actors
    assert env.state == "faulted"


def test_invalid_scenario_contract_reclaims_partially_prepared_actors(runtime_factory, monkeypatch):
    env = runtime_factory()
    original = FakeAdapter.prepare
    def broken_prepare(self, session, seed, options):
        episode = original(self, session, seed, options)
        episode.route = []
        return episode
    monkeypatch.setattr(FakeAdapter, "prepare", broken_prepare)
    with pytest.raises(EnvironmentFault, match="nonempty route"):
        env.reset()
    assert env.state == "faulted"
    assert not env.session.registry.actors


def test_scene_control_conflict_faults_before_physics(runtime_factory):
    env = runtime_factory()
    obs, _ = env.reset()
    def steal():
        env.session.controls.apply_control(env.episode.ego, [0, 1, 0], owner="unrelated_source")
    env.episode.before_tick = steal
    with pytest.raises(EnvironmentFault, match="controlled by"):
        env.step([0, .2, 0])
    assert env.session.frame == obs["frame"]
    assert env.state == "faulted"


def test_reward_integrates_time_and_progress_and_deduplicates_events():
    cfg = validate_config({})["task"]
    task = DrivingTask(cfg)
    observation = {"route": {"progress": 0.0}, "ego": {"acceleration": np.zeros(3), "speed": 12, "speed_limit": 10}}
    task.reset(observation)
    all_terms = Counter()
    for i in range(4):
        observation["route"]["progress"] = i + 1.0
        _, _, _, info = task.evaluate(observation, {"signal": True}, .05, (i + 1) * .05)
        all_terms.update(info["reward_components"])
    assert all_terms["progress"] == pytest.approx(.4)
    assert all_terms["time"] == pytest.approx(-.02)
    assert all_terms["signal"] == -10
    assert all_terms["overspeed"] == pytest.approx(-.008)


def test_native_task_obeys_native_lifecycle_instead_of_rl_collision():
    task = DrivingTask(validate_config({"task": {"profile": "bench2drive_native"}})["task"])
    obs = {"route": {"progress": 0.0}, "ego": {"acceleration": np.zeros(3), "speed": 0, "speed_limit": 10}}
    task.reset(obs)
    _, term, trunc, _ = task.evaluate(obs, {"collision": True}, .05, .05)
    assert not term and not trunc
    _, term, trunc, _ = task.evaluate(obs, {"native_done": True}, .05, .1)
    assert term and not trunc


def test_collision_only_runtime_remains_ready_after_offroad_goal_and_timeout(runtime_factory):
    env = runtime_factory({"task": {"max_seconds": .01,
        "termination": {"offroad": False, "goal": False, "timeout": False}}})
    env.reset()
    first = [True]
    def facts():
        goal = first.pop() if first else False
        return {"offroad": True, "goal": goal}
    env._facts = facts
    _, _, term, trunc, info = env.step([0, .2, 0])
    assert not term and not trunc and env.state == "ready"
    assert info["goal_reached"] and info["cost_components"]["offroad"] == 1
    env.step([0, .2, 0])
    env._facts = lambda: {"collision": True}
    _, _, term, trunc, info = env.step([0, .2, 0])
    assert term and not trunc and env.state == "done"
    assert info["termination_reason"] == "collision"


def test_jerk_uses_inertial_acceleration_when_ego_heading_changes():
    task = DrivingTask(validate_config({})["task"])
    first = {"ego": {"acceleration": [1., 0., 0.], "yaw_world": 0., "speed": 1., "speed_limit": 10.},
             "route": {"progress": 0.}}
    task.reset(first)
    # The same inertial acceleration expressed in a frame rotated 90 degrees.
    rotated = {"ego": {**first["ego"], "acceleration": [0., -1., 0.], "yaw_world": np.pi/2},
               "route": first["route"]}
    _, _, _, info = task.evaluate(rotated, {}, .05, .05)
    assert abs(info["reward_components"]["jerk"]) < 1e-12


def test_disabled_experiment_is_rejection_without_mutating_episode(runtime_factory):
    env = runtime_factory()
    env.reset()
    frame, episode = env.snapshot.frame, env.episode
    with pytest.raises(runtime_module.RequestRejected, match="disabled"):
        env.experiment("raw_state")
    assert env.state == "ready" and env.episode is episode
    assert env.snapshot.frame == frame and env.physical_ticks == 0
    assert env.step([0, .2, 0])[0]["frame"] == frame + 4


def test_prefix_records_each_physical_control_without_extra_ticks(runtime_factory):
    env = runtime_factory()
    env.reset()
    recorded = []
    env.raw_state = lambda: {"diagnostic": True}
    env.experiments.recorder = NS(ensure_capacity=lambda: None, append=lambda command, state: recorded.append((command, state)))
    obs, _, _, _, info = env.step([.1, .2, 0])
    command = recorded[0][0]
    assert command["executed_seconds"] == .2
    assert [item["tick"] for item in command["executed_controls"]] == [1, 2, 3, 4]
    for item in command["executed_controls"]:
        np.testing.assert_allclose(item["applied"], [.1, .2, 0])
        assert item["managed_npcs"] == {}
    assert info["physical_ticks"] == 4


def test_prefix_capacity_rejection_preserves_trajectory_controller_state(runtime_factory):
    from easycarla.extensions import PrefixRecorder, PrefixCapacityError
    env = runtime_factory()
    env.reset(seed=1)
    recorder = PrefixRecorder(max_commands=1)
    recorder.start({}, {})
    recorder.append({}, {})
    env.experiments.recorder = recorder
    before = env.tracker.state_dict()
    frame = env.snapshot.frame
    trajectory = {"reference_frame": frame, "times": [0, .2],
                  "points": [[0, 0, 0, 1], [.2, 0, 0, 1]]}
    with pytest.raises(PrefixCapacityError):
        env.step_trajectory(trajectory, .2)
    assert env.snapshot.frame == frame
    assert env.tracker.state_dict() == before
    assert env.state == "ready"
