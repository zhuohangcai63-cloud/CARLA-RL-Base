from __future__ import annotations
from dataclasses import dataclass, asdict
import random
import numpy as np
import carla
from easycarla.control.ownership import ControlConflict
from .state import StateExporter

@dataclass(frozen=True)
class LeadBrakeConfig:
    hard_break_route_enabled: bool = False
    hard_break_lead_spawn_distance_min: float = 35.0
    hard_break_lead_spawn_distance_max: float = 50.0
    lead_brake_warmup_steps: int = 50
    lead_candidate_distance_min: float = 18.0
    lead_candidate_distance_max: float = 45.0
    lead_candidate_speed_min: float = 2.0
    lead_candidate_speed_max: float = 12.0
    lead_track_min_steps: int = 8
    lead_trigger_distance_min: float = 8.0
    lead_trigger_distance_max: float = 16.0
    lead_brake_strength: float = 1.0
    lead_brake_hold_steps: int = 30
    lead_brake_min_ego_speed: float = 1.5
    lead_event_max_per_episode: int = 1


@dataclass
class LeadBrakeState:
    lead_brake_enabled: bool = False
    hard_break_lead_spawned: bool = False
    hard_break_lead_spawn_distance: float | None = None
    hard_break_lead_spawn_failure: str = ''
    lead_brake_events: int = 0
    lead_brake_completed: int = 0
    lead_brake_aborted: int = 0
    lead_brake_triggered: bool = False
    lead_brake_abort_reason: str = ''
    lead_brake_last_state: str = 'idle'
    lead_brake_last_candidate_id: int | None = None
    lead_brake_last_distance: float | None = None
    lead_brake_last_relative_speed: float | None = None
    lead_brake_last_trigger_step: int | None = None
    lead_brake_trigger_ego_speed: float | None = None


class LeadBrakeEvent:
    """Owned lead_brake event configuration, state and explicit lifecycle."""
    kind = 'lead_brake'
    def __init__(self, context, params, seed, vehicles):
        self.context = context
        self.rng = random.Random(seed)
        values = {
            'hard_break_route_enabled': params.get('hard_break_route_enabled', False),
            'hard_break_lead_spawn_distance_min': params.get('hard_break_lead_spawn_distance_min', 35.0),
            'hard_break_lead_spawn_distance_max': params.get('hard_break_lead_spawn_distance_max', 50.0),
            'lead_brake_warmup_steps': round(float(params.get('lead_brake_warmup_steps', 50)) * 0.1 / context.dt),
            'lead_candidate_distance_min': params.get('lead_candidate_distance_min', 18.0),
            'lead_candidate_distance_max': params.get('lead_candidate_distance_max', 45.0),
            'lead_candidate_speed_min': params.get('lead_candidate_speed_min', 2.0),
            'lead_candidate_speed_max': params.get('lead_candidate_speed_max', 12.0),
            'lead_track_min_steps': round(float(params.get('lead_track_min_steps', 8)) * 0.1 / context.dt),
            'lead_trigger_distance_min': params.get('lead_trigger_distance_min', 8.0),
            'lead_trigger_distance_max': params.get('lead_trigger_distance_max', 16.0),
            'lead_brake_strength': params.get('lead_brake_strength', 1.0),
            'lead_brake_hold_steps': round(float(params.get('lead_brake_hold_steps', 30)) * 0.1 / context.dt),
            'lead_brake_min_ego_speed': params.get('lead_brake_min_ego_speed', 1.5),
            'lead_event_max_per_episode': int(params.get('lead_event_max_per_episode', 1)),
        }
        self.config = LeadBrakeConfig(**values)
        self.state = LeadBrakeState()
        self.spawned_vehicles = list(vehicles)
        self.hard_break_lead_vehicle_id = None
        self.active_lead_brake_event = None
        self.state.lead_brake_enabled = self.rng.random() < params.get("lead_brake_enable_prob", 0.)
        self._prepared = False
        self.prepare()

    def prepare(self):
        if not self._prepared:
            self._spawn_hard_break_lead_vehicle()
            self._prepared = True

    def close(self):
        event = self.active_lead_brake_event
        if event is not None:
            actor = self.context.world.get_actor(event["vehicle_id"])
            if actor is not None and actor.is_alive:
                self._set_vehicle_autopilot(actor, True)
        self.active_lead_brake_event = None
        if self.hard_break_lead_vehicle_id is not None:
            self.context._destroy_actor_ids_batch([self.hard_break_lead_vehicle_id], label="lead_brake", do_tick=False)
            self.hard_break_lead_vehicle_id = None

    def export_state(self, actor_roles):
        exporter = StateExporter(actor_roles)
        event_fields = ("vehicle_id", "state", "track_steps", "distance", "relative_speed",
                        "brake_end_step", "trigger_step", "steer")
        state = exporter.value(asdict(self.state))
        state.update({"prepared": self._prepared,
                      "dedicated_actor": exporter.actor(self.hard_break_lead_vehicle_id),
                      "candidate_actors": [exporter.actor(actor.id) for actor in self.spawned_vehicles],
                      "event": None if self.active_lead_brake_event is None else
                               exporter.fields(self.active_lead_brake_event, event_fields)})
        return {"schema_version": 1, "kind": self.kind, "config": asdict(self.config), "state": state,
                "rng_state": exporter.value(self.rng.getstate()), "limitations": exporter.limitations}

    def before_tick(self):
        self._maybe_start_lead_brake_tracking()
        self._apply_active_lead_brake_control()

    def after_tick(self):
        self._update_lead_brake_event()

    def info(self):
        return {**asdict(self.config), **asdict(self.state)}


    def _vehicle_spawn_area_is_clear(self, location, min_distance=7.0):
        for vehicle in self.context.world.get_actors().filter('vehicle.*'):
            if self.context.ego is not None and vehicle.id == self.context.ego.id:
                continue
            try:
                if vehicle.get_location().distance(location) < min_distance:
                    return False
            except ControlConflict:
                raise
            except RuntimeError:
                continue
        return True


    def _spawn_hard_break_lead_vehicle(self):
        if not self.config.hard_break_route_enabled or self.context.ego is None:
            return False

        self.state.hard_break_lead_spawn_failure = ""
        world_map = self.context.world.get_map()
        ego_waypoint = world_map.get_waypoint(
            self.context.ego.get_location(), project_to_road=True, lane_type=carla.LaneType.Driving
        )
        if ego_waypoint is None or ego_waypoint.is_intersection:
            self.state.hard_break_lead_spawn_failure = "ego_not_on_driving_lane"
            return False

        distance_min = max(0.0, float(self.config.hard_break_lead_spawn_distance_min))
        distance_max = max(distance_min, float(self.config.hard_break_lead_spawn_distance_max))
        sampled_distance = self.rng.uniform(distance_min, distance_max)
        candidate_distances = [
            sampled_distance,
            distance_min,
            distance_max,
            (distance_min + distance_max) * 0.5,
            12.0,
            16.0,
            20.0,
            24.0,
            28.0,
            32.0,
            36.0,
            42.0,
            distance_max + 5.0,
            distance_min + 5.0,
        ]

        blueprints = self._get_npc_vehicle_blueprints()
        if not blueprints:
            self.state.hard_break_lead_spawn_failure = "no_vehicle_blueprints"
            return False

        for spawn_distance in candidate_distances:
            lead_waypoint = self.context._advance_waypoint_along_lane(ego_waypoint, spawn_distance)
            if lead_waypoint is None or lead_waypoint.is_intersection:
                continue

            spawn_transform = carla.Transform(
                lead_waypoint.transform.location + carla.Location(z=0.35),
                lead_waypoint.transform.rotation,
            )
            if not self._vehicle_spawn_area_is_clear(spawn_transform.location):
                continue

            blueprint = self.rng.choice(blueprints)
            if blueprint.has_attribute('color'):
                blueprint.set_attribute('color', '0,0,255')
            blueprint.set_attribute('role_name', 'hard_break_lead')

            try:
                lead_vehicle = self.context.world.try_spawn_actor(blueprint, spawn_transform)
            except ControlConflict:
                raise
            except RuntimeError as exc:
                self.state.hard_break_lead_spawn_failure = f"spawn_exception:{exc}"
                continue

            if lead_vehicle is None:
                continue

            self.hard_break_lead_vehicle_id = lead_vehicle.id
            self.state.hard_break_lead_spawned = True
            self.state.hard_break_lead_spawn_distance = float(spawn_distance)
            self.spawned_vehicles.append(lead_vehicle)

            try:
                self.context.set_autopilot(lead_vehicle, True, source=self.kind)
            except ControlConflict:
                raise
            except RuntimeError as exc:
                self.state.hard_break_lead_spawn_failure = f"autopilot_enable_failed:{exc}"

            print(
                f"HardBreakRoute lead vehicle spawned id={lead_vehicle.id} "
                f"distance={spawn_distance:.1f}m"
            )
            return True

        self.state.hard_break_lead_spawn_failure = "no_clear_same_lane_spawn"
        return False


    def _get_npc_vehicle_blueprints(self):
        blueprints = self.context.world.get_blueprint_library().filter('vehicle.*')
        filtered = []

        for bp in blueprints:
            if not bp.has_attribute('number_of_wheels'):
                continue
            if int(bp.get_attribute('number_of_wheels')) != 4:
                continue
            if bp.id.lower() in self.context.npc_vehicle_blacklist:
                continue
            filtered.append(bp)

        return filtered


    def _set_vehicle_autopilot(self, vehicle, enabled):
        try:
            self.context.set_autopilot(vehicle, bool(enabled), source=self.kind)
        except ControlConflict:
            raise
        except RuntimeError as e:
            print(f"Warning: Failed to set autopilot={enabled} for vehicle {vehicle.id}: {e}")
            return False
        return True


    def _build_lead_brake_candidate_metrics(self, vehicle, ego_transform=None, ego_waypoint=None):
        if self.context.ego is None or vehicle is None:
            return None

        try:
            if not vehicle.is_alive:
                return None
        except ControlConflict:
            raise
        except Exception:
            return None

        if vehicle.id == self.context.ego.id:
            return None

        if ego_transform is None:
            ego_transform = self.context.ego.get_transform()

        world_map = self.context.world.get_map()
        if ego_waypoint is None:
            ego_waypoint = world_map.get_waypoint(
                self.context.ego.get_location(), project_to_road=True, lane_type=carla.LaneType.Driving
            )
        if ego_waypoint is None:
            return None

        vehicle_transform = vehicle.get_transform()
        local_x, local_y = self.context._world_to_ego_local(ego_transform, vehicle_transform.location)
        if (
            local_x < self.config.lead_candidate_distance_min
            or local_x > self.config.lead_candidate_distance_max
        ):
            return None

        lane_width = max(2.5, float(ego_waypoint.lane_width))
        lateral_limit = max(1.2, min(2.5, lane_width * 0.45))
        if abs(local_y) > lateral_limit:
            return None

        vehicle_speed = self.context._actor_speed(vehicle)
        if (
            vehicle_speed < self.config.lead_candidate_speed_min
            or vehicle_speed > self.config.lead_candidate_speed_max
        ):
            return None

        vehicle_waypoint = world_map.get_waypoint(
            vehicle_transform.location, project_to_road=True, lane_type=carla.LaneType.Driving
        )
        if vehicle_waypoint is None:
            return None

        if (
            vehicle_waypoint.road_id != ego_waypoint.road_id
            or vehicle_waypoint.lane_id != ego_waypoint.lane_id
        ):
            return None

        heading_diff = self.context._yaw_diff_deg(
            vehicle_transform.rotation.yaw,
            ego_transform.rotation.yaw,
        )
        if heading_diff > 25.0:
            return None

        ego_speed = self.context._actor_speed(self.context.ego)
        return {
            'vehicle_id': vehicle.id,
            'distance': float(local_x),
            'lateral_offset': float(local_y),
            'speed': vehicle_speed,
            'relative_speed': float(ego_speed - vehicle_speed),
        }


    def _find_lead_brake_candidate(self):
        if self.context.ego is None:
            return None

        ego_transform = self.context.ego.get_transform()
        ego_waypoint = self.context.world.get_map().get_waypoint(
            self.context.ego.get_location(), project_to_road=True, lane_type=carla.LaneType.Driving
        )
        if ego_waypoint is None:
            return None

        if self.config.hard_break_route_enabled and self.hard_break_lead_vehicle_id is not None:
            lead_vehicle = self.context.world.get_actor(self.hard_break_lead_vehicle_id)
            metrics = self._build_lead_brake_candidate_metrics(
                lead_vehicle,
                ego_transform=ego_transform,
                ego_waypoint=ego_waypoint,
            )
            if metrics is not None:
                return metrics

        best_metrics = None
        for vehicle in self.spawned_vehicles:
            metrics = self._build_lead_brake_candidate_metrics(
                vehicle,
                ego_transform=ego_transform,
                ego_waypoint=ego_waypoint,
            )
            if metrics is None:
                continue
            if best_metrics is None or metrics['distance'] < best_metrics['distance']:
                best_metrics = metrics

        return best_metrics


    def _abort_lead_brake_event(self, reason):
        event = self.active_lead_brake_event
        if event is None:
            self.state.lead_brake_abort_reason = reason
            self.state.lead_brake_last_state = "aborted"
            self.state.lead_brake_last_candidate_id = None
            return False

        vehicle = self.context.world.get_actor(event['vehicle_id'])
        if vehicle is not None and vehicle.is_alive:
            self._set_vehicle_autopilot(vehicle, True)
        self.active_lead_brake_event = None
        self.state.lead_brake_aborted += 1
        self.state.lead_brake_abort_reason = reason
        self.state.lead_brake_last_state = "aborted"
        self.state.lead_brake_last_candidate_id = event.get('vehicle_id')
        return True


    def _maybe_start_lead_brake_tracking(self):
        if (
            self.context.ego is None
            or not self.state.lead_brake_enabled
            or self.state.lead_brake_events >= self.config.lead_event_max_per_episode
            or self.active_lead_brake_event is not None
            or self.context.time_step < self.config.lead_brake_warmup_steps
        ):
            return False

        candidate = self._find_lead_brake_candidate()
        if candidate is None:
            return False

        self.active_lead_brake_event = {
            'vehicle_id': candidate['vehicle_id'],
            'state': 'tracking_candidate',
            'track_steps': 1,
            'distance': candidate['distance'],
            'relative_speed': candidate['relative_speed'],
            'brake_end_step': None,
            'trigger_step': None,
            'steer': 0.0,
        }
        self.state.lead_brake_last_state = 'tracking_candidate'
        self.state.lead_brake_last_candidate_id = candidate['vehicle_id']
        self.state.lead_brake_last_distance = candidate['distance']
        self.state.lead_brake_last_relative_speed = candidate['relative_speed']
        self.state.lead_brake_abort_reason = ""
        return True


    def _apply_active_lead_brake_control(self):
        event = self.active_lead_brake_event
        if event is None or event.get('state') != 'braking':
            return

        vehicle = self.context.world.get_actor(event['vehicle_id'])
        if vehicle is None:
            self._abort_lead_brake_event("lead_vehicle_missing_before_tick")
            return

        control = carla.VehicleControl(
            throttle=0.0,
            steer=float(event.get('steer', 0.0)),
            brake=float(self.config.lead_brake_strength),
        )
        try:
            self.context.control(vehicle, control, source=self.kind)
        except ControlConflict:
            raise
        except RuntimeError as e:
            self._abort_lead_brake_event(f"lead_brake_apply_failed:{e}")


    def _update_lead_brake_event(self):
        event = self.active_lead_brake_event
        triggered_this_step = False
        completed_this_step = False
        aborted_this_step = False

        if event is None:
            return triggered_this_step, completed_this_step, aborted_this_step

        vehicle = self.context.world.get_actor(event['vehicle_id'])
        if vehicle is None:
            aborted_this_step = self._abort_lead_brake_event("lead_vehicle_missing_after_tick")
            return triggered_this_step, completed_this_step, aborted_this_step

        vehicle_transform = vehicle.get_transform()
        ego_transform = self.context.ego.get_transform() if self.context.ego is not None else None
        if ego_transform is not None:
            local_x, _ = self.context._world_to_ego_local(ego_transform, vehicle_transform.location)
            ego_speed = self.context._actor_speed(self.context.ego)
            vehicle_speed = self.context._actor_speed(vehicle)
            event['distance'] = float(local_x)
            event['relative_speed'] = float(ego_speed - vehicle_speed)
            self.state.lead_brake_last_distance = event['distance']
            self.state.lead_brake_last_relative_speed = event['relative_speed']

        if event['state'] == 'tracking_candidate':
            candidate_metrics = self._build_lead_brake_candidate_metrics(vehicle)
            if candidate_metrics is None:
                aborted_this_step = self._abort_lead_brake_event("candidate_no_longer_valid")
                return triggered_this_step, completed_this_step, aborted_this_step

            event['track_steps'] += 1
            event['distance'] = candidate_metrics['distance']
            event['relative_speed'] = candidate_metrics['relative_speed']
            self.state.lead_brake_last_state = 'tracking_candidate'
            self.state.lead_brake_last_candidate_id = candidate_metrics['vehicle_id']
            self.state.lead_brake_last_distance = candidate_metrics['distance']
            self.state.lead_brake_last_relative_speed = candidate_metrics['relative_speed']

            if event['track_steps'] < self.config.lead_track_min_steps:
                return triggered_this_step, completed_this_step, aborted_this_step

            distance = candidate_metrics['distance']
            if distance > self.config.lead_trigger_distance_max:
                return triggered_this_step, completed_this_step, aborted_this_step
            if (
                not self.config.hard_break_route_enabled
                and distance < self.config.lead_trigger_distance_min
            ):
                return triggered_this_step, completed_this_step, aborted_this_step

            ego_speed = self.context._actor_speed(self.context.ego)
            if ego_speed < self.config.lead_brake_min_ego_speed:
                self.state.lead_brake_last_state = 'waiting_for_ego_speed'
                event['state'] = 'tracking_candidate'
                return triggered_this_step, completed_this_step, aborted_this_step

            current_control = vehicle.get_control()
            if not self._set_vehicle_autopilot(vehicle, False):
                aborted_this_step = self._abort_lead_brake_event("autopilot_disable_failed")
                return triggered_this_step, completed_this_step, aborted_this_step

            event['state'] = 'braking'
            event['trigger_step'] = self.context.time_step
            event['brake_end_step'] = self.context.time_step + self.config.lead_brake_hold_steps
            event['steer'] = float(current_control.steer)
            self.state.lead_brake_events += 1
            self.state.lead_brake_triggered = True
            self.state.lead_brake_last_trigger_step = self.context.time_step
            self.state.lead_brake_trigger_ego_speed = ego_speed
            self.state.lead_brake_last_state = 'braking'
            triggered_this_step = True
            self._apply_active_lead_brake_control()
            return triggered_this_step, completed_this_step, aborted_this_step

        if event['state'] == 'braking':
            self.state.lead_brake_last_state = 'braking'
            if self.context.time_step >= event['brake_end_step']:
                self._set_vehicle_autopilot(vehicle, True)
                self.active_lead_brake_event = None
                self.state.lead_brake_completed += 1
                self.state.lead_brake_last_state = 'done'
                completed_this_step = True

        return triggered_this_step, completed_this_step, aborted_this_step


