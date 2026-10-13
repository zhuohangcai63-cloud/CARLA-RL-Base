from __future__ import annotations
from dataclasses import dataclass, asdict
import random
import numpy as np
import carla
from easycarla.control.ownership import ControlConflict
from .state import StateExporter

@dataclass(frozen=True)
class CrossingConfig:
    walker_speed_min: float = 1.0
    walker_speed_max: float = 2.0
    forced_crossing_walkers: int = 0
    forced_crossing_distance_min: float = 8.0
    forced_crossing_distance_max: float = 25.0
    forced_crossing_lateral_min: float = 2.0
    forced_crossing_lateral_max: float = 5.0
    forced_crossing_trigger_prob: float = 0.03
    forced_crossing_cooldown_steps: int = 120
    forced_crossing_warmup_steps: int = 50
    forced_crossing_min_speed: float = 1.5
    forced_crossing_max_total: int = 28


@dataclass
class CrossingState:
    forced_crossing_events: int = 0
    forced_crossing_spawned: int = 0
    forced_crossing_completed: int = 0
    forced_crossing_failed: int = 0
    forced_crossing_timeout: int = 0
    forced_crossing_disabled: bool = False
    forced_crossing_disable_reason: str = ''


class CrossingEvent:
    """Owned crossing event configuration, state and explicit lifecycle."""
    kind = 'crossing'
    def __init__(self, context, params, seed):
        self.context = context
        self.rng = random.Random(seed)
        values = {
            'walker_speed_min': params.get('walker_speed_min', 1.0),
            'walker_speed_max': params.get('walker_speed_max', 2.0),
            'forced_crossing_walkers': int(params.get('forced_crossing_walkers', 0)),
            'forced_crossing_distance_min': params.get('forced_crossing_distance_min', 8.0),
            'forced_crossing_distance_max': params.get('forced_crossing_distance_max', 25.0),
            'forced_crossing_lateral_min': params.get('forced_crossing_lateral_min', 2.0),
            'forced_crossing_lateral_max': params.get('forced_crossing_lateral_max', 5.0),
            'forced_crossing_trigger_prob': params.get('forced_crossing_trigger_prob', 0.03),
            'forced_crossing_cooldown_steps': round(float(params.get('forced_crossing_cooldown_steps', 120)) * 0.1 / context.dt),
            'forced_crossing_warmup_steps': round(float(params.get('forced_crossing_warmup_steps', 50)) * 0.1 / context.dt),
            'forced_crossing_min_speed': params.get('forced_crossing_min_speed', 1.5),
            'forced_crossing_max_total': int(params.get('forced_crossing_max_total', 28)),
        }
        # Preserve the original per-0.1-second trigger probability across physics rates.
        values["forced_crossing_trigger_prob"] = 1 - (1 - values["forced_crossing_trigger_prob"]) ** (context.dt / 0.1)
        self.config = CrossingConfig(**values)
        self.state = CrossingState()
        self.active_forced_crossing_events = {}
        self.forced_crossing_walker_ids = []
        self.forced_crossing_event_seq = 0
        self.last_forced_crossing_step = -10**9

    def prepare(self):
        """Crossing geometry is generated at its explicitly sampled trigger tick."""

    def close(self):
        self.context._destroy_actor_ids_batch(self.forced_crossing_walker_ids, label="crossing", do_tick=False)
        self.active_forced_crossing_events.clear()
        self.forced_crossing_walker_ids.clear()

    def export_state(self, actor_roles):
        exporter = StateExporter(actor_roles)
        event_fields = ("event_id", "walker_id", "spawn_location", "target_location", "direction",
                        "speed", "path_length", "completion_margin", "timeout_step")
        state = exporter.value(asdict(self.state))
        state.update({"event_seq": self.forced_crossing_event_seq,
                      "last_trigger_step": self.last_forced_crossing_step,
                      "actors": exporter.value(self.forced_crossing_walker_ids, "forced_crossing_walker_ids"),
                      "events": [exporter.fields(event, event_fields)
                                 for _, event in sorted(self.active_forced_crossing_events.items())]})
        return {"schema_version": 1, "kind": self.kind, "config": asdict(self.config), "state": state,
                "rng_state": exporter.value(self.rng.getstate()), "limitations": exporter.limitations}

    def before_tick(self):
        self._maybe_trigger_forced_crossing({'ego_state': [0,0,0,self.context._actor_speed(self.context.ego)]})
        self._apply_active_forced_crossing_controls()

    def after_tick(self):
        self._update_forced_crossing_events()

    def info(self):
        values = asdict(self.config)
        values.update(asdict(self.state))
        return {key: value for key, value in values.items() if key.startswith("forced_crossing_")}


    def _build_forced_crossing_specs(self, count, distance_min, distance_max, lateral_min, lateral_max):
        if count <= 0 or self.context.ego is None:
            return []

        distance_min = max(0.0, float(distance_min))
        distance_max = max(distance_min, float(distance_max))
        lateral_min = max(0.0, float(lateral_min))
        lateral_max = max(lateral_min, float(lateral_max))

        world_map = self.context.world.get_map()
        ego_waypoint = world_map.get_waypoint(
            self.context.ego.get_location(), project_to_road=True, lane_type=carla.LaneType.Driving
        )
        if ego_waypoint is None or ego_waypoint.is_intersection:
            return []

        specs = []
        attempts = 0
        max_attempts = max(20, count * 12)

        while len(specs) < count and attempts < max_attempts:
            attempts += 1
            longitudinal_distance = self.rng.uniform(distance_min, distance_max)
            anchor_waypoint = self.context._advance_waypoint_along_lane(ego_waypoint, longitudinal_distance)
            if anchor_waypoint is None or anchor_waypoint.is_intersection:
                continue

            lane_yaw = np.deg2rad(anchor_waypoint.transform.rotation.yaw)
            lane_normal = np.array([-np.sin(lane_yaw), np.cos(lane_yaw)], dtype=np.float32)

            lane_width = max(2.5, float(anchor_waypoint.lane_width))
            min_side_offset = max(lateral_min, lane_width * 1.1)
            max_side_offset = max(min_side_offset + 0.2, lateral_max)

            spawn_offset = self.rng.uniform(min_side_offset, max_side_offset)
            target_offset = self.rng.uniform(min_side_offset, max_side_offset)
            start_sign = -1.0 if self.rng.random() < 0.5 else 1.0

            center = anchor_waypoint.transform.location
            spawn_location = carla.Location(
                x=float(center.x + lane_normal[0] * start_sign * spawn_offset),
                y=float(center.y + lane_normal[1] * start_sign * spawn_offset),
                z=float(center.z + 0.8),
            )
            target_location = carla.Location(
                x=float(center.x - lane_normal[0] * start_sign * target_offset),
                y=float(center.y - lane_normal[1] * start_sign * target_offset),
                z=float(center.z + 0.8),
            )

            direction = self.context._normalize_xy(
                target_location.x - spawn_location.x,
                target_location.y - spawn_location.y,
            )
            if direction is None:
                continue

            crossing_distance = float(np.sqrt(
                (target_location.x - spawn_location.x) ** 2 +
                (target_location.y - spawn_location.y) ** 2
            ))
            if crossing_distance < max(4.0, lane_width * 1.5):
                continue

            if any(abs(spec['anchor_distance'] - longitudinal_distance) < 2.0 for spec in specs):
                continue

            specs.append({
                'anchor_distance': longitudinal_distance,
                'spawn_location': spawn_location,
                'target_location': target_location,
                'direction': direction,
                'path_length': crossing_distance,
                'completion_margin': max(0.8, min(1.6, lane_width * 0.35)),
            })

        return specs


    @staticmethod
    def _is_timeout_error(exc):
        msg = str(exc).lower()
        return ("time-out" in msg) or ("timed out" in msg) or ("timeout" in msg)


    def _disable_forced_crossing(self, reason, exc=None):
        if not self.state.forced_crossing_disabled:
            if exc is None:
                print(f"Warning: Forced crossing disabled for this episode ({reason})")
            else:
                print(f"Warning: Forced crossing disabled for this episode ({reason}): {exc}")
        self.state.forced_crossing_disabled = True
        self.state.forced_crossing_disable_reason = reason


    def _apply_forced_crossing_control(self, event, walker=None):
        if walker is None:
            walker = self.context.world.get_actor(event['walker_id'])
        if walker is None:
            return False

        direction = event['direction']
        control = carla.WalkerControl()
        control.direction = carla.Vector3D(x=float(direction[0]), y=float(direction[1]), z=0.0)
        control.speed = float(event['speed'])
        control.jump = False

        try:
            self.context.control(walker, control, source=self.kind)
        except ControlConflict:
            raise
        except RuntimeError as e:
            print(f"Warning: Failed to apply forced crossing control to walker {event['walker_id']}: {e}")
            return False
        return True


    def _cleanup_forced_crossing_event(self, event_id, outcome, reason=None):
        event = self.active_forced_crossing_events.pop(event_id, None)
        if event is None:
            return False

        walker_id = event.get('walker_id')
        if walker_id in self.forced_crossing_walker_ids:
            self.forced_crossing_walker_ids.remove(walker_id)
        if walker_id is not None:
            self.context._destroy_actor_ids_batch([walker_id], label='forced crossing walker cleanup', do_tick=False)

        if outcome == 'completed':
            self.state.forced_crossing_completed += 1
        elif outcome == 'timeout':
            self.state.forced_crossing_timeout += 1
        else:
            self.state.forced_crossing_failed += 1

        if outcome != 'completed':
            suffix = f": {reason}" if reason else ""
            print(f"Info: Forced crossing event {event_id} ended with outcome={outcome}{suffix}")
        return True


    def _apply_active_forced_crossing_controls(self):
        for event_id in list(self.active_forced_crossing_events.keys()):
            event = self.active_forced_crossing_events.get(event_id)
            if event is None:
                continue

            walker = self.context.world.get_actor(event['walker_id'])
            if walker is None:
                self._cleanup_forced_crossing_event(event_id, 'failed', 'actor_missing_before_tick')
                continue

            if not self._apply_forced_crossing_control(event, walker=walker):
                self._cleanup_forced_crossing_event(event_id, 'failed', 'apply_control_failed')


    def _update_forced_crossing_events(self):
        completed_this_step = 0
        failed_this_step = 0
        timeout_this_step = 0

        for event_id in list(self.active_forced_crossing_events.keys()):
            event = self.active_forced_crossing_events.get(event_id)
            if event is None:
                continue

            walker = self.context.world.get_actor(event['walker_id'])
            if walker is None:
                if self._cleanup_forced_crossing_event(event_id, 'failed', 'actor_missing_after_tick'):
                    failed_this_step += 1
                continue

            location = walker.get_location()
            start = event['spawn_location']
            target = event['target_location']
            direction = event['direction']

            progress_x = location.x - start.x
            progress_y = location.y - start.y
            progress = float(progress_x * direction[0] + progress_y * direction[1])
            remaining_distance = float(np.sqrt(
                (target.x - location.x) ** 2 + (target.y - location.y) ** 2
            ))

            if (
                progress >= event['path_length'] - event['completion_margin']
                or remaining_distance <= event['completion_margin']
            ):
                if self._cleanup_forced_crossing_event(event_id, 'completed'):
                    completed_this_step += 1
                continue

            if self.context.time_step >= event['timeout_step']:
                if self._cleanup_forced_crossing_event(event_id, 'timeout', 'crossing_timeout'):
                    timeout_this_step += 1

        return completed_this_step, failed_this_step, timeout_this_step


    def _spawn_forced_crossing_walkers(
        self,
        count,
        distance_min,
        distance_max,
        lateral_min,
        lateral_max,
        tick_before_start=False,
    ):
        if count <= 0 or self.context.ego is None:
            return 0

        del tick_before_start

        crossing_specs = self._build_forced_crossing_specs(
            count=count,
            distance_min=distance_min,
            distance_max=distance_max,
            lateral_min=lateral_min,
            lateral_max=lateral_max,
        )
        if not crossing_specs:
            print("Info: Forced crossing walkers skipped (no valid scripted crossing geometry found)")
            return 0

        walker_bps = self.context.world.get_blueprint_library().filter('walker.pedestrian.*')
        started_count = 0

        for spec in crossing_specs:
            walker_bp = self.rng.choice(walker_bps)
            if walker_bp.has_attribute('is_invincible'):
                walker_bp.set_attribute('is_invincible', 'false')

            direction = spec['direction']
            yaw = float(np.degrees(np.arctan2(direction[1], direction[0])))
            spawn_transform = carla.Transform(
                location=spec['spawn_location'],
                rotation=carla.Rotation(yaw=yaw)
            )

            try:
                walker = self.context.world.try_spawn_actor(walker_bp, spawn_transform)
            except ControlConflict:
                raise
            except RuntimeError as e:
                if self._is_timeout_error(e):
                    self._disable_forced_crossing("walker_spawn_timeout", e)
                    return started_count
                print(f"Warning: Forced crossing walker spawn failed: {e}")
                continue

            if walker is None:
                continue

            scripted_speed = self.rng.uniform(
                self.config.walker_speed_min,
                self.config.walker_speed_max,
            ) * self.rng.uniform(1.05, 1.35)
            timeout_steps = max(
                25,
                int(np.ceil(spec['path_length'] / max(scripted_speed, 0.1) / max(self.context.dt, 1e-3))) + 20
            )

            event_id = self.forced_crossing_event_seq
            self.forced_crossing_event_seq += 1
            event = {
                'event_id': event_id,
                'walker_id': walker.id,
                'spawn_location': spec['spawn_location'],
                'target_location': spec['target_location'],
                'direction': direction,
                'speed': scripted_speed,
                'path_length': spec['path_length'],
                'completion_margin': spec['completion_margin'],
                'timeout_step': self.context.time_step + timeout_steps,
            }
            self.active_forced_crossing_events[event_id] = event
            self.forced_crossing_walker_ids.append(walker.id)

            if not self._apply_forced_crossing_control(event, walker=walker):
                self._cleanup_forced_crossing_event(event_id, 'failed', 'initial_apply_control_failed')
                continue

            started_count += 1

        if started_count > 0:
            print(f"Forced crossing walkers spawned {started_count}/{count}, started={started_count}")
        return started_count


    def _maybe_trigger_forced_crossing(self, obs):
        if (
            self.context.ego is None
            or self.config.forced_crossing_walkers <= 0
            or self.state.forced_crossing_disabled
        ):
            return 0

        if self.context.time_step < self.config.forced_crossing_warmup_steps:
            return 0

        since_last = self.context.time_step - self.last_forced_crossing_step
        if since_last < self.config.forced_crossing_cooldown_steps:
            return 0

        speed = float(obs['ego_state'][3]) if obs is not None else 0.0
        if speed < self.config.forced_crossing_min_speed:
            return 0

        if self.rng.random() >= self.config.forced_crossing_trigger_prob:
            return 0

        remaining_budget = self.config.forced_crossing_max_total - self.state.forced_crossing_spawned
        if remaining_budget <= 0:
            return 0

        trigger_cap = min(self.config.forced_crossing_walkers, remaining_budget)
        trigger_count = self.rng.randint(1, trigger_cap)
        spawned = self._spawn_forced_crossing_walkers(
            count=trigger_count,
            distance_min=self.config.forced_crossing_distance_min,
            distance_max=self.config.forced_crossing_distance_max,
            lateral_min=self.config.forced_crossing_lateral_min,
            lateral_max=self.config.forced_crossing_lateral_max,
            tick_before_start=False,
        )
        if spawned > 0:
            self.last_forced_crossing_step = self.context.time_step
            self.state.forced_crossing_events += 1
            print(
                f"Info: Forced crossing triggered at step={self.context.time_step}, "
                f"spawned={spawned}, target={trigger_count}"
            )
        self.state.forced_crossing_spawned += spawned
        return spawned


