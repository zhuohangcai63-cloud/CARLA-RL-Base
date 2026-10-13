"""Optional Gymnasium facade. This module imports no simulator or service code."""

import copy
import numpy as np
import gymnasium as gym
from .client import CarlaEnvClient
from .pool import validate_control
from .errors import is_request_rejection


class ControlSpace(gym.spaces.Box):
    """The existing three controls, excluding simultaneous throttle and brake."""

    def __init__(self):
        super().__init__(np.array([-1, 0, 0], np.float32), np.ones(3, np.float32))

    def sample(self, mask=None, probability=None):
        value = super().sample(mask=mask)
        value[1 if self.np_random.integers(2) else 2] = 0
        return value

    def contains(self, value):
        return super().contains(value) and not (value[1] > 0 and value[2] > 0)


class BatchedControlSpace(gym.spaces.Box):
    def __init__(self, count):
        super().__init__(
            np.tile([-1, 0, 0], (count, 1)).astype(np.float32),
            np.ones((count, 3), np.float32),
        )

    def sample(self, mask=None, probability=None):
        values = super().sample(mask=mask)
        columns = self.np_random.integers(1, 3, size=len(values))
        values[np.arange(len(values)), columns] = 0
        return values

    def contains(self, value):
        return super().contains(value) and not np.any(
            (np.asarray(value)[:, 1] > 0) & (np.asarray(value)[:, 2] > 0)
        )


def numeric_space(spec):
    spaces = {}
    for name, field in spec["fields"].items():
        dtype = np.dtype(field["dtype"])
        shape = tuple(field["shape"])
        if dtype.kind == "b":
            low, high = 0, 1
        elif dtype.kind in "iu":
            limit = np.iinfo(dtype)
            low, high = limit.min, limit.max
        else:
            low, high = -np.inf, np.inf
        # Gymnasium Box supports bool arrays and fixed scalar shapes.
        spaces[name] = gym.spaces.Box(low, high, shape=shape, dtype=dtype)
    return gym.spaces.Dict(spaces)


def project_observation(raw, spec):
    values = {}
    for path, field in spec["fields"].items():
        value = raw
        for name in path.split("."):
            value = value[name]
        array = np.asarray(value, dtype=field["dtype"])
        shape = tuple(field["shape"])
        if path == "sensors.lidar.points" and array.shape != shape:
            if array.ndim != 2 or array.shape[1] != 4 or array.shape[0] > shape[0]:
                raise ValueError("invalid LiDAR shape")
            padded = np.zeros(shape, dtype=array.dtype)
            padded[: len(array)] = array
            array = padded
        if array.shape != shape or (
            array.dtype.kind == "f" and not np.isfinite(array).all()
        ):
            raise ValueError(f"observation violates published specification: {path}")
        values[path] = array.copy()
    return values


class CarlaGymEnv(gym.Env):
    metadata = {"render_modes": [], "autoreset_mode": "Disabled"}

    def __init__(
        self,
        instance_name="carla_01",
        *,
        timeout=30.0,
        action_mode="vehicle_control",
        client=None,
    ):
        self.client = client or CarlaEnvClient(instance_name, timeout=timeout)
        self._owns_client = client is None
        try:
            self.capabilities = self.client.capabilities()
            self._spec = self.capabilities["observation_spec"]
            # Simulator frame IDs, timings and episode IDs are intentionally
            # observable and cannot satisfy Gym's reset/step equality check.
            self.spec = gym.envs.registration.EnvSpec(
                "EasyCARLA-v1", nondeterministic=True
            )
            self.observation_space = numeric_space(self._spec)
            if action_mode not in ("vehicle_control", "trajectory"):
                raise ValueError("invalid action_mode")
            if (
                action_mode == "trajectory"
                and "trajectory" not in self.capabilities.get("actions", [])
            ):
                raise ValueError("service does not support trajectories")
            self.action_mode = action_mode
            if action_mode == "vehicle_control":
                self.action_space = ControlSpace()
            else:
                self._trajectory = self.capabilities["trajectory_spec"]
                count = self._trajectory["points"]
                self.action_space = gym.spaces.Box(
                    np.tile([-np.inf, -np.inf, -np.pi, 0], (count, 1)).astype(
                        np.float32
                    ),
                    np.tile([np.inf, np.inf, np.pi, np.inf], (count, 1)).astype(
                        np.float32
                    ),
                )
            self._state = "needs_reset"
            self._frame = None
            self.last_observation = None
        except BaseException:
            if self._owns_client:
                self.client.close()
            raise

    def _result(self, raw, info):
        obs = project_observation(raw, self._spec)
        meta = copy.deepcopy(info)
        meta.update(
            {
                key: raw[key]
                for key in ("map_id", "route_id", "coordinate_frame")
                if key in raw
            }
        )
        meta.update(service_epoch=self.client.server_info["epoch"])
        self._frame = int(raw["frame"])
        self.last_observation = obs
        return obs, meta

    def reset(self, *, seed=None, options=None):
        super().reset(seed=seed)
        try:
            raw, info = self.client.reset(seed=seed, options=options)
            obs, info = self._result(raw, info)
            self._state = "ready"
            return obs, info
        except BaseException as exc:
            if not is_request_rejection(exc):
                self._state = "faulted"
            raise

    def prepare_action(self, action):
        if self._state != "ready":
            raise RuntimeError(
                f"environment is {self._state}; explicit reset/reconnect is required"
            )
        if self.action_mode == "vehicle_control":
            return ("step", (validate_control(action),), {})
        points = np.asarray(action, dtype=np.float64)
        if (
            points.shape != self.action_space.shape
            or not np.isfinite(points).all()
            or np.any(points[:, 3] < 0)
            or np.any(np.abs(points[:, 2]) > np.pi)
        ):
            raise ValueError(
                "trajectory action must be finite [x,y,yaw,speed] within its fixed space"
            )
        last = self.last_observation or {}
        if "planning.geometry.rear_axle_valid" in last and not bool(
            last["planning.geometry.rear_axle_valid"]
        ):
            raise ValueError(
                "planning trajectory tracking requires measured vehicle geometry"
            )
        with np.errstate(over="ignore", invalid="ignore"):
            converted = points.astype(np.float32)
        if not np.isfinite(converted).all():
            raise ValueError("trajectory action cannot be represented as finite float32")
        trajectory = {
            "reference_frame": self._frame,
            "reference_point": self._trajectory["reference_point"],
            "times": np.arange(len(points), dtype=np.float64) * self._trajectory["dt"],
            "points": converted,
        }
        return (
            "step_trajectory",
            (trajectory,),
            {"execute_seconds": self.capabilities["clocks"]["decision_dt"]},
        )

    def consume_step(self, result):
        raw, reward, terminated, truncated, info = result
        obs, info = self._result(raw, info)
        self._state = "done" if terminated or truncated else "ready"
        return obs, float(reward), bool(terminated), bool(truncated), info

    def step(self, action):
        method, args, kwargs = self.prepare_action(action)
        try:
            return self.consume_step(getattr(self.client, method)(*args, **kwargs))
        except BaseException as exc:
            if not is_request_rejection(exc):
                self._state = "faulted"
            raise

    def get_static(self, map_id=None):
        return self.client.get_static(map_id)

    def get_route(self, route_id=None):
        return self.client.get_route(route_id)

    def close(self):
        if self._owns_client:
            self.client.close()
        self._state = "closed"
