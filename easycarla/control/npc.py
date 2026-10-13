"""Reactive vehicle mechanisms over live states, not prerecorded trajectories.

This controller is deliberately simulator independent. A CARLA adapter supplies
right-handed world state, route points and traffic-signal constraints. It must
also own the actor exclusively before applying the returned control.
"""
from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass, replace
import hashlib
import json
import math

import numpy as np

from .actions import validate_action
from .trajectory import PathTracker, pose_values


CONTROLLER_VERSION = "reactive-vehicle-1"


def indexed_uniform(seed, actor_id, tick, channel, low=0.0, high=1.0):
    """Deterministic noise unaffected by another branch's number of RNG calls."""
    key = json.dumps([int(seed), str(actor_id), int(tick), str(channel)], separators=(",", ":"))
    bits = int.from_bytes(hashlib.blake2b(key.encode(), digest_size=8).digest(), "big")
    return float(low + (high - low) * ((bits >> 11) / (1 << 53)))


@dataclass(frozen=True)
class Mechanism:
    reaction_delay: float = 0.2
    accepted_gap: float = 4.0
    yield_window: float = 3.0
    yield_speed_multiplier: float = 0.75
    yield_deceleration: float = 2.0
    proactive_yield: bool = True
    delayed_confirmation: bool = True
    gap_check: bool = True

    def __post_init__(self):
        numeric = [self.reaction_delay, self.accepted_gap, self.yield_window,
                   self.yield_speed_multiplier, self.yield_deceleration]
        if not all(math.isfinite(x) for x in numeric):
            raise ValueError("mechanism parameters must be finite")
        if not 0 <= self.reaction_delay <= 10 or self.accepted_gap <= 0 or self.yield_window <= 0:
            raise ValueError("invalid reaction delay, accepted gap or yield window")
        if not 0 < self.yield_speed_multiplier <= 1 or self.yield_deceleration <= 0:
            raise ValueError("invalid yield speed or deceleration")


def mechanism_preset(name, seed=0, actor_id="npc", randomize=False):
    """C/A/L/G differ only on their declared axis; optional ranges are initial values."""
    if name not in ("C", "A", "L", "G"):
        raise ValueError("mechanism preset must be C, A, L or G")
    base = Mechanism()
    if randomize:
        base = replace(base,
                       reaction_delay=indexed_uniform(seed, actor_id, 0, "normal_delay", 0.1, 0.3),
                       accepted_gap=indexed_uniform(seed, actor_id, 0, "normal_gap", 3.5, 4.5),
                       yield_speed_multiplier=indexed_uniform(seed, actor_id, 0, "yield_speed", 0.65, 0.85))
    if name == "A":
        return replace(base, proactive_yield=False)
    if name == "L":
        return replace(base, reaction_delay=indexed_uniform(seed, actor_id, 0, "slow_delay", 0.6, 1.0) if randomize else 0.8)
    if name == "G":
        return replace(base, accepted_gap=indexed_uniform(seed, actor_id, 0, "small_gap", 1.5, 2.5) if randomize else 2.0)
    return base


def _velocity(state):
    if "velocity" in state:
        result = np.asarray(state["velocity"], dtype=np.float64)[:2]
        if result.shape != (2,) or not np.isfinite(result).all():
            raise ValueError("velocity must be a finite world vector")
        return result
    _, yaw, speed = pose_values(state)
    return np.array([math.cos(yaw), math.sin(yaw)]) * speed


def _dimensions(state):
    dimensions = state.get("dimensions", [4.5, 1.8, 1.5])
    if len(dimensions) < 2 or not np.isfinite(dimensions[:2]).all() or min(dimensions[:2]) <= 0:
        raise ValueError("dimensions must start with positive length and width")
    return float(dimensions[0]), float(dimensions[1])


class ReactiveNPCController:
    """Route-following controller with shared safety and three mechanism axes.

    state fields: position, yaw, speed, optional velocity and dimensions.
    Optional signal_red/stop_distance enforce a common signal constraint.
    Neighbors use the same fields plus id; all are live state from this tick.
    Geometry predicts constant-velocity route crossings as an interaction cue;
    the controller recomputes them on every physical tick.
    """
    def __init__(self, actor_id, route, mechanism="C", target_speed=8.0, seed=0,
                 randomize=False, event_capacity=256):
        self.actor_id = str(actor_id)
        self.seed = int(seed)
        self.randomize = bool(randomize)
        self.target_speed = float(target_speed)
        if not math.isfinite(self.target_speed) or self.target_speed <= 0:
            raise ValueError("target_speed must be positive and finite")
        if int(event_capacity) < 1:
            raise ValueError("event_capacity must be positive")
        self.tracker = PathTracker(route)
        self.time = 0.0
        self.last_tick = None
        self._history = deque()
        self._events = deque(maxlen=int(event_capacity))
        self.dropped_events = 0
        self.mode = "cruise"
        self.last_diagnostics = {}
        self.trigger_counts = {"proactive_yield": 0, "gap_wait": 0, "following": 0,
                               "emergency": 0, "signal": 0}
        self.set_mechanism(mechanism, record=False)

    def set_mechanism(self, mechanism, record=True):
        if isinstance(mechanism, str):
            params = mechanism_preset(mechanism, self.seed, self.actor_id, self.randomize)
            name = mechanism
        else:
            params = mechanism if isinstance(mechanism, Mechanism) else Mechanism(**mechanism)
            name = "custom"
        self.mechanism = params
        self.mechanism_name = name
        if record:
            self._event({"type": "mechanism_switch", "time": self.time,
                         "mechanism": name, "parameters": asdict(params)})

    def _event(self, event):
        if len(self._events) == self._events.maxlen:
            self.dropped_events += 1
        self._events.append(event)

    def _features(self, state, neighbors):
        position, yaw, speed = pose_values(state)
        velocity = _velocity(state)
        forward = np.array([math.cos(yaw), math.sin(yaw)])
        left = np.array([-math.sin(yaw), math.cos(yaw)])
        own_length, own_width = _dimensions(state)
        features = {"following": [], "crossings": [], "emergency": []}
        # Sorting makes a tied interaction independent of world actor iteration order.
        for other in sorted(neighbors, key=lambda x: str(x.get("id", ""))):
            if str(other.get("id", "")) == self.actor_id:
                continue
            other_pos, _, _ = pose_values(other)
            other_vel = _velocity(other)
            other_length, other_width = _dimensions(other)
            delta = other_pos - position
            longitudinal, lateral = float(delta @ forward), float(delta @ left)
            half_lengths = (own_length + other_length) / 2
            lateral_width = (own_width + other_width) / 2 + 0.4
            lead_speed = float(other_vel @ forward)
            gap = longitudinal - half_lengths
            closing = speed - lead_speed
            if longitudinal > 0 and abs(lateral) <= lateral_width:
                features["following"].append({"id": str(other.get("id", "")), "gap": gap,
                                               "speed": max(lead_speed, 0)})
                if gap < 1.0 or (closing > 0 and gap / closing < 1.0):
                    features["emergency"].append(str(other.get("id", "")))
            relative_velocity = other_vel - velocity
            relative_speed_sq = float(relative_velocity @ relative_velocity)
            if relative_speed_sq > 1e-8:
                closest_time = -float(delta @ relative_velocity) / relative_speed_sq
                closest = delta + relative_velocity * max(closest_time, 0)
                if 0 <= closest_time <= 1.0 and np.linalg.norm(closest) < lateral_width:
                    features["emergency"].append(str(other.get("id", "")))
            other_speed = float(np.linalg.norm(other_vel))
            if other_speed < 0.1:
                continue
            other_direction = other_vel / other_speed
            matrix = np.column_stack([forward, -other_direction])
            if abs(float(np.linalg.det(matrix))) < 0.2:
                continue
            own_distance, other_distance = np.linalg.solve(matrix, delta)
            if 0 < own_distance <= 50 and 0 < other_distance <= 60:
                own_time = float(own_distance / max(speed, 1.0))
                other_time = float(other_distance / other_speed)
                features["crossings"].append({"id": str(other.get("id", "")),
                    "distance": float(own_distance), "own_time": own_time,
                    "other_time": other_time, "arrival_gap": abs(own_time - other_time)})
        return features

    def control(self, state, neighbors, dt, tick):
        if not math.isfinite(dt) or dt <= 0:
            raise ValueError("dt must be positive and finite")
        if self.last_tick is not None and tick <= self.last_tick:
            raise ValueError("NPC physical ticks must increase")
        _, _, speed = pose_values(state)
        now = self.time
        features = self._features(state, neighbors)
        self._history.append({"time": now, "features": features})
        if len(self._history) > 10000:
            raise RuntimeError("NPC observation history capacity exceeded")
        delay = self.mechanism.reaction_delay if self.mechanism.delayed_confirmation else 0.0
        # Retain 10 seconds even for short-delay mechanisms, so a switch to a
        # longer delay can use the identical causal history without reset.
        while len(self._history) > 1 and self._history[1]["time"] < now - 10.0:
            self._history.popleft()
        confirmed = {"following": [], "crossings": []}
        for sample in reversed(self._history):
            if sample["time"] <= now - delay + 1e-9:
                confirmed = sample["features"]
                break
        desired = self.target_speed
        reasons = []
        for lead in confirmed["following"]:
            desired_gap = 2.0 + speed * 1.5
            if lead["gap"] < desired_gap:
                desired = min(desired, max(0.0, lead["speed"] + (lead["gap"] - desired_gap) / 1.5))
                reasons.append("following")
        for conflict in confirmed["crossings"]:
            if (self.mechanism.proactive_yield and 2 <= conflict["own_time"] <= self.mechanism.yield_window + 1
                    and conflict["arrival_gap"] < self.mechanism.yield_window):
                desired = min(desired, self.target_speed * self.mechanism.yield_speed_multiplier)
                reasons.append("proactive_yield")
            if (self.mechanism.gap_check and conflict["distance"] <= max(5.0, speed)
                    and conflict["arrival_gap"] < self.mechanism.accepted_gap):
                stopping_distance = max(0.0, conflict["distance"] - _dimensions(state)[0] / 2 - 2.0)
                desired = min(desired, math.sqrt(2 * 2.0 * stopping_distance))
                reasons.append("gap_wait")
        # Signal constraints and the emergency floor always use the current
        # state, never the delayed high-level interaction history.
        if state.get("signal_red", False):
            distance = float(state.get("stop_distance", 0.0))
            if not math.isfinite(distance):
                raise ValueError("stop_distance must be finite")
            desired = min(desired, math.sqrt(2 * 2.0 * max(distance - 2.0, 0.0)))
            reasons.append("signal")
        remaining_distance = float(np.linalg.norm(self.tracker.route[-1] - np.asarray(state["position"])[:2]))
        if remaining_distance < max(8.0, speed * 2):
            desired = min(desired, math.sqrt(2 * 2.0 * max(remaining_distance - 0.5, 0.0)))
        action = self.tracker.control(state, desired, dt)
        if set(reasons) == {"proactive_yield"}:
            action[2] = min(float(action[2]), self.mechanism.yield_deceleration / 6.0)
        if features["emergency"]:
            action[1:] = [0, 1]
            reasons.append("emergency")
        reasons = sorted(set(reasons))
        mode = "+".join(reasons) or "cruise"
        if mode != self.mode:
            self._event({"type": "state_transition", "tick": int(tick), "time": now,
                         "from": self.mode, "to": mode, "confirmed_features": confirmed})
        for reason in reasons:
            self.trigger_counts[reason] += 1
        self.mode = mode
        self.last_diagnostics = {"tick": int(tick), "mechanism": self.mechanism_name,
                                 "parameters": asdict(self.mechanism), "active": reasons,
                                 "desired_speed": desired, "features": features,
                                 "confirmed_features": confirmed}
        self.time += dt
        self.last_tick = int(tick)
        return validate_action(action)

    def state_dict(self):
        result = {"version": CONTROLLER_VERSION, "actor_id": self.actor_id,
                "seed": self.seed, "randomize": self.randomize,
                "target_speed": self.target_speed, "route": self.tracker.route.tolist(),
                "tracker": self.tracker.state_dict(), "time": self.time, "last_tick": self.last_tick,
                "mechanism_name": self.mechanism_name, "mechanism": asdict(self.mechanism),
                "history": list(self._history), "mode": self.mode, "trigger_counts": dict(self.trigger_counts),
                "events": list(self._events), "event_capacity": self._events.maxlen,
                "dropped_events": self.dropped_events, "last_diagnostics": self.last_diagnostics}
        # A saved state must not change as the controller continues or allow
        # a caller to mutate the live event/history queues through references.
        return json.loads(json.dumps(result, allow_nan=False))

    def load_state_dict(self, state):
        if state["version"] != CONTROLLER_VERSION or str(state["actor_id"]) != self.actor_id:
            raise ValueError("controller version or semantic actor id mismatch")
        # Round-trip through JSON also rejects non-data objects before mutation.
        state = json.loads(json.dumps(state, allow_nan=False))
        mechanism = Mechanism(**state["mechanism"])
        tracker = PathTracker(state["route"])
        tracker.load_state_dict(state["tracker"])
        if len(state["history"]) > 10000 or len(state["events"]) > state["event_capacity"]:
            raise ValueError("controller history or event capacity exceeded")
        self.seed = int(state["seed"])
        self.randomize = bool(state["randomize"])
        self.target_speed = float(state["target_speed"])
        self.tracker = tracker
        self.time = float(state["time"])
        self.last_tick = state["last_tick"]
        self.mechanism = mechanism
        self.mechanism_name = state["mechanism_name"]
        self._history = deque(state["history"])
        self.mode = state["mode"]
        self.trigger_counts = dict(state["trigger_counts"])
        self._events = deque(state["events"], maxlen=state["event_capacity"])
        self.dropped_events = int(state["dropped_events"])
        self.last_diagnostics = state["last_diagnostics"]
