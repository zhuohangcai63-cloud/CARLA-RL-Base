"""Scenic parameter sampling and executable episodes, independent of Gym/IPC."""
from copy import deepcopy
import hashlib
from pathlib import Path
import random
import numpy as np
from .context import EventContext, OwnedWorld
from .contracts import ResolvedControl, ScenarioFacts


def sample_parameters(path, seed, overrides, max_iterations=2000):
    import scenic
    saved_python, saved_numpy = random.getstate(), np.random.get_state()
    try:
        random.seed(seed)
        np.random.seed(seed % 2**32)
        compiled = scenic.scenarioFromFile(str(path), params=overrides)
        scene, iterations = compiled.generate(maxIterations=max_iterations)
        values = {str(k): v.item() if isinstance(v, np.generic) else v for k, v in scene.params.items()}
        return values, iterations
    finally:
        random.setstate(saved_python)
        np.random.set_state(saved_numpy)


class ScenicAdapter:
    def __init__(self, config):
        self.config = config

    def town(self, seed, options):
        return options.get("recipe", {}).get("town", self.config.get("town", "Town10HD"))

    def prepare(self, session, seed, options):
        episode = ScenicEpisode.__new__(ScenicEpisode)
        try:
            episode.__init__(session, self.config, seed, options)
            return episode
        except Exception as original:
            try:
                episode.close()
            except Exception as cleanup:
                raise RuntimeError(f"Scenic preparation failed: {original}; cleanup also failed: {cleanup}") from original
            raise


class ScenicEpisode:
    task_profiles = frozenset({"driving"})

    def __init__(self, session, config, seed, options):
        from agents.navigation.global_route_planner import GlobalRoutePlanner
        from .events import LongTailEventManager
        from .events.crossing import CrossingEvent
        from .events.lead_brake import LeadBrakeEvent
        self.session, self.config, self.seed = session, config, seed
        self.rng = random.Random(seed)
        # Route rejection sampling must not shift subsequent actor sampling.
        # Recipes skip rejected routes, while this spawn stream starts identically.
        route_rng = random.Random(seed)
        self.owned_world = OwnedWorld(session)
        self._lights = []
        self._tick = 0
        self.controllers = {}
        recipe = options.get("recipe")
        path = Path(config["file"]).expanduser().resolve()
        params, iterations = (deepcopy(recipe["parameters"]), 0) if recipe else sample_parameters(
            path, seed, config.get("params", {}), config.get("max_iterations", 2000))
        self.parameters = params
        town = session.map.name.rsplit("/", 1)[-1]
        self.metadata = {"source": "scenic", "scenario_id": str(params.get("scenario_name", path.stem)),
                         "seed": int(seed), "source_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                         "file": str(path), "town": town, "iterations": iterations,
                         "parameters": deepcopy(params), "generation_failures": []}
        self.metadata["seeds"] = {"scenic_python": seed, "scenic_numpy": seed % 2**32,
                                  "route": seed, "spawn": seed, "streams_independent": True,
                                  "traffic_manager": seed % 2**31,
                                  "pedestrians": seed % 2**31, "events": seed + 1,
                                  "crossing": seed + 2, "lead_brake": seed + 3,
                                  "reactive_npc": seed}
        c = session.carla
        weather = params.get("weather", "ClearNoon")
        if not hasattr(c.WeatherParameters, weather):
            raise ValueError(f"Unknown weather preset {weather}")
        session.world.set_weather(getattr(c.WeatherParameters, weather))
        self._set_lights(params.get("traffic_light_policy", "on"))
        spawn_points = session.map.get_spawn_points()
        if not spawn_points:
            raise RuntimeError("No vehicle spawn points")
        planner = GlobalRoutePlanner(session.map, 2.0)
        start_idx = int(params.get("ego_spawn_index", route_rng.randrange(len(spawn_points)))) % len(spawn_points)
        end_idx = int(params.get("destination_spawn_index", route_rng.randrange(len(spawn_points)))) % len(spawn_points)
        # Route resampling stays within this source and every attempt is recorded.
        attempts = 1 if recipe else int(config.get("route_attempts", 30))
        for attempt in range(attempts):
            if attempt:
                start_idx, end_idx = route_rng.sample(range(len(spawn_points)), 2)
            route = planner.trace_route(spawn_points[start_idx].location, spawn_points[end_idx].location)
            distance = sum(a[0].transform.location.distance(b[0].transform.location) for a, b in zip(route, route[1:]))
            forward = len(route) > 2 and route[0][0].transform.get_forward_vector().dot(
                route[2][0].transform.location - route[0][0].transform.location) > 0
            if distance >= float(params.get("min_route_distance", 50.0)) and forward:
                break
            self.metadata["generation_failures"].append({"attempt": attempt, "reason": "route_not_forward_or_too_short", "start": start_idx, "end": end_idx})
        else:
            raise RuntimeError(f"Scenic route generation failed: {self.metadata['generation_failures']}")
        self.route = [wp.transform for wp, _ in route]
        self.metadata.update(ego_spawn_index=start_idx, destination_spawn_index=end_idx)
        self.parameters.update(ego_spawn_index=start_idx, destination_spawn_index=end_idx)
        self.metadata["parameters"] = deepcopy(self.parameters)
        bp = session.world.get_blueprint_library().find(config.get("ego_blueprint", "vehicle.tesla.model3"))
        # CARLA's large-map manager recognizes hero/ego_vehicle, not "ego".
        # Preserve the existing semantic registry identity independently.
        bp.set_attribute("role_name", "hero")
        self.ego = self.owned_world.spawn_actor(bp, spawn_points[start_idx], semantic_role="ego")
        session.tick("initialization")
        self.context = EventContext(session, self.ego, self.route)
        self.vehicles = self._background_vehicles(int(params.get("number_of_vehicles", 0)), spawn_points)
        self._background_walkers(int(params.get("number_of_walkers", 0)))
        session.tick("initialization")
        self.events = LongTailEventManager(self.context, config.get("max_active_events", 1), seed + 1)
        specs = list(params.get("long_tail_events", []))
        event_type = params.get("long_tail_event_type")
        if event_type:
            specs.append(dict(params, type=event_type))
        for spec in specs:
            if "event_timeout_steps" in spec:
                spec["event_timeout_steps"] = round(float(spec["event_timeout_steps"]) * 0.1 / self.context.dt)
        self.events.setup(specs)
        self.crossing = CrossingEvent(self.context, params, seed + 2)
        self.lead = LeadBrakeEvent(self.context, params, seed + 3, self.vehicles)
        self._setup_reactive(config.get("reactive_npcs", []), planner)

    def _set_lights(self, policy):
        c = self.session.carla
        for light in self.session.world.get_actors().filter("traffic.traffic_light*"):
            self._lights.append((light, light.get_state(), light.is_frozen()))
            if policy == "off":
                light.set_state(c.TrafficLightState.Green)
                light.freeze(True)
            elif policy == "on":
                light.freeze(False)
            else:
                raise ValueError(f"Unknown traffic_light_policy {policy}")

    def _background_vehicles(self, count, spawn_points):
        blueprints = [bp for bp in self.session.world.get_blueprint_library().filter("vehicle.*")
                      if bp.has_attribute("number_of_wheels") and int(bp.get_attribute("number_of_wheels")) == 4]
        points = list(spawn_points)
        self.rng.shuffle(points)
        vehicles = []
        for point in points:
            if len(vehicles) >= count:
                break
            if point.location.distance(self.ego.get_location()) < 15:
                continue
            bp = self.rng.choice(blueprints)
            bp.set_attribute("role_name", "background")
            actor = self.owned_world.try_spawn_actor(bp, point)
            if actor:
                self.session.controls.set_autopilot(actor, True, owner="scenic/background")
                vehicles.append(actor)
        self.metadata["background_vehicles_requested"] = count
        self.metadata["background_vehicles_spawned"] = len(vehicles)
        return vehicles

    def _background_walkers(self, count):
        c = self.session.carla
        lib = self.session.world.get_blueprint_library()
        for i in range(count):
            location = self.session.world.get_random_location_from_navigation()
            if location is None:
                continue
            bp = self.rng.choice(list(lib.filter("walker.pedestrian.*")))
            if bp.has_attribute("is_invincible"):
                bp.set_attribute("is_invincible", "false")
            actor = self.owned_world.try_spawn_actor(bp, c.Transform(location))
            if actor:
                self.session.controls.claim(actor.id, "scenic/walker_ai", mode="delegated")
                controller = self.owned_world.spawn_actor(lib.find("controller.ai.walker"), c.Transform(), attach_to=actor)
                self.session.tick("initialization")
                controller.start()
                target = self.session.world.get_random_location_from_navigation()
                if target:
                    controller.go_to_location(target)
                controller.set_max_speed(self.rng.uniform(float(self.parameters.get("walker_speed_min", 1)), float(self.parameters.get("walker_speed_max", 2))))
        self.session.world.set_pedestrians_cross_factor(float(self.parameters.get("pedestrian_cross_factor", 0.2)))

    def _setup_reactive(self, specs, planner=None):
        from easycarla.control import ReactiveNPCController
        from easycarla.observations.geometry import xyz
        from .reactive_routes import build_reactive_route
        c = self.session.carla
        self.metadata["reactive_routes"] = {}
        for i, spec in enumerate(specs):
            spec = {"distance": 25.0 + 15*i, **spec}
            role = spec.get("role", f"reactive_{i}")
            if role in self.controllers:
                raise ValueError(f"Duplicate reactive NPC role {role}")
            fixed_route = build_reactive_route(self.session.map, self.route, spec, c, planner)
            start = fixed_route.transforms[0]
            transform = c.Transform(start.location + c.Location(z=0.35), start.rotation)
            bp = self.session.world.get_blueprint_library().find(spec.get("blueprint", "vehicle.audi.a2"))
            bp.set_attribute("role_name", role)
            actor = self.owned_world.spawn_actor(bp, transform)
            route = [xyz(t.location).tolist() for t in fixed_route.transforms]
            ctrl = ReactiveNPCController(role, route, mechanism=spec.get("mechanism", "C"),
                                         target_speed=spec.get("target_speed", 8.0), seed=self.seed)
            self.controllers[role] = (actor, ctrl)
            self.metadata["reactive_routes"][role] = fixed_route.metadata

    def before_tick(self):
        self.context.time_step = self._tick
        self.events.before_tick()
        self.crossing.before_tick()
        self.lead.before_tick()

    def resolve_control(self, requested):
        return ResolvedControl(requested)

    def scenario_facts(self):
        return ScenarioFacts()

    def takeover(self, role):
        raise ValueError(f"Scenic role {role!r} has no native takeover; configure reactive_npcs at initialization")

    def export_state(self, actor_roles):
        from .events.state import StateExporter
        exporter = StateExporter(actor_roles)
        components = {name: getattr(self, name).export_state(actor_roles)
                      for name in ("events", "crossing", "lead")}
        limitations = set(self.capabilities()["prefix_replay"]["limitations"])
        for component in components.values():
            limitations.update(component.get("limitations", []))
        return {"schema_version": 1, "tick": self._tick, "components": components,
                "rng_state": exporter.value(self.rng.getstate()),
                "limitations": sorted(limitations)}

    def after_tick(self, snapshot):
        self._tick += 1
        self.context.time_step = self._tick
        self.events.after_tick()
        self.crossing.after_tick()
        self.lead.after_tick()

    def info(self):
        return {**self.events.info(), **self.crossing.info(), **self.lead.info()}

    def capabilities(self):
        has_background = bool(self.vehicles or int(self.parameters.get("number_of_walkers", 0)))
        return {"source": "scenic", "rebuild": True,
                "prefix_replay": {"available": True, "verified": False,
                                  "limitations": ["Traffic Manager/walker hidden state is not exportable"] if has_background else []},
                "takeover_roles": {}, "mechanism_roles": list(self.controllers),
                "mechanism_switch": bool(self.controllers)}

    def close(self):
        errors = []
        for name in ("events", "crossing", "lead"):
            component = getattr(self, name, None)
            if component is not None:
                try:
                    component.close()
                except Exception as exc:
                    errors.append(str(exc))
        for light, state, frozen in getattr(self, "_lights", []):
            try:
                if light.is_alive:
                    light.set_state(state)
                    light.freeze(frozen)
            except Exception as exc:
                errors.append(str(exc))
        self._lights = []
        if errors:
            raise RuntimeError(f"Scenic cleanup failed: {errors}")
