"""Threaded Gymnasium vector client with explicit reset and partial failures."""

import numpy as np
import gymnasium as gym
from gymnasium.vector.utils import batch_space
from .gym_env import CarlaGymEnv, BatchedControlSpace
from .pool import ClientPool
from .errors import is_request_rejection


class BatchError(RuntimeError):
    def __init__(self, completed, failures):
        self.completed = completed
        self.failures = failures
        super().__init__(
            f"workers failed: {list(failures)}; completed transitions are in .completed"
        )


class CarlaVectorEnv(gym.vector.VectorEnv):
    def __init__(
        self, instances, *, timeout=30.0, action_mode="vehicle_control", envs=None
    ):
        self.envs = []
        self._waiting = None
        self._closed = False
        try:
            if envs is not None:
                self.envs = list(envs)
            else:
                for instance in instances:
                    self.envs.append(
                        CarlaGymEnv(instance, timeout=timeout, action_mode=action_mode)
                    )
            if not self.envs:
                raise ValueError("vector environment requires at least one worker")
            first = self.envs[0]
            for env in self.envs[1:]:
                if (
                    env.observation_space != first.observation_space
                    or env.action_space != first.action_space
                    or env.action_mode != first.action_mode
                    or env.capabilities["clocks"] != first.capabilities["clocks"]
                    or env.capabilities.get("trajectory_spec")
                    != first.capabilities.get("trajectory_spec")
                ):
                    raise ValueError(
                        "workers must have identical observation/action specifications and clocks"
                    )
            self.num_envs = len(self.envs)
            self.single_observation_space = first.observation_space
            self.single_action_space = first.action_space
            self.observation_space = batch_space(
                self.single_observation_space, self.num_envs
            )
            self.action_space = (
                BatchedControlSpace(self.num_envs)
                if first.action_mode == "vehicle_control"
                else batch_space(self.single_action_space, self.num_envs)
            )
            try:
                from gymnasium.vector import AutoresetMode

                mode = AutoresetMode.DISABLED
            except ImportError:
                mode = "Disabled"
            self.metadata = {"render_modes": [], "autoreset_mode": mode}
            self.pool = ClientPool({i: env.client for i, env in enumerate(self.envs)})
            self._last = [None] * self.num_envs
        except BaseException:
            for env in self.envs:
                env.close()
            raise

    def _idle(self):
        if self._closed:
            raise RuntimeError("vector environment is closed")
        if self._waiting is not None:
            raise RuntimeError("a vector request is pending")

    def _collect(self, operation, indices):
        pending = set(indices)
        completed = {}
        failures = {}
        while pending:
            for completion in self.pool.poll_ready(timeout=0.1):
                i = completion.worker_id
                pending.remove(i)
                if completion.error is not None:
                    if not is_request_rejection(completion.error):
                        self.envs[i]._state = "faulted"
                    failures[i] = completion.error
                    continue
                try:
                    if operation == "step":
                        result = self.envs[i].consume_step(completion.result)
                    else:
                        obs, info = self.envs[i]._result(*completion.result)
                        self.envs[i]._state = "ready"
                        result = (obs, info)
                    self._last[i] = result[0]
                    completed[i] = result
                except BaseException as exc:
                    self.envs[i]._state = "faulted"
                    failures[i] = exc
        if failures:
            raise BatchError(completed, failures)
        return completed

    def _stack(self):
        if any(value is None for value in self._last):
            raise RuntimeError("all workers need an initial reset")
        return {
            key: np.stack([o[key] for o in self._last])
            for key in self.single_observation_space.spaces
        }

    def _infos(self, results, reset=False):
        infos = {}
        for i, result in results.items():
            infos = self._add_info(infos, result[1] if reset else result[4], i)
        return infos

    def reset(self, *, seed=None, options=None, mask=None):
        self._idle()
        options = dict(options or {})
        if mask is None:
            mask = options.pop("reset_mask", np.ones(self.num_envs, bool))
        mask = np.asarray(mask)
        if mask.shape != (self.num_envs,) or mask.dtype != np.bool_:
            raise ValueError("reset mask must be bool[num_envs]")
        if isinstance(seed, (int, np.integer)) and not isinstance(
            seed, (bool, np.bool_)
        ):
            seeds = [int(seed) + i for i in range(self.num_envs)]
        elif seed is None:
            seeds = [None] * self.num_envs
        else:
            seeds = list(seed)
            if len(seeds) != self.num_envs:
                raise ValueError("seed sequence has wrong length")
        for value in seeds:
            if value is not None and (
                isinstance(value, (bool, np.bool_))
                or not isinstance(value, (int, np.integer))
                or value < 0
            ):
                raise ValueError("invalid reset seed")
        indices = np.flatnonzero(mask).tolist()
        if any(self._last[i] is None and not mask[i] for i in range(self.num_envs)):
            raise ValueError("initial reset must include every uninitialized worker")
        self.pool.submit_batch(
            {i: ("reset", (), {"seed": seeds[i], "options": options}) for i in indices}
        )
        results = self._collect("reset", indices)
        return self._stack(), self._infos(results, reset=True)

    def step_async(self, actions):
        self._idle()
        actions = np.asarray(actions)
        if actions.shape != self.action_space.shape:
            raise ValueError("batch action has wrong shape")
        # Validate every worker BEFORE the first request publication.
        prepared = [
            env.prepare_action(action) for env, action in zip(self.envs, actions)
        ]
        self.pool.submit_batch(dict(enumerate(prepared)))
        self._waiting = list(range(self.num_envs))

    def step_wait(self):
        if self._waiting is None:
            raise RuntimeError("step_async is required")
        indices = self._waiting
        try:
            results = self._collect("step", indices)
            return (
                self._stack(),
                np.asarray([results[i][1] for i in indices], np.float64),
                np.asarray([results[i][2] for i in indices], bool),
                np.asarray([results[i][3] for i in indices], bool),
                self._infos(results),
            )
        finally:
            self._waiting = None

    def step(self, actions):
        self.step_async(actions)
        return self.step_wait()

    def replace_worker(self, index, env):
        self._idle()
        old = self.envs[index]
        if env is old or env.client is old.client:
            raise ValueError("replacement must use a different environment and client")
        if any(env is other or env.client is other.client
               for i, other in enumerate(self.envs) if i != index):
            raise ValueError("replacement already belongs to another worker")
        if (
            env.observation_space != self.single_observation_space
            or env.action_space != self.single_action_space
            or env.action_mode != self.envs[index].action_mode
            or env.capabilities["clocks"] != self.envs[index].capabilities["clocks"]
            or env.capabilities.get("trajectory_spec")
            != self.envs[index].capabilities.get("trajectory_spec")
        ):
            raise ValueError("replacement worker specification differs")
        self.pool.replace(index, env.client)
        self.envs[index] = env
        env._state = "needs_reset"
        self._last[index] = None
        old.close()

    def close(self, **kwargs):
        if self._closed:
            return
        self._closed = True
        errors = []
        self.pool.close()
        for env in self.envs:
            try:
                env.close()
            except Exception as exc:
                errors.append(exc)
        if errors:
            raise RuntimeError(f"vector cleanup failures: {errors}")
