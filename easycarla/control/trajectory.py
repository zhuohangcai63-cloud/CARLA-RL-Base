"""CARLA-independent trajectory and route tracking in world right-handed axes."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import math

import numpy as np

from .actions import validate_action


def wrap_angle(angle):
    return (angle + np.pi) % (2 * np.pi) - np.pi


def pose_values(state):
    position = np.asarray(state["position"], dtype=np.float64)
    yaw = float(state["yaw"])
    speed = float(state.get("speed", np.linalg.norm(state.get("velocity", [0, 0]))))
    if position.shape not in ((2,), (3,)) or not np.isfinite(position).all():
        raise ValueError("position must be a finite world [x,y] or [x,y,z]")
    if not np.isfinite([yaw, speed]).all() or speed < 0:
        raise ValueError("yaw and nonnegative speed must be finite")
    return position[:2], yaw, speed


@dataclass
class PID:
    kp: float = 0.4
    ki: float = 0.05
    kd: float = 0.02
    integral_limit: float = 5.0
    integral: float = 0.0
    previous_error: float | None = None

    def control(self, error: float, dt: float) -> float:
        if not math.isfinite(dt) or dt <= 0:
            raise ValueError("dt must be positive and finite")
        derivative = 0 if self.previous_error is None else (error - self.previous_error) / dt
        candidate = float(np.clip(self.integral + error * dt, -self.integral_limit, self.integral_limit))
        raw = self.kp * error + self.ki * candidate + self.kd * derivative
        # Integrate only when unsaturated or when the error unwinds saturation.
        if abs(raw) <= 1 or raw * error <= 0:
            self.integral = candidate
        self.previous_error = error
        return float(np.clip(raw, -1, 1))

    def state_dict(self):
        return asdict(self)

    def load_state_dict(self, state):
        replacement = PID(**state)
        if not all(math.isfinite(v) for k, v in asdict(replacement).items() if v is not None):
            raise ValueError("PID state must be finite")
        self.__dict__.update(replacement.__dict__)


class PathTracker:
    """Shared pure-pursuit lateral and PID longitudinal controller."""
    def __init__(self, route, wheelbase=2.8, max_steer_angle=0.7, lookahead=3.0):
        route = np.asarray(route, dtype=np.float64)
        if route.ndim != 2 or route.shape[0] < 2 or route.shape[1] not in (2, 3):
            raise ValueError("route requires at least two world [x,y] or [x,y,z] points")
        if not np.isfinite(route).all():
            raise ValueError("route must be finite")
        self.route = route[:, :2].copy()
        self.wheelbase = float(wheelbase)
        self.max_steer_angle = float(max_steer_angle)
        self.lookahead = float(lookahead)
        if min(self.wheelbase, self.max_steer_angle, self.lookahead) <= 0:
            raise ValueError("tracker geometry must be positive")
        self.path_index = 0
        self.pid = PID()

    def control(self, state, target_speed, dt):
        position, yaw, speed = pose_values(state)
        if not math.isfinite(target_speed) or target_speed < 0:
            raise ValueError("target speed must be finite and nonnegative")
        # Only search forward; a mechanism switch must not reset route progress.
        remaining = self.route[self.path_index:]
        self.path_index += int(np.argmin(np.linalg.norm(remaining - position, axis=1)))
        distance = np.linalg.norm(self.route[self.path_index:] - position, axis=1)
        ahead = np.flatnonzero(distance >= max(self.lookahead, speed * 0.6))
        target_index = self.path_index + (int(ahead[0]) if len(ahead) else len(distance) - 1)
        target = self.route[target_index]
        return self.control_target(state, target, target_speed, dt)

    def control_target(self, state, target, target_speed, dt, target_yaw=None):
        position, yaw, speed = pose_values(state)
        delta = np.asarray(target, dtype=np.float64)[:2] - position
        lateral = -math.sin(yaw) * delta[0] + math.cos(yaw) * delta[1]
        length_sq = max(float(delta @ delta), 0.25)
        steering_angle = math.atan2(2 * self.wheelbase * lateral, length_sq)
        if target_yaw is not None and float(delta @ delta) < 0.25:
            steering_angle = float(wrap_angle(target_yaw - yaw))
        # RH yaw is positive left; CARLA steer is positive right.
        steer = float(np.clip(-steering_angle / self.max_steer_angle, -1, 1))
        longitudinal = self.pid.control(float(target_speed) - speed, dt)
        if target_speed <= 0.02 and speed < 0.1:
            longitudinal = min(longitudinal, -0.2)  # Hold on slopes.
        return validate_action([steer, max(longitudinal, 0), max(-longitudinal, 0)])

    def state_dict(self):
        return {"path_index": self.path_index, "pid": self.pid.state_dict()}

    def load_state_dict(self, state):
        path_index = int(state["path_index"])
        if not 0 <= path_index < len(self.route):
            raise ValueError("path_index is outside route")
        self.pid.load_state_dict(state["pid"])
        self.path_index = path_index


class TrajectoryTracker:
    """Tracks externally supplied, time-indexed points; never generates a plan.

    The reference pose is frozen by start. All input points are x-forward,
    y-left, yaw-counterclockwise relative to that pose, in meters/radians.
    """
    def __init__(self):
        self._tracker = None
        self.elapsed = 0.0
        self.execute_seconds = 0.0

    def start(self, trajectory, reference_frame, ego_pose, execute_seconds=None):
        reference_point = trajectory.get("reference_point", "actor")
        if reference_point not in ("actor", "rear_axle"):
            raise ValueError("reference_point must be actor or rear_axle")
        g = ego_pose.get("geometry", {})
        if g and not g.get("rear_valid", False):
            raise ValueError("planning trajectory tracking requires measured vehicle geometry")
        if reference_point == "rear_axle" and not g.get("rear_valid", False):
            raise ValueError("rear_axle tracking requires measured vehicle geometry")
        frame = trajectory["reference_frame"]
        if isinstance(frame, (bool, np.bool_)) or not isinstance(frame, (int, np.integer)) or frame < 0:
            raise ValueError("trajectory reference_frame must be a nonnegative integer")
        if frame != reference_frame:
            raise ValueError("trajectory reference_frame is stale")
        times = np.asarray(trajectory["times"], dtype=np.float64)
        points = np.asarray(trajectory["points"], dtype=np.float64)
        if times.ndim != 1 or len(times) < 2 or points.shape != (len(times), 4):
            raise ValueError("trajectory requires times[N] and points[N,4] (x,y,yaw,speed)")
        if not np.isfinite(times).all() or not np.isfinite(points).all():
            raise ValueError("trajectory must be finite")
        if times[0] != 0 or np.any(np.diff(times) <= 0) or np.any(points[:, 3] < 0):
            raise ValueError("times must start at zero and increase; speeds must be nonnegative")
        duration = float(times[-1] if execute_seconds is None else execute_seconds)
        if not math.isfinite(duration) or duration <= 0 or duration > times[-1] + 1e-9:
            raise ValueError("execution duration must be positive and covered by the trajectory")
        tracking_pose = dict(ego_pose)
        if reference_point == "rear_axle":
            tracking_pose["position"] = ego_pose["rear_axle"]
        position, yaw, _ = pose_values(tracking_pose)
        rotation = np.array([[math.cos(yaw), -math.sin(yaw)], [math.sin(yaw), math.cos(yaw)]])
        self.points = points.copy()
        self.points[:, :2] = points[:, :2] @ rotation.T + position
        self.points[:, 2] = np.unwrap(points[:, 2] + yaw)
        self.times = times.copy()
        previous_pid = None if self._tracker is None else self._tracker.pid.state_dict()
        self._tracker = PathTracker(self.points[:, :2], **({"wheelbase": g["wheelbase"],
                                  "max_steer_angle": g["max_steer_angle"]} if g.get("rear_valid", False) else {}))
        self.reference_point = reference_point
        if previous_pid is not None:
            self._tracker.pid.load_state_dict(previous_pid)
        self.execute_seconds = duration
        self.elapsed = 0.0

    def control(self, state, dt):
        if self._tracker is None or self.elapsed >= self.execute_seconds - 1e-9:
            raise RuntimeError("trajectory is not active")
        if not math.isfinite(dt) or dt <= 0 or self.elapsed + dt > self.execute_seconds + 1e-8:
            raise ValueError("dt must fit inside the requested execution window")
        target_time = min(self.elapsed + max(0.3, dt), self.times[-1])
        target = [np.interp(target_time, self.times, self.points[:, i]) for i in range(3)]
        speed = float(np.interp(min(self.elapsed + dt, self.times[-1]), self.times, self.points[:, 3]))
        tracking_state = dict(state)
        if self.reference_point == "rear_axle":
            tracking_state["position"] = state["rear_axle"]
        control = self._tracker.control_target(tracking_state, target[:2], speed, dt, target_yaw=target[2])
        self.elapsed += dt
        return control

    def state_dict(self):
        if self._tracker is None:
            return {"active": False}
        return {"active": True, "elapsed": self.elapsed, "execute_seconds": self.execute_seconds,
                "times": self.times.tolist(), "world_points": self.points.tolist(),
                "reference_point": self.reference_point, "wheelbase": self._tracker.wheelbase,
                "max_steer_angle": self._tracker.max_steer_angle,
                "tracker": self._tracker.state_dict()}

    def load_state_dict(self, state):
        if not state["active"]:
            self._tracker = None
            self.elapsed = self.execute_seconds = 0.0
            return
        self.times = np.asarray(state["times"], dtype=np.float64)
        self.points = np.asarray(state["world_points"], dtype=np.float64)
        self.reference_point = state.get("reference_point", "actor")
        self._tracker = PathTracker(self.points[:, :2], wheelbase=state.get("wheelbase", 2.8),
                                   max_steer_angle=state.get("max_steer_angle", .7))
        self._tracker.load_state_dict(state["tracker"])
        self.elapsed = float(state["elapsed"])
        self.execute_seconds = float(state["execute_seconds"])
