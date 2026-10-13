"""Time-integrated driving reward, event costs and explicit episode outcomes."""
from copy import deepcopy
import math
import numpy as np


def inertial_acceleration(obs):
    """Undo the observation's yaw rotation before differencing accelerations."""
    value = np.asarray(obs["ego"]["acceleration"], float).copy()
    yaw = obs["ego"].get("yaw_world", 0.0)
    c, s = math.cos(yaw), math.sin(yaw)
    x, y = value[:2]
    value[:2] = [c*x - s*y, s*x + c*y]
    return value


class DrivingTask:
    def __init__(self, config):
        self.config = deepcopy(config)
        self.previous_progress = 0.0
        self.previous_acceleration = np.zeros(3)
        self.events = set()
        self.goal_reached = False

    def reset(self, obs):
        self.previous_progress = obs["route"]["progress"]
        self.previous_acceleration = inertial_acceleration(obs)
        self.events.clear()
        self.goal_reached = False

    def evaluate(self, obs, facts, dt, elapsed):
        self.goal_reached |= bool(facts.get("goal"))
        acc = inertial_acceleration(obs)
        jerk = (acc - self.previous_acceleration) / dt
        limit = max(obs["ego"]["speed_limit"], 0.1)
        components = {"progress": obs["route"]["progress"] - self.previous_progress,
                      "time": dt, "acceleration": min(4.0, float(np.sum((acc[:2] / 3.0)**2))) * dt,
                      "jerk": min(4.0, float(np.sum((jerk[:2] / 5.0)**2))) * dt,
                      "overspeed": max(0.0, (obs["ego"]["speed"] - limit) / limit)**2 * dt}
        for event in ("collision", "offroad", "signal", "goal"):
            # Event ids let distinct violations be charged without per-tick repeats.
            event_id = facts.get(event + "_id", event)
            new = bool(facts.get(event)) and event_id not in self.events
            components[event] = float(new)
            if new:
                self.events.add(event_id)
        self.previous_progress = obs["route"]["progress"]
        self.previous_acceleration = acc
        terms = {k: float(v * self.config["weights"][k]) for k, v in components.items()}
        termination_events = ("collision", "offroad", "goal")
        if self.config["profile"] == "bench2drive_native":
            terminated = bool(facts.get("native_done") and not facts.get("native_timeout"))
            truncated = bool(facts.get("native_timeout")) and not terminated
            reason = "timeout" if truncated else next(
                (k for k in termination_events if facts.get(k)), "native_done" if terminated else "")
        else:
            rules = self.config.get("termination", dict.fromkeys((*termination_events, "timeout"), True))
            termination_events = tuple(k for k in termination_events if rules[k])
            terminated = any(bool(facts.get(k)) for k in termination_events)
            truncated = bool(rules["timeout"] and elapsed >= self.config["max_seconds"] - 1e-9 and not terminated)
            reason = "timeout" if truncated else next(
                (k for k in termination_events if facts.get(k)), "")
        cost_components = {k: components[k] for k in ("collision", "offroad", "signal", "overspeed")}
        return sum(terms.values()), terminated, truncated, {
            "reward_components": terms, "cost": sum(cost_components.values()),
            "cost_components": cost_components,
            "termination_reason": reason,
            "goal_reached": self.goal_reached,
        }
