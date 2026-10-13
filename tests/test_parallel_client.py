"""Real POSIX services: vector validation, partial failure, explicit reset."""

import signal
import time
import uuid
import numpy as np
import pytest
from gymnasium.utils.env_checker import check_env
from test_ipc import launch
from easycarla_client import CarlaEnvClient, UnknownOutcomeError, ClientPool, RemoteError
from easycarla_client.gym_env import CarlaGymEnv
from easycarla_client.vector_env import CarlaVectorEnv, BatchError


@pytest.fixture
def workers():
    owned = []

    def start(config=None):
        name = "parallel_" + uuid.uuid4().hex
        process, stop = launch(name, config)
        owned.append((name, process, stop))
        return name, process

    yield start
    from easycarla_client.transport import cleanup_instance

    for name, process, stop in owned:
        stop.set()
        process.join(5)
        if process.is_alive():
            process.kill()
            process.join(5)
        cleanup_instance(name)


def test_gym_fixed_space_and_terminal(workers):
    name, _ = workers({"terminal_frame": 8})
    env = CarlaGymEnv(name)
    try:
        space = env.observation_space
        check_env(env, skip_render_check=True, skip_close_check=True)
        obs, _ = env.reset(seed=1)
        assert env.observation_space is space and space.contains(obs)
        env.step([0, 0, 1])
        obs, _, term, _, info = env.step([0, 0, 1])
        assert term and int(obs["frame"]) == 8 and env.last_observation is obs
        with pytest.raises(RuntimeError):
            env.step([0, 0, 1])
        obs, _ = env.reset()
        assert int(obs["frame"]) == 0
    finally:
        env.close()


def test_vector_prevalidation_and_mask_reset(workers):
    a, _ = workers({"terminal_frame": 4})
    b, _ = workers({"terminal_frame": 8})
    env = CarlaVectorEnv([a, b])
    try:
        obs, _ = env.reset(seed=7)
        with pytest.raises(ValueError):
            env.step([[0, 0, 1], [0, 1, 1]])
        assert env.envs[0].client.experiment.diagnostics()["total_steps"] == 0
        env.step_async([[0, 0, 1], [0, 0, 1]])
        with pytest.raises(RuntimeError):
            env.step_async([[0, 0, 1], [0, 0, 1]])
        obs, _, term, _, _ = env.step_wait()
        assert term.tolist() == [True, False]
        obs, info = env.reset(mask=np.array([True, False]), seed=10)
        assert obs["frame"].tolist() == [0, 4]
        obs, _, term, _, _ = env.step([[0, 0, 1], [0, 0, 1]])
        assert term.all()
        assert env.observation_space.contains(obs)
    finally:
        env.close()


def test_partial_failure_keeps_healthy_transition(workers):
    a, _ = workers()
    b, p = workers({"delay": 0.8})
    env = CarlaVectorEnv([a, b], timeout=2.0)
    try:
        env.reset()
        env.step_async([[0, 0, 1], [0, 0, 1]])
        time.sleep(0.1)
        p.kill()
        p.join(3)
        with pytest.raises(BatchError) as error:
            env.step_wait()
        assert int(error.value.completed[0][0]["frame"]) == 4
        assert isinstance(error.value.failures[1], UnknownOutcomeError)
        assert env.envs[0]._state == "ready" and env.envs[1]._state == "faulted"
        with pytest.raises(RuntimeError):
            env.step([[0, 0, 1], [0, 0, 1]])
        assert env.envs[0].client.experiment.diagnostics()["total_steps"] == 1
    finally:
        env.close()


def test_pool_completion_order_and_failure_replacement(workers):
    a, _ = workers({"delay": 0.5})
    b, _ = workers()
    with CarlaEnvClient(a) as slow, CarlaEnvClient(b) as fast:
        slow.reset()
        fast.reset()
        with ClientPool({"slow": slow, "fast": fast}) as pool:
            pool.submit("slow", "step", [0, 0, 1])
            pool.submit("fast", "step", [0, 0, 1])
            with pytest.raises(RuntimeError):
                pool.submit("slow", "step", [0, 0, 1])
            ready = pool.poll_ready(timeout=0.3)
            assert [r.worker_id for r in ready] == ["fast"]
            assert ready[0].error is None
            ready = pool.poll_ready(timeout=2.0)
            assert [r.worker_id for r in ready] == ["slow"]


def test_spec_mismatch_closes_only_owned_clients(workers):
    a, _ = workers()
    b, _ = workers()
    e0 = CarlaGymEnv(a)
    e1 = CarlaGymEnv(b)
    e1.capabilities["clocks"]["decision_dt"] = 0.1
    with pytest.raises(ValueError):
        CarlaVectorEnv([], envs=[e0, e1])
    assert e0.client._closed and e1.client._closed


def test_pool_retains_completion_order_when_both_results_are_ready():
    from threading import Event
    from types import SimpleNamespace

    release = Event()

    def slow_step(action):
        assert release.wait(3)
        return "slow"

    with ClientPool(
        {
            "slow": SimpleNamespace(step=slow_step),
            "fast": SimpleNamespace(step=lambda action: "fast"),
        }
    ) as pool:
        pool.submit("slow", "step", [0, 0, 1])
        pool.submit("fast", "step", [0, 0, 1])
        deadline = time.monotonic() + 3
        while pool._ready.qsize() < 1 and time.monotonic() < deadline:
            time.sleep(0.005)
        assert pool._ready.qsize() == 1
        release.set()
        while pool._ready.qsize() < 2 and time.monotonic() < deadline:
            time.sleep(0.005)
        results = pool.poll_ready()
        assert [item.worker_id for item in results] == ["fast", "slow"]
        assert [item.result for item in results] == ["fast", "slow"]


def test_trajectory_checks_negative_speed_before_float32_rounding():
    from gymnasium.spaces import Box

    env = object.__new__(CarlaGymEnv)
    env._state = "ready"
    env.action_mode = "trajectory"
    env.action_space = Box(-np.inf, np.inf, shape=(3, 4), dtype=np.float32)
    points = np.zeros((3, 4), np.float64)
    points[0, 3] = -1e-50
    with pytest.raises(ValueError):
        env.prepare_action(points)


def test_pool_rejects_same_client_replacement_without_changing_worker():
    from types import SimpleNamespace

    client = SimpleNamespace(step=lambda action: "original")
    with ClientPool({0: client}) as pool:
        with pytest.raises(ValueError, match="different client"):
            pool.replace(0, client)
        assert pool.clients[0] is client and not pool._pending and not pool._failed
        pool.submit(0, "step", [0, 0, 1])
        result = pool.poll_ready(timeout=3)[0]
        assert result.error is None and result.result == "original"


def test_pool_copies_trajectory_and_nested_reset_options_before_submission():
    from threading import Event

    entered, release = Event(), Event()

    class Client:
        def step_trajectory(self, trajectory, execute_seconds=None):
            entered.set()
            assert release.wait(3)
            return trajectory

        def reset(self, *, seed=None, options=None):
            entered.set()
            assert release.wait(3)
            return options

    trajectory = {"reference_frame": 1, "times": np.array([0., 1.]),
                  "points": np.zeros((2, 4)), "extra": {"value": [1]}}
    with ClientPool({0: Client()}) as pool:
        try:
            pool.submit(0, "step_trajectory", trajectory)
            assert entered.wait(3)
            trajectory["points"][0, 0] = 99
            trajectory["times"][1] = 9
            trajectory["extra"]["value"][0] = 9
        finally:
            release.set()
        result = pool.poll_ready(timeout=3)[0].result
        assert result["points"][0, 0] == 0 and result["times"][1] == 1
        assert result["extra"]["value"] == [1]
        entered.clear()
        release.clear()
        options = {"params": {"array": np.array([1]), "list": [2]}}
        try:
            pool.submit(0, "reset", seed=1, options=options)
            assert entered.wait(3)
            options["params"]["array"][0] = 9
            options["params"]["list"][0] = 9
        finally:
            release.set()
        result = pool.poll_ready(timeout=3)[0].result
        assert result["params"]["array"][0] == 1 and result["params"]["list"] == [2]


@pytest.mark.parametrize("changes,duration", [
    ({"reference_frame": True}, None), ({"reference_frame": -1}, None),
    ({"reference_point": "center"}, None), ({"times": [0, 0]}, None),
    ({"times": [1, 2]}, None), ({"points": [[0, 0, 0, -1], [0, 0, 0, 0]]}, None),
    ({"points": [[0, 0, np.inf, 0], [0, 0, 0, 0]]}, None),
    ({"points": [[0, 0, 0, 0]]}, None), ({}, 2.), ({}, np.nan),
])
def test_pool_invalid_trajectory_batch_publishes_nothing(changes, duration):
    calls = []

    class Client:
        def step(self, action):
            calls.append("step")

        def step_trajectory(self, trajectory, execute_seconds=None):
            calls.append("trajectory")

    trajectory = {"reference_frame": 1, "times": [0, 1], "points": np.zeros((2, 4)), **changes}
    with ClientPool({0: Client(), 1: Client()}) as pool:
        with pytest.raises(ValueError):
            pool.submit_batch({0: ("step", ([0, 0, 1],), {}),
                               1: ("step_trajectory", (trajectory,), {"execute_seconds": duration})})
        assert calls == [] and pool._pending == {}


def test_pool_invalid_call_signature_does_not_submit_healthy_worker():
    from types import SimpleNamespace
    calls = []
    with ClientPool({0: SimpleNamespace(step=lambda action: calls.append(0)),
                     1: SimpleNamespace(reset=lambda: calls.append(1))}) as pool:
        with pytest.raises(TypeError):
            pool.submit_batch({0: ("step", ([0, 0, 1],), {}), 1: ("reset", (), {"seed": 1})})
        assert calls == [] and pool._pending == {}


def test_trajectory_float32_overflow_rejects_whole_vector_batch(workers):
    from gymnasium.spaces import Box
    a, _ = workers()
    b, _ = workers()
    env = CarlaVectorEnv([a, b])
    try:
        env.reset()
        env.action_space = Box(-np.inf, np.inf, shape=(2, 3, 4), dtype=np.float32)
        for single in env.envs:
            single.action_mode = "trajectory"
            single.action_space = Box(-np.inf, np.inf, shape=(3, 4), dtype=np.float32)
            single._trajectory = {"reference_point": "actor", "dt": .1}
        actions = np.zeros((2, 3, 4), np.float64)
        actions[1, 0, 0] = 1e100
        before = [single.client._sequence for single in env.envs]
        with pytest.raises(ValueError, match="float32"):
            env.step_async(actions)
        assert [single.client._sequence for single in env.envs] == before
        assert env.pool._pending == {} and env._waiting is None
    finally:
        env.close()


@pytest.mark.parametrize("kind", ["self", "same_client", "other_worker"])
def test_worker_replacement_rejects_aliases_without_changing_state(workers, kind):
    from copy import copy
    a, _ = workers()
    b, _ = workers()
    env = CarlaVectorEnv([a, b])
    try:
        env.reset()
        old = env.envs[0]
        last = env._last[0]
        replacement = old if kind == "self" else copy(old) if kind == "same_client" else env.envs[1]
        with pytest.raises(ValueError, match="replacement"):
            env.replace_worker(0, replacement)
        assert env.envs[0] is old and env.pool.clients[0] is old.client
        assert env._last[0] is last and old._state == "ready" and not old.client._closed
        env.step([[0, 0, 1], [0, 0, 1]])
    finally:
        env.close()


@pytest.mark.parametrize("already_ready", [False, True])
def test_legal_worker_replacement_requires_reset(workers, already_ready):
    a, _ = workers()
    b, _ = workers()
    env = CarlaVectorEnv([a])
    replacement = CarlaGymEnv(b)
    try:
        env.reset()
        if already_ready:
            replacement.reset()
        old = env.envs[0]
        env.replace_worker(0, replacement)
        assert old.client._closed and env._last[0] is None
        with pytest.raises(RuntimeError, match="reset"):
            env.step([[0, 0, 1]])
        env.reset()
        assert env.step([[0, 0, 1]])[0]["frame"].tolist() == [4]
    finally:
        env.close()
        replacement.close()


@pytest.mark.parametrize("kwargs", [{"seed": True}, {"options": []}, {"options": {"reject": True}}])
def test_ipc_reset_rejection_preserves_episode(workers, kwargs):
    name, _ = workers({"reject_reset": True})
    with CarlaEnvClient(name) as client:
        client.reset(seed=1)
        client.step([0, 0, 1])
        before = client.experiment.diagnostics()
        with pytest.raises(RemoteError) as caught:
            client.reset(**kwargs)
        assert caught.value.code == "invalid_request"
        assert caught.value.details["request_rejected"] is True
        assert client.experiment.diagnostics() == before
        assert int(client.step([0, 0, 1])[0]["frame"]) == 8


def test_gym_reset_rejection_keeps_state_and_observation(workers):
    name, _ = workers({"reject_reset": True})
    env = CarlaGymEnv(name)
    try:
        env.reset(seed=1)
        obs = env.step([0, 0, 1])[0]
        with pytest.raises(RemoteError):
            env.reset(options={"reject": True})
        assert env._state == "ready" and env.last_observation is obs and env._frame == 4
        assert int(env.step([0, 0, 1])[0]["frame"]) == 8
    finally:
        env.close()


def test_vector_reset_rejection_does_not_disable_worker(workers):
    name, _ = workers({"reject_reset": True})
    env = CarlaVectorEnv([name])
    try:
        env.reset(seed=1)
        obs = env._last[0]
        with pytest.raises(BatchError) as caught:
            env.reset(options={"reject": True})
        assert caught.value.failures[0].details["request_rejected"]
        assert env.envs[0]._state == "ready" and env._last[0] is obs
        assert env.pool._failed == set()
        assert env.step([[0, 0, 1]])[0]["frame"].tolist() == [4]
        env.reset(seed=2)
    finally:
        env.close()


def test_reset_initialization_failure_still_cleans_and_faults(workers):
    name, _ = workers({"reject_reset": True})
    env = CarlaGymEnv(name)
    try:
        env.reset(seed=1)
        with pytest.raises(RemoteError) as caught:
            env.reset(options={"fail_initialization": True})
        assert caught.value.details["episode_faulted"] and env._state == "faulted"
        assert not env.client.experiment.diagnostics()["active"]
        assert "request_rejected" not in caught.value.details
        env.reset(seed=2)
        assert env.step([0, 0, 1])[0]["frame"] == 4
    finally:
        env.close()
