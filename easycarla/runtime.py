"""Request-driven episode orchestration. No learning code and no transport code."""
from copy import deepcopy
import math
from numbers import Integral
import random
import time
import uuid
import numpy as np
from .config import validate_config
from .control import TrajectoryTracker, validate_action
from .errors import EnvironmentFault, RequestRejected
from .extensions.session import ExperimentSession
from .scenarios.contracts import validate_episode
from .observations import ObservationBuilder
from .observations.geometry import xyz
from .simulation import WorldSession
from .simulation.sensors import SensorSuite
from .simulation.traffic import TrafficRules
from .scenarios.selection import select_source
from .tasks import DrivingTask


class EnvironmentRuntime:
    def __init__(self, config):
        self.config = validate_config(config)
        self.session = None
        self.episode = None
        self.sensors = None
        self.observer = None
        self.state = "idle"
        self.last_obs = None
        self.snapshot = None
        self.elapsed = 0.0
        self.physical_ticks = 0
        self.decision_steps = 0
        self.episode_id = None
        self.seed = None
        self.experiments = ExperimentSession(self)
        self.tracker = TrajectoryTracker()
        self.arbiter = None
        self.task = DrivingTask(self.config["task"])

    def reset(self, seed=None, options=None, *, _source_config=None):
        if options is not None and not isinstance(options, dict):
            raise RequestRejected("reset options must be a mapping")
        if seed is not None and (isinstance(seed, bool) or not isinstance(seed, Integral)):
            raise RequestRejected("reset seed must be an integer or None")
        options = deepcopy(options or {})
        seed = int(seed if seed is not None else random.SystemRandom().randrange(2**31))
        try:
            adapter, source_config = select_source(_source_config if _source_config is not None else self.config["scenario"], seed, options)
            if self.config["task"]["profile"] == "bench2drive_native" and source_config["source"] != "bench2drive":
                raise RequestRejected("bench2drive_native task requires the Bench2Drive source")
            town = adapter.town(seed, options)
            affinity = self.config["simulation"]["map_affinity"]
            if affinity is not None and town.rsplit("/", 1)[-1] != affinity:
                raise RequestRejected(f"Selected map {town!r} violates worker map_affinity {affinity!r}; configure sources explicitly")
        except RequestRejected:
            raise
        except (ValueError, TypeError, KeyError) as exc:
            raise RequestRejected(str(exc)) from exc
        self.close_episode()
        self.experiments.on_episode_closed()
        self.state = "initializing"
        self.episode_id = str(uuid.uuid4())
        self.seed = seed
        self.source_config = source_config
        start = time.perf_counter()
        try:
            random.seed(seed)
            np.random.seed(seed % 2**32)
            if self.session is None:
                self.session = WorldSession(self.config)
            self.session.load(town, seed)
            try:
                self.episode = adapter.prepare(self.session, seed, options)
            except Exception as first_error:
                fallback = source_config.get("fallback")
                if fallback is None or options.get("recipe"):
                    raise
                errors = self.session.registry.clear()
                if errors:
                    raise EnvironmentFault(f"Fallback cleanup failed: {errors}")
                second, source_config = select_source(fallback, seed, {})
                if self.config["task"]["profile"] == "bench2drive_native" and source_config["source"] != "bench2drive":
                    raise ValueError("bench2drive_native task cannot fall back to a non-Bench2Drive source")
                fallback_town = second.town(seed, {})
                if affinity is not None and fallback_town.rsplit("/", 1)[-1] != affinity:
                    raise RequestRejected("Explicit fallback violates worker map_affinity")
                self.session.load(fallback_town, seed)
                self.episode = second.prepare(self.session, seed, {})
                self.episode.metadata["fallback"] = {"requested_source": self.source_config["source"], "reason": str(first_error)}
                self.source_config = source_config
            self.elapsed, self.physical_ticks, self.decision_steps = 0.0, 0, 0
            self.tracker = TrajectoryTracker()
            validate_episode(self.episode, self.config["task"]["profile"])
            self.arbiter = self.session.controls.arbiter
            self.session.controls.handoff(self.episode.ego, "client")
            for role, (actor, _) in self.episode.controllers.items():
                self.session.controls.handoff(actor, role)
            self.sensors = SensorSuite(self.session, self.episode.ego, self.config["observations"], self.episode_id)
            self.observer = ObservationBuilder(self.session, self.episode, self.config)
            self.session.controls.apply_control(self.episode.ego, [0, 0, 1], owner="client")
            self.snapshot = self.session.tick("initialization")
            self.episode.after_tick(self.snapshot)
            sensor_data = self.sensors.collect(self.snapshot.frame)
            self.observer.sample(self.snapshot, 0)
            self.last_obs = self.observer.build(self.snapshot, sensor_data)
            self.task = DrivingTask(self.config["task"])
            self.task.reset(self.last_obs)
            self.traffic_rules = TrafficRules(self.session, self.episode.ego)
            self.traffic_rules.update(self.observer.frame_state)
            self.state = "ready"
            if self.config["experiments"]["enabled"]:
                self.experiments.on_episode_ready()
            info = self._info()
            info["timing"]["initialization_seconds"] = time.perf_counter() - start
            self.sensors.commit(self.snapshot.frame)
            return self.last_obs, info
        except Exception as exc:
            self.state = "faulted"
            cleanup_details = ""
            failed_scenario = deepcopy(self.episode.metadata) if self.episode is not None else None
            try:
                self.close_episode()
            except Exception as cleanup_error:
                cleanup_details = f"; cleanup also failed: {cleanup_error}"
            self.state = "faulted"
            error = EnvironmentFault(f"Reset failed ({self.source_config.get('source')}): {exc}{cleanup_details}", exc)
            error.details.update(phase="reset", seed=self.seed, source=self.source_config.get("source"),
                                 scenario=failed_scenario, failure_reason=str(exc))
            raise error from exc

    def require_ready(self):
        self._ready()

    def episode_info(self):
        return self._info()

    def mark_faulted(self):
        self.state = "faulted"

    def rebuild_episode(self, recipe):
        return self.reset(seed=recipe["seed"], options={"recipe": recipe["metadata"]},
                          _source_config=recipe["source_config"])

    def attach_controller(self, role, lease, controller):
        actor = lease["actor"]
        if role in self.episode.controllers or actor.id not in self.session.registry.actors:
            raise EnvironmentFault("Controller attachment requires an owned actor and an unused role")
        if self.session.controls.owner(actor.id) != role:
            raise EnvironmentFault("Scene adapter must explicitly transfer control before attachment")
        self.episode.controllers[role] = (actor, controller)

    def _ready(self):
        if self.sensors is not None and self.state in ("ready", "done"):
            self._sensors_healthy()
        if self.state != "ready":
            raise RequestRejected(f"Episode is {self.state}; reset is required")

    def _sensors_healthy(self):
        try:
            self.sensors.assert_healthy()
        except Exception as exc:
            self.state = "faulted"
            raise EnvironmentFault(f"Sensor stream failed: {exc}", exc) from exc

    def step(self, action):
        self._ready()
        self._sensors_healthy()
        action = validate_action(action)
        command = {"method": "step", "action": action.tolist()}
        return self._advance(action, self.config["simulation"]["decision_dt"], command)

    def step_trajectory(self, trajectory, execute_seconds=None):
        self._ready()
        self._sensors_healthy()
        duration = self.config["simulation"]["decision_dt"] if execute_seconds is None else float(execute_seconds)
        ratio = duration / self.config["simulation"]["physics_dt"]
        if not math.isfinite(duration) or duration <= 0 or abs(round(ratio) - ratio) > 1e-8:
            raise ValueError("Trajectory execution must be a positive integer number of physical ticks")
        if self.experiments.recorder is not None:
            self.experiments.recorder.ensure_capacity()
        self.tracker.start(trajectory, self.snapshot.frame, self.observer.get_state(self.episode.ego, frame=self.snapshot.frame), duration)
        command = {"method": "step_trajectory", "trajectory": deepcopy(trajectory), "execute_seconds": duration}
        return self._advance(None, duration, command)

    def _advance(self, action, duration, command):
        if self.experiments.recorder is not None:
            self.experiments.recorder.ensure_capacity()
        sim = self.config["simulation"]
        dt = sim["physics_dt"]
        count = round(duration / dt)
        total_reward, terms, costs, controls = 0.0, {}, {}, []
        prefix_controls = []
        sensor_wait, started = 0.0, time.perf_counter()
        sim_before = self.session.timings["simulation_seconds"]
        commit_before = self.session.timings["control_commit_seconds"]
        observation_seconds = history_seconds = scenario_seconds = 0.0
        frame_capture_seconds = history_sampling_seconds = 0.0
        deferred = self.config["performance"]["deferred_observations"]
        start_frame = self.snapshot.frame
        try:
            terminated = truncated = False
            for _ in range(count):
                self.sensors.assert_healthy()
                if not self.episode.ego.is_alive:
                    raise EnvironmentFault("Ego actor disappeared")
                scenario_start = time.perf_counter()
                self.episode.before_tick()
                requested = action if action is not None else self.tracker.control(self.observer.get_state(self.episode.ego, frame=self.snapshot.frame), dt)
                resolved = self.episode.resolve_control(requested)
                applied = self.session.controls.apply_control(self.episode.ego, resolved.values,
                                                               owner="client", allow_combined=resolved.allow_combined)
                npc_controls = self._npc_controls()
                scenario_seconds += time.perf_counter() - scenario_start
                self.snapshot = self.session.tick("replay" if self.experiments.replaying else "step")
                self.physical_ticks += 1
                self.elapsed += dt
                scenario_start = time.perf_counter()
                self.episode.after_tick(self.snapshot)
                scenario_seconds += time.perf_counter() - scenario_start
                before_wait = time.perf_counter()
                sensor_data = self.sensors.collect(self.snapshot.frame)
                sensor_wait += time.perf_counter() - before_wait
                if not self.episode.ego.is_alive:
                    raise EnvironmentFault("Ego actor disappeared after physical tick")
                observation_start = time.perf_counter()
                self.observer.sample(self.snapshot, self.physical_ticks)
                history_seconds += time.perf_counter() - observation_start
                sample_timing = getattr(self.observer, "last_sample_timings", {})
                frame_capture_seconds += sample_timing.get("frame_capture_seconds", 0.0)
                history_sampling_seconds += sample_timing.get("history_sampling_seconds", 0.0)
                observation_start = time.perf_counter()
                task_obs = self.observer.task_state() if deferred else self.observer.build(self.snapshot, sensor_data)
                observation_seconds += time.perf_counter() - observation_start
                facts = self._facts()
                reward, terminated, truncated, task_info = self.task.evaluate(task_obs, facts, dt, self.elapsed)
                total_reward += reward
                for key, value in task_info["reward_components"].items():
                    terms[key] = terms.get(key, 0.0) + value
                for key, value in task_info["cost_components"].items():
                    costs[key] = costs.get(key, 0.0) + value
                controls.append({"frame": self.snapshot.frame, "requested": requested.tolist(), "applied": applied.tolist(),
                                 "position": self.observer.get_state(self.episode.ego, frame=self.snapshot.frame)["position"]})
                if self.experiments.recorder is not None:
                    prefix_controls.append({"tick": self.physical_ticks,
                                            "requested": requested.tolist(), "applied": applied.tolist(),
                                            "managed_npcs": npc_controls})
                if terminated or truncated:
                    self.state = "done"
                    break
            observation_start = time.perf_counter()
            self.last_obs = self.observer.build(self.snapshot, sensor_data) if deferred else task_obs
            observation_seconds += time.perf_counter() - observation_start
            self.decision_steps += 1
            info = self._info()
            info.update(task_info)
            info.update(reward_components=terms, cost_components=costs, cost=sum(costs.values()),
                        executed_seconds=len(controls)*dt, physical_ticks=len(controls), actual_controls=controls,
                        start_frame=start_frame, final_frame=self.snapshot.frame)
            info["timing"].update(simulation_seconds=self.session.timings["simulation_seconds"]-sim_before,
                                  control_commit_seconds=self.session.timings["control_commit_seconds"]-commit_before,
                                  observation_seconds=observation_seconds,
                                  history_seconds=history_seconds, scenario_seconds=scenario_seconds,
                                  frame_capture_seconds=frame_capture_seconds,
                                  history_sampling_seconds=history_sampling_seconds,
                                  sensor_wait_seconds=sensor_wait, environment_seconds=time.perf_counter()-started)
            self.sensors.commit(self.snapshot.frame)
            if self.experiments.recorder is not None:
                recorded_command = {**command, "executed_controls": prefix_controls,
                                    "executed_seconds": len(controls)*dt}
                self.experiments.recorder.append(recorded_command, self.raw_state())
            return self.last_obs, float(total_reward), terminated, truncated, info
        except Exception as exc:
            self.state = "faulted"
            error = EnvironmentFault(f"Step failed after submission; no valid transition: {exc}", exc)
            error.details.update(phase="step", start_frame=start_frame,
                                 last_confirmed_frame=self.snapshot.frame,
                                 command_may_have_executed=True, failure_reason=str(exc))
            raise error from exc

    def _npc_controls(self):
        managed = self.episode.controllers
        if not managed:
            return {}
        applied = {}
        neighbors = []
        for actor_id, captured in self.observer.frame_state.actors.items():
            state = dict(captured)
            state["id"] = self.session.registry.roles.get(actor_id, str(actor_id))
            state["dimensions"] = state["size"]
            state["carla_id"] = actor_id
            neighbors.append(state)
        for role, (actor, controller) in managed.items():
            if not actor.is_alive:
                raise EnvironmentFault(f"Controlled NPC {role} disappeared")
            state = dict(self.observer.get_state(actor, frame=self.snapshot.frame))
            state["dimensions"] = state["size"]
            control = controller.control(state, [n for n in neighbors if n["carla_id"] != actor.id],
                                         self.config["simulation"]["physics_dt"], self.physical_ticks)
            control = self.session.controls.apply_control(actor, control, owner=role)
            applied[role] = control.tolist()
        return applied

    def _facts(self):
        c = self.session.carla
        state = self.observer.get_state(self.episode.ego, frame=self.snapshot.frame)
        x, y, z = state["position"]
        location = c.Location(x=float(x), y=-float(y), z=float(z))
        wp = self.session.map.get_waypoint(location, project_to_road=False, lane_type=c.LaneType.Driving | c.LaneType.Parking | c.LaneType.Shoulder)
        collision = any(frame <= self.snapshot.frame for frame, _ in self.sensors.collisions)
        distance = location.distance(self.episode.route[-1].location)
        goal = distance <= self.config["task"]["goal_radius"] and self.observer.route_index >= len(self.episode.route) - 5
        native = self.episode.scenario_facts()
        violations = self.traffic_rules.update(self.observer.frame_state)
        return {"collision": collision, "offroad": wp is None, "goal": goal,
                "signal": bool(violations), "signal_id": "signal:" + ",".join(violations),
                "native_done": native.terminated, "native_timeout": native.truncated}

    def _info(self):
        return {"backend": "carla", "episode_id": self.episode_id, "scenario": deepcopy(self.episode.metadata),
                "seed": self.seed, "task_profile": self.config["task"]["profile"],
                "effective_world_settings": deepcopy(self.session.effective_world_settings),
                "applied_map_override": deepcopy(getattr(self.session, "applied_map_override", {})),
                "simulation_config": {key: deepcopy(self.config["simulation"][key]) for key in
                    ("physics_dt", "decision_dt", "history_dt", "no_rendering_mode", "map_layers",
                     "tile_stream_distance", "actor_active_distance", "spectator_as_ego", "map_overrides")},
                "observation_profile": self.config["observations"]["profile"],
                "performance_config": deepcopy(self.config["performance"]),
                "map_affinity": self.config["simulation"]["map_affinity"],
                "decision_steps": self.decision_steps, "episode_seconds": self.elapsed,
                "events": self.episode.info(), "tick_counts": dict(self.session.ticks), "timing": {}}

    def get_static(self, map_id=None):
        if self.observer is None:
            raise RequestRejected("reset required before querying map")
        if map_id is not None and map_id != self.observer.static["map_id"]:
            raise ValueError("map_id does not belong to current episode")
        return self.observer.static

    def capabilities(self):
        from .observations.spec import observation_spec
        return {"protocol": 1, "actions": ["vehicle_control", "trajectory"],
                "observation_spec": observation_spec(self.config),
                "observation_profile": self.config["observations"]["profile"],
                "clocks": {k: self.config["simulation"][k] for k in ("physics_dt", "decision_dt", "history_dt")},
                "trajectory_spec": dict(self.config["trajectory"]),
                "experiments_enabled": self.config["experiments"]["enabled"],
                "scenario": self.episode.capabilities() if self.episode else None}

    def get_route(self, route_id=None):
        if self.observer is None or self.observer.route_description is None:
            raise RequestRejected("planning_v1 reset required before querying route")
        route = self.observer.route_description
        if route_id is not None and route_id != route["route_id"]:
            raise ValueError("route_id does not belong to current episode")
        return route

    def raw_state(self):
        return self.experiments.raw_state()

    def experiment(self, method, **kwargs):
        if not self.config["experiments"]["enabled"]:
            raise RequestRejected("Experiment extension is disabled")
        try:
            return self.experiments.dispatch(method, **kwargs)
        except Exception as exc:
            if self.state == "faulted" and not isinstance(exc, EnvironmentFault):
                raise EnvironmentFault(f"Experiment operation failed after mutation: {exc}", exc) from exc
            raise

    def close_episode(self):
        errors = []
        sensor_error = None
        was_faulted = self.state == "faulted"
        if self.sensors is not None:
            if self.state in ("ready", "done"):
                try:
                    self.sensors.assert_healthy()
                except Exception as exc:
                    sensor_error = exc
            try:
                self.sensors.close()
            except Exception as exc:
                errors.append(str(exc))
            self.sensors = None
        if self.episode is not None:
            try:
                self.episode.close()
            except Exception as exc:
                errors.append(str(exc))
            self.episode = None
        if self.session is not None:
            errors.extend(self.session.registry.clear())
        self.observer = None
        self.experiments.on_episode_closed(preserve_recording=was_faulted or sensor_error is not None)
        self.state = "faulted" if errors or sensor_error is not None else "idle"
        if sensor_error is not None:
            raise EnvironmentFault(f"Late sensor failure discovered during episode cleanup: {sensor_error}", sensor_error)
        if errors:
            raise EnvironmentFault(f"Episode cleanup failed: {errors}")

    def shutdown(self):
        try:
            self.close_episode()
        finally:
            try:
                if self.session is not None:
                    try:
                        self.session.close()
                    finally:
                        self.session = None
            finally:
                self.experiments.close()

    def close_client_session(self):
        """Release episode and client-owned experiment materials, preserving the service."""
        try:
            self.close_episode()
        finally:
            self.experiments.close()
