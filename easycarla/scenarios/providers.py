from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
import random
from typing import Any

import carla


@dataclass
class ScenarioSpec:
    town: str | None = None
    weather: carla.WeatherParameters | None = None
    ego_spawn_point: carla.Transform | None = None
    destination_transform: carla.Transform | None = None
    traffic_light_policy: str | None = None
    number_of_vehicles: int | None = None
    number_of_walkers: int | None = None
    min_route_distance: float | None = None
    pedestrian_cross_factor: float | None = None
    walker_speed_min: float | None = None
    walker_speed_max: float | None = None
    forced_crossing_walkers: int | None = None
    forced_crossing_distance_min: float | None = None
    forced_crossing_distance_max: float | None = None
    forced_crossing_lateral_min: float | None = None
    forced_crossing_lateral_max: float | None = None
    forced_crossing_trigger_prob: float | None = None
    forced_crossing_cooldown_steps: int | None = None
    forced_crossing_warmup_steps: int | None = None
    forced_crossing_min_speed: float | None = None
    forced_crossing_max_total: int | None = None
    lead_brake_enable_prob: float | None = None
    lead_brake_warmup_steps: int | None = None
    lead_candidate_distance_min: float | None = None
    lead_candidate_distance_max: float | None = None
    lead_candidate_speed_min: float | None = None
    lead_candidate_speed_max: float | None = None
    lead_track_min_steps: int | None = None
    lead_trigger_distance_min: float | None = None
    lead_trigger_distance_max: float | None = None
    lead_brake_strength: float | None = None
    lead_brake_hold_steps: int | None = None
    lead_brake_min_ego_speed: float | None = None
    lead_event_max_per_episode: int | None = None
    hard_break_route_enabled: bool | None = None
    hard_break_lead_spawn_distance_min: float | None = None
    hard_break_lead_spawn_distance_max: float | None = None
    long_tail_events: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


class ScenarioProvider(ABC):
    @abstractmethod
    def sample(self, env: Any) -> ScenarioSpec:
        raise NotImplementedError


class RandomScenarioProvider(ScenarioProvider):
    def __init__(self, min_route_distance: float = 50.0):
        self.min_route_distance = float(min_route_distance)

    def sample(self, env: Any) -> ScenarioSpec:
        spawn_points = list(getattr(env, "vehicle_spawn_points", []))
        if not spawn_points:
            return ScenarioSpec(
                town=getattr(env, "town_name", None),
                weather=carla.WeatherParameters.ClearNoon,
                traffic_light_policy=getattr(env, "traffic", None),
                number_of_vehicles=getattr(env, "number_of_vehicles", None),
                number_of_walkers=getattr(env, "number_of_walkers", None),
                min_route_distance=self.min_route_distance,
                pedestrian_cross_factor=getattr(env, "pedestrian_cross_factor", None),
                walker_speed_min=getattr(env, "walker_speed_min", None),
                walker_speed_max=getattr(env, "walker_speed_max", None),
                forced_crossing_walkers=getattr(env, "forced_crossing_walkers", None),
                forced_crossing_distance_min=getattr(env, "forced_crossing_distance_min", None),
                forced_crossing_distance_max=getattr(env, "forced_crossing_distance_max", None),
                forced_crossing_lateral_min=getattr(env, "forced_crossing_lateral_min", None),
                forced_crossing_lateral_max=getattr(env, "forced_crossing_lateral_max", None),
                forced_crossing_trigger_prob=getattr(env, "forced_crossing_trigger_prob", None),
                forced_crossing_cooldown_steps=getattr(env, "forced_crossing_cooldown_steps", None),
                forced_crossing_warmup_steps=getattr(env, "forced_crossing_warmup_steps", None),
                forced_crossing_min_speed=getattr(env, "forced_crossing_min_speed", None),
                forced_crossing_max_total=getattr(env, "forced_crossing_max_total", None),
                lead_brake_enable_prob=getattr(env, "lead_brake_enable_prob", None),
                lead_brake_warmup_steps=getattr(env, "lead_brake_warmup_steps", None),
                lead_candidate_distance_min=getattr(env, "lead_candidate_distance_min", None),
                lead_candidate_distance_max=getattr(env, "lead_candidate_distance_max", None),
                lead_candidate_speed_min=getattr(env, "lead_candidate_speed_min", None),
                lead_candidate_speed_max=getattr(env, "lead_candidate_speed_max", None),
                lead_track_min_steps=getattr(env, "lead_track_min_steps", None),
                lead_trigger_distance_min=getattr(env, "lead_trigger_distance_min", None),
                lead_trigger_distance_max=getattr(env, "lead_trigger_distance_max", None),
                lead_brake_strength=getattr(env, "lead_brake_strength", None),
                lead_brake_hold_steps=getattr(env, "lead_brake_hold_steps", None),
                lead_brake_min_ego_speed=getattr(env, "lead_brake_min_ego_speed", None),
                lead_event_max_per_episode=getattr(env, "lead_event_max_per_episode", None),
                hard_break_route_enabled=getattr(env, "hard_break_route_enabled", None),
                hard_break_lead_spawn_distance_min=getattr(env, "hard_break_lead_spawn_distance_min", None),
                hard_break_lead_spawn_distance_max=getattr(env, "hard_break_lead_spawn_distance_max", None),
                long_tail_events=list(getattr(env, "long_tail_event_specs", []) or []),
                metadata={"backend": "random", "reason": "no_spawn_points"},
            )

        ego_spawn = random.choice(spawn_points)
        destination_transform = self._pick_destination(spawn_points, ego_spawn)
        return ScenarioSpec(
            town=getattr(env, "town_name", None),
            weather=carla.WeatherParameters.ClearNoon,
            ego_spawn_point=ego_spawn,
            destination_transform=destination_transform,
            traffic_light_policy=getattr(env, "traffic", None),
            number_of_vehicles=getattr(env, "number_of_vehicles", None),
            number_of_walkers=getattr(env, "number_of_walkers", None),
            min_route_distance=self.min_route_distance,
            pedestrian_cross_factor=getattr(env, "pedestrian_cross_factor", None),
            walker_speed_min=getattr(env, "walker_speed_min", None),
            walker_speed_max=getattr(env, "walker_speed_max", None),
            forced_crossing_walkers=getattr(env, "forced_crossing_walkers", None),
            forced_crossing_distance_min=getattr(env, "forced_crossing_distance_min", None),
            forced_crossing_distance_max=getattr(env, "forced_crossing_distance_max", None),
            forced_crossing_lateral_min=getattr(env, "forced_crossing_lateral_min", None),
            forced_crossing_lateral_max=getattr(env, "forced_crossing_lateral_max", None),
            forced_crossing_trigger_prob=getattr(env, "forced_crossing_trigger_prob", None),
            forced_crossing_cooldown_steps=getattr(env, "forced_crossing_cooldown_steps", None),
            forced_crossing_warmup_steps=getattr(env, "forced_crossing_warmup_steps", None),
            forced_crossing_min_speed=getattr(env, "forced_crossing_min_speed", None),
            forced_crossing_max_total=getattr(env, "forced_crossing_max_total", None),
            lead_brake_enable_prob=getattr(env, "lead_brake_enable_prob", None),
            lead_brake_warmup_steps=getattr(env, "lead_brake_warmup_steps", None),
            lead_candidate_distance_min=getattr(env, "lead_candidate_distance_min", None),
            lead_candidate_distance_max=getattr(env, "lead_candidate_distance_max", None),
            lead_candidate_speed_min=getattr(env, "lead_candidate_speed_min", None),
            lead_candidate_speed_max=getattr(env, "lead_candidate_speed_max", None),
            lead_track_min_steps=getattr(env, "lead_track_min_steps", None),
            lead_trigger_distance_min=getattr(env, "lead_trigger_distance_min", None),
            lead_trigger_distance_max=getattr(env, "lead_trigger_distance_max", None),
            lead_brake_strength=getattr(env, "lead_brake_strength", None),
            lead_brake_hold_steps=getattr(env, "lead_brake_hold_steps", None),
            lead_brake_min_ego_speed=getattr(env, "lead_brake_min_ego_speed", None),
            lead_event_max_per_episode=getattr(env, "lead_event_max_per_episode", None),
            hard_break_route_enabled=getattr(env, "hard_break_route_enabled", None),
            hard_break_lead_spawn_distance_min=getattr(env, "hard_break_lead_spawn_distance_min", None),
            hard_break_lead_spawn_distance_max=getattr(env, "hard_break_lead_spawn_distance_max", None),
            long_tail_events=list(getattr(env, "long_tail_event_specs", []) or []),
            metadata={
                "backend": "random",
                "min_route_distance": self.min_route_distance,
            },
        )

    def _pick_destination(
        self,
        spawn_points: list[carla.Transform],
        ego_spawn: carla.Transform | None,
    ) -> carla.Transform | None:
        if ego_spawn is None:
            return random.choice(spawn_points) if spawn_points else None

        candidates = spawn_points.copy()
        random.shuffle(candidates)
        for destination in candidates:
            if ego_spawn.location.distance(destination.location) >= self.min_route_distance:
                return destination

        return random.choice(spawn_points) if spawn_points else None


class ScenicScenarioProvider(ScenarioProvider):
    def __init__(
        self,
        scenic_file: str | None,
        scenic_params: dict[str, Any] | None = None,
        scenic_model: str | None = None,
        scenic_scenario: str | None = None,
        scenic_mode2d: bool = False,
        scenic_max_iterations: int = 2000,
        fallback_provider: ScenarioProvider | None = None,
    ):
        self.scenic_file = self._resolve_scenic_file(scenic_file)
        self.scenic_params = dict(scenic_params or {})
        self.scenic_model = scenic_model
        self.scenic_scenario = scenic_scenario
        self.scenic_mode2d = bool(scenic_mode2d)
        self.scenic_max_iterations = int(scenic_max_iterations)
        self.fallback_provider = fallback_provider

        self._compiled_scenario = None
        self._compile_error: str | None = None
        self._compile_attempted = False

    @staticmethod
    def _resolve_scenic_file(scenic_file: str | None) -> Path | None:
        if not scenic_file:
            return None

        raw_path = Path(scenic_file).expanduser()
        if raw_path.is_absolute():
            return raw_path.resolve()

        cwd_candidate = (Path.cwd() / raw_path).resolve()
        if cwd_candidate.exists():
            return cwd_candidate

        project_root = Path(__file__).resolve().parents[2]
        return (project_root / raw_path).resolve()

    def sample(self, env: Any) -> ScenarioSpec:
        try:
            scenario = self._ensure_compiled(env)
            scene, iterations = scenario.generate(maxIterations=self.scenic_max_iterations)
            return self._spec_from_scene(scene, iterations, env)
        except Exception as exc:
            return self._fallback(env, exc)

    def _ensure_compiled(self, env: Any):
        if self._compiled_scenario is not None:
            return self._compiled_scenario
        if self._compile_attempted and self._compile_error is not None:
            raise RuntimeError(self._compile_error)

        self._compile_attempted = True
        if self.scenic_file is None:
            self._compile_error = "scenic_file is not configured"
            raise RuntimeError(self._compile_error)
        if not self.scenic_file.exists():
            self._compile_error = f"Scenic file not found: {self.scenic_file}"
            raise FileNotFoundError(self._compile_error)

        try:
            import scenic  # type: ignore
        except Exception as exc:
            self._compile_error = f"Failed to import scenic: {exc}"
            raise RuntimeError(self._compile_error) from exc

        compile_params = dict(self.scenic_params)
        if "carla_map" not in compile_params and hasattr(env, "town_name"):
            compile_params["carla_map"] = getattr(env, "town_name")
        if "town" not in compile_params and hasattr(env, "town_name"):
            compile_params["town"] = getattr(env, "town_name")
        if "port" not in compile_params and hasattr(env, "port"):
            compile_params["port"] = getattr(env, "port")

        kwargs: dict[str, Any] = {"params": compile_params}
        if self.scenic_model:
            kwargs["model"] = self.scenic_model
        if self.scenic_scenario:
            kwargs["scenario"] = self.scenic_scenario
        if self.scenic_mode2d:
            kwargs["mode2D"] = True

        try:
            self._compiled_scenario = scenic.scenarioFromFile(str(self.scenic_file), **kwargs)
        except Exception as exc:
            self._compile_error = f"Failed to compile scenic file '{self.scenic_file}': {exc}"
            raise RuntimeError(self._compile_error) from exc
        return self._compiled_scenario

    def _spec_from_scene(self, scene: Any, iterations: int, env: Any) -> ScenarioSpec:
        scene_params = dict(getattr(scene, "params", {}) or {})
        spawn_points = list(getattr(env, "vehicle_spawn_points", []))

        weather = self._parse_weather(self._pick(scene_params, "weather", "weather_preset"))
        if weather is None:
            weather = carla.WeatherParameters.ClearNoon

        ego_spawn = self._pick_transform(
            spawn_points=spawn_points,
            idx_value=self._pick(scene_params, "ego_spawn_index", "ego_index"),
            transform_value=self._pick(scene_params, "ego_spawn", "ego_transform"),
        )
        if ego_spawn is None:
            ego_spawn = self._transform_from_components(scene_params, "ego")

        destination = self._pick_transform(
            spawn_points=spawn_points,
            idx_value=self._pick(scene_params, "destination_spawn_index", "destination_index", "dest_index"),
            transform_value=self._pick(scene_params, "destination", "destination_transform", "dest_transform"),
        )
        if destination is None:
            destination = self._transform_from_components(scene_params, "destination")
            if destination is None:
                destination = self._transform_from_components(scene_params, "dest")

        metadata = {
            "backend": "scenic",
            "scenic_file": str(self.scenic_file) if self.scenic_file else None,
            "scenic_iterations": int(iterations),
        }
        for key in ("scenario_name", "scenario_id", "difficulty"):
            if key in scene_params:
                metadata[key] = scene_params[key]

        return ScenarioSpec(
            town=self._pick(scene_params, "town", "carla_map") or getattr(env, "town_name", None),
            weather=weather,
            ego_spawn_point=ego_spawn,
            destination_transform=destination,
            traffic_light_policy=self._pick(scene_params, "traffic_light_policy", "traffic"),
            number_of_vehicles=self._to_int(self._pick(scene_params, "number_of_vehicles", "npc_vehicles")),
            number_of_walkers=self._to_int(self._pick(scene_params, "number_of_walkers", "npc_walkers")),
            min_route_distance=self._to_float(
                self._pick(scene_params, "min_route_distance", "route_min_distance")
            ),
            pedestrian_cross_factor=self._to_float(
                self._pick(scene_params, "pedestrian_cross_factor", "walker_cross_factor")
            ),
            walker_speed_min=self._to_float(
                self._pick(scene_params, "walker_speed_min", "pedestrian_speed_min")
            ),
            walker_speed_max=self._to_float(
                self._pick(scene_params, "walker_speed_max", "pedestrian_speed_max")
            ),
            forced_crossing_walkers=self._to_int(
                self._pick(
                    scene_params,
                    "forced_crossing_walkers",
                    "front_crossing_walkers",
                    "forced_crossing_pedestrians",
                )
            ),
            forced_crossing_distance_min=self._to_float(
                self._pick(scene_params, "forced_crossing_distance_min", "front_crossing_distance_min")
            ),
            forced_crossing_distance_max=self._to_float(
                self._pick(scene_params, "forced_crossing_distance_max", "front_crossing_distance_max")
            ),
            forced_crossing_lateral_min=self._to_float(
                self._pick(scene_params, "forced_crossing_lateral_min", "front_crossing_lateral_min")
            ),
            forced_crossing_lateral_max=self._to_float(
                self._pick(scene_params, "forced_crossing_lateral_max", "front_crossing_lateral_max")
            ),
            forced_crossing_trigger_prob=self._to_float(
                self._pick(scene_params, "forced_crossing_trigger_prob", "front_crossing_trigger_prob")
            ),
            forced_crossing_cooldown_steps=self._to_int(
                self._pick(scene_params, "forced_crossing_cooldown_steps", "front_crossing_cooldown_steps")
            ),
            forced_crossing_warmup_steps=self._to_int(
                self._pick(scene_params, "forced_crossing_warmup_steps", "front_crossing_warmup_steps")
            ),
            forced_crossing_min_speed=self._to_float(
                self._pick(scene_params, "forced_crossing_min_speed", "front_crossing_min_speed")
            ),
            forced_crossing_max_total=self._to_int(
                self._pick(scene_params, "forced_crossing_max_total", "front_crossing_max_total")
            ),
            lead_brake_enable_prob=self._to_float(
                self._pick(scene_params, "lead_brake_enable_prob")
            ),
            lead_brake_warmup_steps=self._to_int(
                self._pick(scene_params, "lead_brake_warmup_steps")
            ),
            lead_candidate_distance_min=self._to_float(
                self._pick(scene_params, "lead_candidate_distance_min")
            ),
            lead_candidate_distance_max=self._to_float(
                self._pick(scene_params, "lead_candidate_distance_max")
            ),
            lead_candidate_speed_min=self._to_float(
                self._pick(scene_params, "lead_candidate_speed_min")
            ),
            lead_candidate_speed_max=self._to_float(
                self._pick(scene_params, "lead_candidate_speed_max")
            ),
            lead_track_min_steps=self._to_int(
                self._pick(scene_params, "lead_track_min_steps")
            ),
            lead_trigger_distance_min=self._to_float(
                self._pick(scene_params, "lead_trigger_distance_min")
            ),
            lead_trigger_distance_max=self._to_float(
                self._pick(scene_params, "lead_trigger_distance_max")
            ),
            lead_brake_strength=self._to_float(
                self._pick(scene_params, "lead_brake_strength")
            ),
            lead_brake_hold_steps=self._to_int(
                self._pick(scene_params, "lead_brake_hold_steps")
            ),
            lead_brake_min_ego_speed=self._to_float(
                self._pick(scene_params, "lead_brake_min_ego_speed", "hard_break_min_ego_speed")
            ),
            lead_event_max_per_episode=self._to_int(
                self._pick(scene_params, "lead_event_max_per_episode")
            ),
            hard_break_route_enabled=self._to_bool(
                self._pick(scene_params, "hard_break_route_enabled", "scripted_lead_vehicle")
            ),
            hard_break_lead_spawn_distance_min=self._to_float(
                self._pick(scene_params, "hard_break_lead_spawn_distance_min", "hard_break_spawn_distance_min")
            ),
            hard_break_lead_spawn_distance_max=self._to_float(
                self._pick(scene_params, "hard_break_lead_spawn_distance_max", "hard_break_spawn_distance_max")
            ),
            long_tail_events=self._build_long_tail_events(scene_params),
            metadata=metadata,
        )

    def _build_long_tail_events(self, scene_params: dict[str, Any]) -> list[dict[str, Any]]:
        raw_events = self._pick(scene_params, "long_tail_events")
        if isinstance(raw_events, list):
            return [dict(event) for event in raw_events if isinstance(event, dict)]

        raw_type = self._pick(
            scene_params,
            "long_tail_event_type",
            "event_type",
            "scenario_name",
        )
        if raw_type is None:
            return []

        event_type = self._normalize_event_type(str(raw_type))
        if event_type is None:
            return []

        event: dict[str, Any] = {
            "type": event_type,
            "name": str(self._pick(scene_params, "scenario_name", "scenario_id") or event_type),
        }

        for source_key, target_key in (
            ("direction", "direction"),
            ("allowed_towns", "allowed_towns"),
            ("fallback_event", "fallback_event"),
        ):
            value = self._pick(scene_params, source_key)
            if value is not None:
                event[target_key] = value

        float_mappings = {
            "event_distance_min": ("event_distance_min", "distance_min", "long_tail_distance_min"),
            "event_distance_max": ("event_distance_max", "distance_max", "long_tail_distance_max"),
            "trigger_distance_min": ("trigger_distance_min",),
            "trigger_distance_max": ("trigger_distance_max",),
            "reaction_time": ("reaction_time", "time_to_arrival"),
            "actor_speed_min": ("actor_speed_min", "speed_min"),
            "actor_speed_max": ("actor_speed_max", "speed_max"),
            "hold_seconds": ("hold_seconds",),
            "enabled_prob": ("enabled_prob", "probability"),
        }
        for target_key, aliases in float_mappings.items():
            value = self._to_float(self._pick(scene_params, *aliases))
            if value is not None:
                event[target_key] = value

        distance = self._to_float(self._pick(scene_params, "distance"))
        if distance is not None:
            event.setdefault("event_distance_min", distance)
            event.setdefault("event_distance_max", distance)

        speed = self._to_float(self._pick(scene_params, "speed"))
        if speed is not None:
            meters_per_second = speed / 3.6 if speed > 25.0 else speed
            event.setdefault("actor_speed_min", meters_per_second)
            event.setdefault("actor_speed_max", meters_per_second)

        timeout_steps = self._to_int(self._pick(scene_params, "event_timeout_steps", "timeout_steps"))
        if timeout_steps is not None:
            event["event_timeout_steps"] = timeout_steps

        allow_skip = self._to_bool(self._pick(scene_params, "allow_skip_if_incompatible"))
        if allow_skip is not None:
            event["allow_skip_if_incompatible"] = allow_skip

        return [event]

    @staticmethod
    def _normalize_event_type(raw_name: str) -> str | None:
        normalized = raw_name.strip().lower()
        normalized = normalized.replace("-", "_").replace(" ", "_")
        normalized = normalized.split("_", 1)[0] if normalized.startswith("route") else normalized
        aliases = {
            "side_cut_in": "side_cut_in",
            "highway_cut_in": "side_cut_in",
            "cut_in": "side_cut_in",
            "parking_cut_in": "parking_cut_in",
            "vehicle_opens_door": "vehicle_opens_door",
            "vehicle_opens_door_two_ways": "vehicle_opens_door",
            "construction_obstacle": "construction_obstacle",
            "mixed_long_tail": "mixed_long_tail",
        }
        if normalized in aliases:
            return aliases[normalized]

        if normalized.startswith("highwaycutin") or normalized.startswith("cutin"):
            return "side_cut_in"
        if normalized.startswith("parkingcutin"):
            return "parking_cut_in"
        if normalized.startswith("vehicleopensdoor"):
            return "vehicle_opens_door"
        if normalized.startswith("constructionobstacle"):
            return "construction_obstacle"
        return None

    def _fallback(self, env: Any, exc: Exception) -> ScenarioSpec:
        if self.fallback_provider is None:
            raise RuntimeError(f"Scenic sampling failed and no fallback provider is set: {exc}") from exc

        fallback = self.fallback_provider.sample(env)
        metadata = dict(fallback.metadata)
        metadata["backend"] = "scenic_fallback"
        metadata["scenic_error"] = str(exc)
        metadata["scenic_file"] = str(self.scenic_file) if self.scenic_file else None

        return ScenarioSpec(
            town=fallback.town,
            weather=fallback.weather,
            ego_spawn_point=fallback.ego_spawn_point,
            destination_transform=fallback.destination_transform,
            traffic_light_policy=fallback.traffic_light_policy,
            number_of_vehicles=fallback.number_of_vehicles,
            number_of_walkers=fallback.number_of_walkers,
            min_route_distance=fallback.min_route_distance,
            pedestrian_cross_factor=fallback.pedestrian_cross_factor,
            walker_speed_min=fallback.walker_speed_min,
            walker_speed_max=fallback.walker_speed_max,
            forced_crossing_walkers=fallback.forced_crossing_walkers,
            forced_crossing_distance_min=fallback.forced_crossing_distance_min,
            forced_crossing_distance_max=fallback.forced_crossing_distance_max,
            forced_crossing_lateral_min=fallback.forced_crossing_lateral_min,
            forced_crossing_lateral_max=fallback.forced_crossing_lateral_max,
            forced_crossing_trigger_prob=fallback.forced_crossing_trigger_prob,
            forced_crossing_cooldown_steps=fallback.forced_crossing_cooldown_steps,
            forced_crossing_warmup_steps=fallback.forced_crossing_warmup_steps,
            forced_crossing_min_speed=fallback.forced_crossing_min_speed,
            forced_crossing_max_total=fallback.forced_crossing_max_total,
            lead_brake_enable_prob=fallback.lead_brake_enable_prob,
            lead_brake_warmup_steps=fallback.lead_brake_warmup_steps,
            lead_candidate_distance_min=fallback.lead_candidate_distance_min,
            lead_candidate_distance_max=fallback.lead_candidate_distance_max,
            lead_candidate_speed_min=fallback.lead_candidate_speed_min,
            lead_candidate_speed_max=fallback.lead_candidate_speed_max,
            lead_track_min_steps=fallback.lead_track_min_steps,
            lead_trigger_distance_min=fallback.lead_trigger_distance_min,
            lead_trigger_distance_max=fallback.lead_trigger_distance_max,
            lead_brake_strength=fallback.lead_brake_strength,
            lead_brake_hold_steps=fallback.lead_brake_hold_steps,
            lead_brake_min_ego_speed=fallback.lead_brake_min_ego_speed,
            lead_event_max_per_episode=fallback.lead_event_max_per_episode,
            hard_break_route_enabled=fallback.hard_break_route_enabled,
            hard_break_lead_spawn_distance_min=fallback.hard_break_lead_spawn_distance_min,
            hard_break_lead_spawn_distance_max=fallback.hard_break_lead_spawn_distance_max,
            long_tail_events=fallback.long_tail_events,
            metadata=metadata,
        )

    @staticmethod
    def _pick(values: dict[str, Any], *keys: str) -> Any:
        for key in keys:
            if key in values:
                value = values[key]
                if value is not None:
                    return value
        return None

    @staticmethod
    def _to_int(value: Any) -> int | None:
        try:
            if value is None:
                return None
            return int(round(float(value)))
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _to_float(value: Any) -> float | None:
        try:
            if value is None:
                return None
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _to_bool(value: Any) -> bool | None:
        if value is None:
            return None
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"1", "true", "yes", "on"}:
                return True
            if normalized in {"0", "false", "no", "off"}:
                return False
        return None

    def _pick_transform(
        self,
        spawn_points: list[carla.Transform],
        idx_value: Any,
        transform_value: Any,
    ) -> carla.Transform | None:
        idx = self._to_int(idx_value)
        if idx is not None and spawn_points:
            return spawn_points[idx % len(spawn_points)]

        return self._parse_transform(transform_value)

    def _parse_transform(self, value: Any) -> carla.Transform | None:
        if value is None:
            return None
        if isinstance(value, carla.Transform):
            return value
        if isinstance(value, dict):
            return self._transform_from_dict(value)
        if isinstance(value, (list, tuple)):
            return self._transform_from_sequence(value)
        return None

    def _transform_from_components(self, values: dict[str, Any], prefix: str) -> carla.Transform | None:
        x = self._to_float(values.get(f"{prefix}_x"))
        y = self._to_float(values.get(f"{prefix}_y"))
        if x is None or y is None:
            return None

        z = self._to_float(values.get(f"{prefix}_z"))
        yaw = self._to_float(values.get(f"{prefix}_yaw"))
        pitch = self._to_float(values.get(f"{prefix}_pitch"))
        roll = self._to_float(values.get(f"{prefix}_roll"))
        heading = self._to_float(values.get(f"{prefix}_heading"))

        if yaw is None and heading is not None:
            yaw = heading
        location = carla.Location(x=x, y=y, z=(z or 0.0))
        rotation = carla.Rotation(
            pitch=(pitch or 0.0),
            yaw=(yaw or 0.0),
            roll=(roll or 0.0),
        )
        return carla.Transform(location, rotation)

    def _transform_from_dict(self, value: dict[str, Any]) -> carla.Transform | None:
        x = self._to_float(value.get("x"))
        y = self._to_float(value.get("y"))
        if x is None or y is None:
            return None

        z = self._to_float(value.get("z"))
        yaw = self._to_float(value.get("yaw"))
        pitch = self._to_float(value.get("pitch"))
        roll = self._to_float(value.get("roll"))
        heading = self._to_float(value.get("heading"))

        if yaw is None and heading is not None:
            yaw = heading
        location = carla.Location(x=x, y=y, z=(z or 0.0))
        rotation = carla.Rotation(
            pitch=(pitch or 0.0),
            yaw=(yaw or 0.0),
            roll=(roll or 0.0),
        )
        return carla.Transform(location, rotation)

    def _transform_from_sequence(self, value: list[Any] | tuple[Any, ...]) -> carla.Transform | None:
        if len(value) < 2:
            return None

        x = self._to_float(value[0])
        y = self._to_float(value[1])
        z = self._to_float(value[2]) if len(value) >= 3 else 0.0
        yaw = self._to_float(value[3]) if len(value) >= 4 else 0.0
        pitch = self._to_float(value[4]) if len(value) >= 5 else 0.0
        roll = self._to_float(value[5]) if len(value) >= 6 else 0.0
        if x is None or y is None:
            return None
        location = carla.Location(x=x, y=y, z=(z or 0.0))
        rotation = carla.Rotation(
            pitch=(pitch or 0.0),
            yaw=(yaw or 0.0),
            roll=(roll or 0.0),
        )
        return carla.Transform(location, rotation)

    def _parse_weather(self, value: Any) -> carla.WeatherParameters | None:
        if value is None:
            return None
        if isinstance(value, carla.WeatherParameters):
            return value
        if isinstance(value, str):
            normalized = value.strip().split(".")[-1]
            if hasattr(carla.WeatherParameters, normalized):
                return getattr(carla.WeatherParameters, normalized)
            for attr_name in dir(carla.WeatherParameters):
                if attr_name.lower() == normalized.lower():
                    attr_value = getattr(carla.WeatherParameters, attr_name)
                    if isinstance(attr_value, carla.WeatherParameters):
                        return attr_value
            return None
        if isinstance(value, dict):
            weather = carla.WeatherParameters()
            for key, raw in value.items():
                if hasattr(weather, key):
                    numeric = self._to_float(raw)
                    if numeric is not None:
                        setattr(weather, key, numeric)
            return weather
        return None


def build_scenario_provider(backend: str | None, params: dict[str, Any]) -> ScenarioProvider:
    backend_name = (backend or "random").lower()
    min_route_distance = params.get("min_route_distance", 50.0)

    if backend_name == "random":
        return RandomScenarioProvider(min_route_distance=min_route_distance)

    if backend_name == "scenic":
        scenic_params = params.get("scenic_params", {})
        if not isinstance(scenic_params, dict):
            scenic_params = {}
        fallback = RandomScenarioProvider(min_route_distance=min_route_distance)
        return ScenicScenarioProvider(
            scenic_file=params.get("scenic_file"),
            scenic_params=scenic_params,
            scenic_model=params.get("scenic_model"),
            scenic_scenario=params.get("scenic_scenario"),
            scenic_mode2d=bool(params.get("scenic_mode2d", False)),
            scenic_max_iterations=int(params.get("scenic_max_iterations", 2000)),
            fallback_provider=fallback,
        )

    raise ValueError(
        f"Unsupported scenario backend '{backend_name}'. "
        "Available backends: 'random', 'scenic'."
    )
