"""Deterministic transport fixture. This is NOT a CARLA simulator or driving test."""
import time
import numpy as np


class FakeBackend:
    def __init__(self, config=None):
        self.config = config or {}
        self.frame = 0
        self.active = False
        self.total_steps = 0
        self.close_count = 0

    def _obs(self):
        return {"state": np.full((4,), self.frame, dtype=np.float32),
                "frame": np.array(self.frame, dtype=np.int64), "mask": np.ones((4,), dtype=bool)}

    def reset(self, seed=None, options=None):
        self.frame = 0
        self.active = True
        return self._obs(), {"backend": "fake", "seed": seed, "map_id": "fake-map-v1"}

    def step(self, action):
        if not self.active:
            raise ValueError("reset is required")
        action = np.asarray(action)
        if action.shape != (3,) or not np.isfinite(action).all():
            raise ValueError("Expected a finite action of shape (3,)")
        if np.any(action < [-1, 0, 0]) or np.any(action > [1, 1, 1]) or action[1] > 0 and action[2] > 0:
            raise ValueError("Invalid control bounds or simultaneous throttle/brake")
        time.sleep(float(self.config.get("delay", 0)))
        if self.config.get("fail_step"):
            raise RuntimeError("Injected backend failure")
        self.frame += 4
        self.total_steps += 1
        terminated = self.frame >= int(self.config.get("terminal_frame", 100000))
        if terminated:
            self.active = False
        return self._obs(), float(action[1]), terminated, False, {"backend": "fake", "total_steps": self.total_steps}

    def step_trajectory(self, trajectory, execute_seconds=None):
        raise ValueError("The transport fixture does not implement trajectory physics")

    def get_static(self, map_id=None):
        if map_id not in (None, "fake-map-v1"):
            raise ValueError("Unknown static map")
        return {"map_id": "fake-map-v1", "points": np.arange(12, dtype=np.float32).reshape(4, 3)}

    def capabilities(self):
        return {"backend": "fake", "trajectory": False, "replay": False, "npc_takeover": False,
                "actions": ["vehicle_control"], "clocks": {"physics_dt":.05,"decision_dt":.2,"history_dt":.1},
                "observation_spec": {"version":1,"fields":{
                    "state":{"shape":[4],"dtype":"float32"},
                    "frame":{"shape":[],"dtype":"int64"},
                    "mask":{"shape":[4],"dtype":"bool"}}}}

    def experiment(self, method, **kwargs):
        if method == "diagnostics":
            return {"frame": self.frame, "total_steps": self.total_steps,
                    "active": self.active, "close_count": self.close_count}
        if method == "echo":
            return kwargs
        raise ValueError("Unknown fake experiment method")

    def close_episode(self):
        if self.active:
            self.close_count += 1
        self.active = False

    def shutdown(self):
        self.close_episode()

    def close_client_session(self):
        self.close_episode()
