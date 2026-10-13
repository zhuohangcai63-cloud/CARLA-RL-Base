"""Optional in-process Gym shim; remote users install easycarla_client."""
import warnings
import gymnasium as gym
import numpy as np
from easycarla.control import legacy_action
from easycarla.runtime import EnvironmentRuntime


class CarlaEnv(gym.Env):
    def __init__(self, params=None, config=None):
        self.legacy = config is None
        if self.legacy:
            p = params or {}
            warnings.warn("carla-v0 now returns structured observations; see docs/behavior-changes.md", DeprecationWarning, stacklevel=2)
            if p.get("ego_autopilot") or p.get("ego_external_control"):
                raise ValueError("Use external actions/step_trajectory; implicit ego autopilot is no longer an action mode")
            config = {"simulation": {"port": p.get("port", 2000), "physics_dt": p.get("dt", 0.1),
                                      "decision_dt": p.get("dt", 0.1), "history_dt": p.get("dt", 0.1)},
                      "scenario": {"source": "scenic", "town": p.get("town", "Town10HD"),
                                   "file": p.get("scenic_file", "scenarios/scenic/mixed_long_tail.scenic"),
                                   "params": p.get("scenic_params", {})},
                      "task": {"max_seconds": p.get("max_time_episode", 3000) * p.get("dt", 0.1)},
                      "observations": {"rgb": p.get("enable_camera", False), "bev": p.get("enable_bev", False),
                                       "lidar": p.get("enable_lidar", False)}}
        self.runtime = EnvironmentRuntime(config)
        self.action_space = gym.spaces.Box(np.array([-1, -1] if self.legacy else [-1, 0, 0], np.float32),
                                           np.ones(2 if self.legacy else 3, np.float32))
        self.observation_space = gym.spaces.Dict({})

    def reset(self, *, seed=None, options=None):
        super().reset(seed=seed)
        obs, info = self.runtime.reset(seed=seed, options=options)
        self.observation_space = _space(obs)
        return obs, info

    def step(self, action):
        return self.runtime.step(legacy_action(action) if self.legacy else action)

    def close(self):
        self.runtime.shutdown()


def _space(value):
    if isinstance(value, dict):
        return gym.spaces.Dict({k: _space(v) for k, v in value.items()})
    if isinstance(value, np.ndarray):
        if value.dtype == bool:
            return gym.spaces.Box(0, 1, value.shape, dtype=bool)
        if np.issubdtype(value.dtype, np.integer):
            limits = np.iinfo(value.dtype)
            return gym.spaces.Box(limits.min, limits.max, value.shape, dtype=value.dtype)
        return gym.spaces.Box(-np.inf, np.inf, value.shape, dtype=value.dtype)
    if isinstance(value, str):
        return gym.spaces.Text(max_length=256, charset="abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_/-.")
    return gym.spaces.Box(-np.inf, np.inf, (), dtype=np.float64)
