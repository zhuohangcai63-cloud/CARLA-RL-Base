"""In-memory use of the pinned official scoring implementation.

No result files, training labels, or experiment scheduling belong here. Native
telemetry deliberately keeps CARLA's original units for upstream tool parity.
"""
from collections import deque
from copy import deepcopy
import math
import numbers
import time


def event_data(value):
    """Convert upstream event CARLA objects to protocol-safe raw facts."""
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, numbers.Integral):
        return int(value)
    if isinstance(value, numbers.Real):
        return float(value)
    if isinstance(value, dict):
        return {str(k): event_data(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [event_data(v) for v in value]
    if all(hasattr(value, k) for k in ("x", "y", "z")):
        return {k: float(getattr(value, k)) for k in ("x", "y", "z")}
    if hasattr(value, "id") and hasattr(value, "type_id"):
        return {"actor_id": int(value.id), "type_id": str(value.type_id)}
    raise TypeError(f"Unsupported native event payload {type(value).__name__}")


class NativeStatistics:
    def __init__(self, scene, route_id, town, config, physics_dt, decision_dt):
        from leaderboard.utils.statistics_manager import StatisticsManager
        self.manager = StatisticsManager(endpoint="", debug_endpoint="")
        self.manager.create_route_data(f"RouteScenario_{route_id}_rep0",
                                       ",".join(s.type for s in scene.config.scenario_configs),
                                       None, None, town, 0)
        self.manager.set_scenario(scene)
        self.started = time.monotonic()
        self.origin_time = None
        self.last_frame = None
        self.elapsed = 0.0
        self.physics_dt = physics_dt
        capacity = config.get("metric_history_ticks", max(1, math.ceil(decision_dt / physics_dt)))
        if not isinstance(capacity, int) or not 1 <= capacity <= 4096:
            raise ValueError("metric_history_ticks must be an integer between 1 and 4096")
        self.samples = deque(maxlen=capacity)
        self.telemetry_enabled = bool(config.get("native_metrics", False))
        self.telemetry_count = 0
        self._cached = None

    def update(self, snapshot, ego, initializing=False):
        if self.last_frame == snapshot.frame:
            return
        self.last_frame = snapshot.frame
        timestamp = snapshot.timestamp.elapsed_seconds
        if self.origin_time is None or initializing:
            self.origin_time = timestamp
        if initializing:
            self.samples.clear()
            self.telemetry_count = 0
        self.elapsed = timestamp - self.origin_time
        self._cached = None
        if self.telemetry_enabled:
            transform = ego.get_transform()
            def xyz(vector):
                return [float(vector.x), float(vector.y), float(vector.z)]
            self.samples.append({"frame": int(snapshot.frame), "timestamp": float(timestamp),
                                 "acceleration": xyz(ego.get_acceleration()),
                                 "angular_velocity": xyz(ego.get_angular_velocity()),
                                 "forward_vector": xyz(transform.get_forward_vector()),
                                 "right_vector": xyz(transform.get_right_vector()),
                                 "location": xyz(transform.location),
                                 "rotation": [float(transform.rotation.roll), float(transform.rotation.pitch),
                                              float(transform.rotation.yaw)]})
            self.telemetry_count += 1

    def result(self, final=False):
        if self._cached is None:
            # Upstream appends route_timeout each compute call; reset that list
            # as it already does for all other infractions, preserving idempotence.
            self.manager._results.checkpoint.records[0].infractions["route_timeout"] = []
            self.manager.compute_route_statistics(0, time.monotonic() - self.started, self.elapsed)
            record = deepcopy(self.manager._results.checkpoint.records[0].to_json())
            raw_events = []
            efficiency = []
            for criterion in self.manager._scenario.get_criteria():
                for event in criterion.events:
                    event_type = event.get_type().name
                    value = event.get_dict()
                    raw_events.append({"criterion": criterion.name, "type": event_type,
                                       "frame": int(event.get_frame()), "message": event.get_message(),
                                       "data": event_data(value)})
                    if event_type == "MIN_SPEED_INFRACTION" and value is not None:
                        percentage = float(value["percentage"])
                        if 0 <= percentage <= 1000:
                            efficiency.append(percentage)
            self._cached = {"implementation": "pinned_leaderboard_statistics_manager", "record": record,
                            "events": raw_events,
                            "efficiency_mean_percent": sum(efficiency) / len(efficiency) if efficiency else None,
                            "efficiency_valid_checkpoints": len(efficiency),
                            "smoothness_score": None,
                            "smoothness_status": "not_computed; raw telemetry available with native_metrics=true"}
        result = deepcopy(self._cached)
        result["final"] = bool(final)
        if self.telemetry_enabled:
            result["telemetry"] = {"samples": deepcopy(list(self.samples)), "capacity": self.samples.maxlen,
                                   "total_samples": self.telemetry_count, "physics_dt": self.physics_dt,
                                   "coordinate_frame": "carla_world_left_handed",
                                   "units": {"acceleration": "m/s^2", "angular_velocity": "degrees/s",
                                             "rotation": "degrees (roll,pitch,yaw)", "location": "m",
                                             "forward_vector": "unit vector", "right_vector": "unit vector"}}
        return result
