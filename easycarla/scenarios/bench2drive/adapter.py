"""Run upstream route/scenario trees under the environment's one-tick owner.

No ScenarioManager, evaluator, worker thread, or background ticking is used.
All upstream compatibility lives here and in the reproducibly patched copy.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import random
import queue
import sys

import numpy as np

from easycarla.scenarios.manifests import load_manifest, select_entry, verify_entry
from .native_control import control_scope, native_actor_call

PINNED_COMMIT = "acbeb0dd8aa99dd0e71b9b75bb542f6fe7c7f3d8"


def _dependencies(root):
    root = Path(root).expanduser().resolve()
    lock = json.loads((root / "easycarla-lock.json").read_text())
    if lock.get("commit") != PINNED_COMMIT or lock.get("patch_version") not in (4, 5, 6):
        raise RuntimeError("Use scripts/prepare_bench2drive.py to prepare the pinned dependency")
    # Validate executable dependency content once on adapter creation.
    for name, expected in lock["files"].items():
        if name.endswith(".py") and hashlib.sha256((root / name).read_bytes()).hexdigest() != expected:
            raise RuntimeError(f"Pinned Bench2Drive code changed: {name}")
    for namespace in ("srunner", "leaderboard"):
        existing = sys.modules.get(namespace)
        if existing is not None:
            paths = list(getattr(existing, "__path__", []))
            if not paths or not all(Path(p).resolve().is_relative_to(root) for p in paths):
                raise RuntimeError(f"{namespace} already imported from a different checkout")
    sys.path[:0] = [str(root / "scenario_runner"), str(root / "leaderboard")]
    os.environ["SCENARIO_RUNNER_ROOT"] = str(root / "scenario_runner")
    return root, lock


class WorldBridge:
    """Record ownership and route hidden upstream tick requests to one manager."""
    def __init__(self, episode):
        self.episode = episode

    def __getattr__(self, name):
        return getattr(self.episode.session.world, name)

    def tick(self, *args, **kwargs):
        return self._advance().frame

    def wait_for_tick(self, *args, **kwargs):
        return self._advance()

    def _advance(self):
        episode = self.episode
        if episode.initializing:
            snapshot = episode.session.tick(phase="initialization")
            if episode.provider is not None:
                episode.provider.on_carla_tick()
            return snapshot
        # Actor spawning is synchronous RPC. The enclosing decision tick will
        # advance physics. Do not wait for a second client or invent extra ticks.
        episode.deferred_tick_requests += 1
        return episode.session.world.get_snapshot()

    def _register(self, actor):
        if actor is not None:
            episode = self.episode
            episode.owned_ids.add(actor.id)
            # CARLA role_name is shared by many background actors. A creation
            # ordinal gives each actor a unique semantic ID that survives a
            # deterministic rebuild, without using transient CARLA actor IDs.
            if actor.id not in episode.semantic_roles:
                ordinal = episode.spawn_count
                episode.spawn_count += 1
                episode.semantic_roles[actor.id] = (
                    f"bench2drive:{ordinal:06d}:{actor.type_id}:{actor.attributes.get('role_name', '')}")
                episode.session.controls.claim(actor.id, "bench2drive/native", mode="delegated")
            episode.session.registry.register(actor, episode.semantic_roles[actor.id])
            if getattr(episode, "metadata", {}).get("patch_version", 6) < 6:
                # p4/p5 cannot witness native physics-disable calls. Keep their
                # legacy execution usable without guessing that physics is on.
                episode.session.registry.physics_enabled[actor.id] = None
        return actor

    def spawn_actor(self, *args, **kwargs):
        return self._register(self.episode.session.world.spawn_actor(*args, **kwargs))

    def forget(self, actor_id):
        # Native destruction has already completed synchronously. CARLA actor
        # is_alive can remain stale until the next physics snapshot; avoid a
        # second destructive RPC during cleanup without adding a cleanup tick.
        self.episode.owned_ids.discard(actor_id)
        self.episode.session.registry.forget(actor_id)
        self.episode.state.get("autopilot", set()).discard(actor_id)
        self.episode.state.get("resume_autopilot", {}).pop(actor_id, None)
        for role, leased_id in list(self.episode._lease_roles.items()):
            if leased_id == actor_id:
                self.episode.controllers.pop(role, None)
                self.episode._lease_roles.pop(role)
                self.episode.state["leased"].discard(actor_id)
                self.episode.metadata["mechanism_replacements"].append({
                    "role": role, "actor_id": actor_id, "event": "native_lifetime_ended",
                    "physical_frame": self.episode.session.world.get_snapshot().frame})

    def try_spawn_actor(self, *args, **kwargs):
        return self._register(self.episode.session.world.try_spawn_actor(*args, **kwargs))


class ClientBridge:
    def __init__(self, episode):
        self.episode = episode

    def __getattr__(self, name):
        return getattr(self.episode.session.client, name)

    def get_world(self):
        return self.episode.world

    def apply_batch_sync(self, batch, do_tick=False):
        # Never pass tick=True to CARLA. The upstream caller's explicit tick (or
        # the enclosing runtime tick) owns the advance, without a double step.
        batch = list(batch)
        # Spawn chains are opaque in the CARLA API; the complete new actor is
        # registered as delegated native ownership below. Existing actor control
        # batches would bypass that ownership boundary, so reject them before
        # issuing any RPC. The pinned provider uses spawn and destroy batches.
        allowed = {"SpawnActor", "DestroyActor"}
        unsupported = sorted({type(command).__name__ for command in batch} - allowed)
        if unsupported:
            raise RuntimeError(f"Unsupported native batch commands: {unsupported}")
        responses = self.episode.session.client.apply_batch_sync(batch, False)
        for command, result in zip(batch, responses):
            physics_enabled = self.episode.state.get("spawn_physics", {}).pop(id(command), None)
            if not result.error:
                if type(command).__name__ == "DestroyActor":
                    self.episode.world.forget(command.actor_id)
                    continue
                actor = self.episode.session.world.get_actor(result.actor_id)
                if actor is not None:
                    self.episode.world._register(actor)
                    if physics_enabled is not None:
                        self.episode.session.registry.physics_enabled[actor.id] = physics_enabled
        if do_tick:
            self.episode.deferred_batch_ticks += 1
        return responses

    def apply_batch(self, batch):
        # Native route destructors submit only destruction batches. Waiting for
        # acknowledgement lets ownership be retired accurately without ticking.
        return self.apply_batch_sync(batch, False)


class Bench2DriveAdapter:
    def __init__(self, config):
        self.config = config.get("bench2drive", config)
        if not self.config.get("manifest"):
            raise ValueError("Bench2Drive requires an explicit manifest; no implicit evaluation fallback")
        purpose = self.config.get("purpose")
        if purpose not in ("train", "dev", "eval"):
            raise ValueError("Bench2Drive purpose must explicitly be train, dev, or eval")
        self.manifest = load_manifest(self.config["manifest"], purpose=purpose)
        default = Path.home() / ".cache/easycarla/bench2drive" / (PINNED_COMMIT + "-p4")
        if not default.exists():
            newest = default.with_name(PINNED_COMMIT + "-p6")
            default = newest if newest.exists() else default.with_name(PINNED_COMMIT + "-p5")
        self.root = Path(self.config.get("dependency_root", default))
        self._selection = None
        self._lock = None

    def town(self, seed, options):
        options = options or {}
        entry = select_entry(self.manifest, seed, options)
        self._selection = (int(seed), dict(options), entry)
        return self.config.get("town_overrides", {}).get(entry["town"], entry["town"])

    def prepare(self, session, seed, options=None):
        options = options or {}
        if self._selection is None or self._selection[:2] != (int(seed), dict(options)):
            self.town(seed, options)
        entry = self._selection[2]
        expected_town = self.config.get("town_overrides", {}).get(entry["town"], entry["town"])
        if session.map.name.rsplit("/", 1)[-1] != expected_town:
            raise ValueError("Runtime must load adapter.town() before prepare()")
        if self._lock is None:
            self.root, self._lock = _dependencies(self.root)
        if (getattr(session, "config", {}).get("observations", {}).get("profile") == "planning_v1"
                and self._lock["patch_version"] < 6):
            raise RuntimeError("planning_v1 requires a p6 Bench2Drive copy to track disabled physics safely")
        route_file = verify_entry(entry, self.root)
        episode = Bench2DriveEpisode(session, entry, self.config, seed, self._lock)
        try:
            episode.prepare(route_file)
            return episode
        except Exception as exc:
            try:
                episode.close()
            except Exception as cleanup_error:
                exc.add_note(f"Bench2Drive cleanup also failed: {cleanup_error}") if hasattr(exc, "add_note") else None
            raise RuntimeError(f"Bench2Drive generation failed (route={entry['route_id']}, seed={seed}): {exc}") from exc


class Bench2DriveEpisode:
    task_profiles = frozenset({"driving", "bench2drive_native"})

    def __init__(self, session, entry, config, seed, lock):
        self.session, self.entry, self.config = session, entry, config
        self.seed = int(seed)
        self.world, self.client = WorldBridge(self), ClientBridge(self)
        self.initializing = True
        self.owned_ids = set()
        self.semantic_roles = {}
        self.spawn_count = 0
        self.provider = self.scene = self.ego = None
        self.route = []
        self.controllers = {}
        self.deferred_tick_requests = self.deferred_batch_ticks = 0
        self.tick_count = 0
        self.statistics = None
        self._closed = False
        self._lease_roles = {}
        self._scenario_progress = {}
        self.state = {"leased": set(), "suppressed": {}, "capture_ego": False, "world": self.world,
                      "controls": session.controls, "owner": "bench2drive/native",
                      "native_events": queue.Queue(maxsize=4096), "closed": False, "callback_fault": None}
        self.metadata = {"source": "bench2drive", "scenario_id": str(entry["route_id"]), "seed": self.seed,
                         "town": entry["town"], "scenario_types": entry["scenario_types"],
                         "loaded_town": session.map.name.rsplit("/", 1)[-1],
                         "route_fingerprint": entry["route_fingerprint"], "upstream_commit": lock["commit"],
                         "patch_version": lock["patch_version"], "native_npc": True,
                         "mechanism_replacements": [], "generation_failure": None,
                         "native_validation": "not_run", "repeatability_validation": "not_run"}

    @contextmanager
    def scope(self):
        with control_scope(self.state):
            yield

    def prepare(self, route_file):
        from srunner.scenariomanager.carla_data_provider import CarlaDataProvider
        from srunner.scenariomanager.timer import GameTime
        from leaderboard.utils.route_parser import RouteParser
        from leaderboard.scenarios.route_scenario import RouteScenario
        import py_trees
        self.provider, self.clock = CarlaDataProvider, GameTime
        random.seed(self.seed)
        np.random.seed(self.seed % (2**32))
        py_trees.blackboard.Blackboard().__dict__.clear()
        CarlaDataProvider._random_seed = self.seed % (2**32)
        CarlaDataProvider._rng = np.random.RandomState(CarlaDataProvider._random_seed)
        CarlaDataProvider.set_client(self.client)
        CarlaDataProvider.set_world(self.world)
        CarlaDataProvider.set_traffic_manager_port(self.session.sim["tm_port"])
        CarlaDataProvider.set_runtime_init_mode(False)
        routes = RouteParser.parse_routes_file(str(route_file), str(self.entry["route_id"]))
        if len(routes) != 1:
            raise RuntimeError("Pinned parser did not select exactly one route")
        route_config = routes[0]
        with self.scope():
            self.scene = RouteScenario(self.world, route_config, debug_mode=0, criteria_enable=True)
        if len(self.scene.scenario_configurations) != len(route_config.scenario_configs):
            raise RuntimeError("Native filtering dropped a configured scenario trigger")
        self.ego = self.scene.ego_vehicles[0]
        self.state["ego_id"] = self.ego.id
        self.route = [transform for transform, _ in self.scene.route]
        self.metadata["scenario_names"] = [s.name for s in route_config.scenario_configs]
        from .statistics import NativeStatistics
        self.statistics = NativeStatistics(self.scene, self.entry["route_id"], self.metadata["loaded_town"],
                                           self.config, self.session.sim["physics_dt"], self.session.sim["decision_dt"])
        self.initializing = False
        GameTime.restart()
        self.after_tick(self.session.world.get_snapshot())

    def before_tick(self):
        self._ensure_open()
        if self.state["callback_fault"]:
            raise RuntimeError(self.state["callback_fault"])
        # Deterministic physical-tick scheduling, replacing the upstream 1s
        # wall-clock construction thread. Construction can never tick physics.
        with self.scope():
            resume = self.state.pop("resume_autopilot", {})
            for actor_id, actor in resume.items():
                if actor_id in self.owned_ids:
                    native_actor_call(actor, "set_autopilot", True, self.session.sim["tm_port"])
            while True:
                try:
                    actor_id, callback = self.state["native_events"].get_nowait()
                except queue.Empty:
                    break
                if actor_id in self.owned_ids:
                    callback()
            self.scene.build_scenarios(self.ego, debug=False)
            self.scene.spawn_parked_vehicles(self.ego)

    def transform_control(self, action):
        """Evaluate native trees and capture ego disturbances before submission."""
        self._ensure_open()
        import carla
        import py_trees
        control = carla.VehicleControl(steer=float(action[0]), throttle=float(action[1]), brake=float(action[2]))
        self.state.update(capture_ego=True, ego_control=control)
        py_trees.blackboard.Blackboard().set("AV_control", control, overwrite=True)
        try:
            with self.scope():
                # Controls consume the current observed state. Evaluation is
                # run after physics on the resulting frame, exactly once.
                for node in self.scene.scenario_tree.children:
                    if node is not self.scene.criteria_tree and node is not self.scene.timeout_node:
                        node.tick_once()
        finally:
            self.state["capture_ego"] = False
        actual = self.state["ego_control"]
        self.tick_count += 1
        # Native ControlLoss can add throttle even while braking. Preserve and
        # explicitly report the native transform instead of revalidating it as
        # an external action or silently dropping the disturbance.
        return np.array([actual.steer, actual.throttle, actual.brake], dtype=np.float32)

    def resolve_control(self, requested):
        from easycarla.scenarios.contracts import ResolvedControl
        return ResolvedControl(self.transform_control(requested), allow_combined=True)

    def scenario_facts(self):
        from easycarla.scenarios.contracts import ScenarioFacts
        import py_trees
        timeout = bool(getattr(getattr(self.scene, "timeout_node", None), "timeout", False))
        done = self.scene.scenario_tree.status not in (py_trees.common.Status.RUNNING, py_trees.common.Status.INVALID)
        return ScenarioFacts(terminated=bool(done and not timeout), truncated=timeout)

    def export_state(self, actor_roles):
        """A versioned diagnostic boundary, never a claimed native snapshot."""
        info = self.info()
        return {"schema_version": 1, "source": "bench2drive", "restorable": False,
                "limitations": self.capabilities()["replay_restrictions"],
                "scenario_id": self.metadata["scenario_id"], "tick_count": self.tick_count,
                "native_status": info["native_status"], "native_scenarios": info["native_scenarios"],
                "pending_scenarios": info["pending_scenarios"],
                "takeovers": sorted(self._lease_roles),
                "suppressed_controls": {actor_roles.get(actor_id, str(actor_id)): count
                                        for actor_id, count in self.state["suppressed"].items()}}

    def after_tick(self, snapshot):
        self.clock.on_carla_tick(snapshot.timestamp)
        self.provider.on_carla_tick()
        if self.scene is not None and self.tick_count:
            import py_trees
            with self.scope():
                self.scene.criteria_tree.tick_once()
                self.scene.timeout_node.tick_once()
                children = self.scene.scenario_tree.children
                status = py_trees.common.Status.RUNNING
                if any(c.status == py_trees.common.Status.FAILURE for c in children):
                    status = py_trees.common.Status.FAILURE
                elif any(c.status == py_trees.common.Status.SUCCESS for c in children):
                    status = py_trees.common.Status.SUCCESS
                self.scene.scenario_tree.status = status
        if self.statistics is not None:
            self.statistics.update(snapshot, self.ego, initializing=self.tick_count == 0)
        self._latch_progress()

    def _latch_progress(self):
        from .progress import scenario_progress
        if self.scene is None:
            return
        for scenario in self.scene.list_scenarios:
            previous = self._scenario_progress.setdefault(scenario.config.name, {})
            progress = scenario_progress(scenario)
            for field in ("activation_observed", "completion_observed"):
                if progress[field] is True:
                    previous[field] = True

    def info(self):
        import py_trees
        from .progress import scenario_progress
        criteria = [{"name": c.name, "status": c.test_status,
                     "actual_value": float(c.actual_value), "expected_value": float(c.success_value)}
                    for c in self.scene.get_criteria()]
        success = any(c["name"] == "RouteCompletionTest" and c["status"] == "SUCCESS" for c in criteria)
        timeout_node = getattr(self.scene, "timeout_node", None)
        timeout = bool(getattr(timeout_node, "timeout", False))
        status = self.scene.scenario_tree.status
        done = status not in (py_trees.common.Status.RUNNING, py_trees.common.Status.INVALID)
        scenarios = []
        for scenario in self.scene.list_scenarios:
            progress = scenario_progress(scenario)
            previous = self._scenario_progress.setdefault(scenario.config.name, {})
            # The actual behavior can later be invalidated by a parent. Keep
            # positive physical-tick observations for this episode only.
            for field in ("activation_observed", "completion_observed"):
                if previous.get(field) is True:
                    progress[field] = True
                elif progress[field] is True:
                    previous[field] = True
            scenarios.append({"name": scenario.config.name, "type": scenario.config.type,
                              "status": str(scenario.behavior_tree.status), **progress})
        return {"native_done": done, "native_success": success, "native_timeout": timeout,
                "native_status": str(status), "native_criteria": criteria,
                "native_scenario_progress_schema": 1,
                "native_scenarios": scenarios,
                "pending_scenarios": [s.name for s in self.scene.missing_scenario_configurations],
                "deferred_tick_requests": self.deferred_tick_requests,
                "deferred_batch_ticks": self.deferred_batch_ticks,
                "native_spawn_velocity_handoffs": self.state.get("spawn_velocity_handoffs", 0),
                "native_constant_velocity_handoffs": self.state.get("constant_velocity_handoffs", 0),
                "native_velocity_impulse_handoffs": self.state.get("velocity_impulse_handoffs", 0),
                "native_transform_reset_handoffs": self.state.get("transform_reset_handoffs", 0),
                "native_statistics": self.statistics.result(final=done) if self.statistics is not None and (done or not getattr(self, "config", {}).get("terminal_statistics_only", False)) else None,
                "native_statistics_deferred": bool(getattr(self, "config", {}).get("terminal_statistics_only", False) and not done),
                "native_control_suppressed": {str(k): v for k, v in self.state["suppressed"].items()}}

    def _roles(self):
        roles = {}
        attributes = {"ParkingCutIn": "_parked_actor", "StaticCutIn": "_adversary_actor", "HighwayCutIn": "_cut_in_vehicle"}
        if self.scene is None:
            return roles
        for scenario in self.scene.list_scenarios:
            attribute = attributes.get(scenario.config.type)
            if attribute:
                actor = getattr(scenario, attribute, None)
                if actor is not None:
                    roles[f"{scenario.config.name}:adversary"] = (scenario, actor)
        return roles

    def capabilities(self):
        roles = {}
        for role, (scenario, actor) in self._roles().items():
            alive = bool(actor.is_alive)
            phase = self._takeover_phase(scenario, actor) if alive else "destroyed_or_unavailable"
            eligible = phase in ("waiting_for_trigger", "synchronizing_arrival")
            opted_in = bool(self.config.get("allow_unvalidated_takeover", False))
            roles[role] = {"actor_id": actor.id, "native_type": scenario.config.type,
                           "can_takeover": eligible and opted_in and actor.id not in self.state["leased"],
                           "phase": phase, "validation": "not_run", "reason": (
                               "Already controlled by the replacement" if actor.id in self.state["leased"] else
                               "Native actor is not in a supported handoff phase" if not eligible else
                               "Explicit allow_unvalidated_takeover required" if not opted_in else
                               "Experimental phase-qualified handoff; native actor lifetime remains authoritative")}
        return {"source": "bench2drive", "native_execution": True, "native_validation": "not_run",
                "fixed_recipe_rebuild": True, "prefix_replay": False,
                "replay_restrictions": ["Native Traffic Manager and behavior tree internal state is not serializable",
                                        "Same seed alone is not a verified common-prefix state"],
                "takeover_roles": roles,
                "takeover_restrictions": ["Only initialized ParkingCutIn, StaticCutIn, HighwayCutIn adversaries",
                                          "Requires native placement and explicit experimental opt-in",
                                          "No actor lifetime extension; irreversible within the episode"]}

    @staticmethod
    def _takeover_phase(scenario, actor):
        """Only explicit upstream phases may surrender an actor's actuator."""
        from .progress import scenario_progress
        progress = scenario_progress(scenario)
        if progress["extraction"] != "pinned_basic_scenario_route_wrapper_v1":
            return "unsupported_native_layout"
        nodes = list(scenario.behavior_tree.iterate())
        kind = scenario.config.type
        required = {"ParkingCutIn": {"BasicAgentBehavior"},
                    "StaticCutIn": {"CutIn", "BasicAgentBehavior"},
                    "HighwayCutIn": {"SyncArrivalWithAgent", "CutIn"}}
        actor_nodes = [node for node in nodes
                       if getattr(getattr(node, "_actor", None), "id", None) == actor.id]
        if kind not in required or not required[kind].issubset({type(node).__name__ for node in actor_nodes}):
            return "unsupported_native_layout"
        if kind in ("StaticCutIn", "HighwayCutIn"):
            placements = [node for node in nodes if (
                kind == "HighwayCutIn" and type(node).__name__ == "ActorTransformSetter"
                and getattr(getattr(node, "_actor", None), "id", None) == actor.id) or (
                kind == "StaticCutIn" and type(node).__name__ == "BatchActorTransformSetter"
                and any(getattr(candidate, "id", None) == actor.id
                        for candidate, _ in getattr(node, "_actor_transform_list", ())))]
            if len(placements) != 1:
                return "unsupported_native_layout"
            # Absolute elevation cannot establish placement: Town12 terrain
            # can remain above sea level after the native 100 m hiding offset.
            # Query only at observed decision boundaries after physics; the
            # pinned placement behavior must actually have completed first.
            if str(placements[0].status) != "Status.SUCCESS":
                return "awaiting_native_placement"
        active = [node for node in actor_nodes if str(node.status) != "Status.INVALID"]
        if any(type(node).__name__ in ("CutIn", "BasicAgentBehavior", "WaypointFollower") for node in active):
            return "native_maneuver_started"
        if any(type(node).__name__ == "SyncArrivalWithAgent" for node in active):
            return "synchronizing_arrival"
        return "waiting_for_trigger"

    def takeover(self, role):
        """Yield a placed native role to an external-to-source controller.

        The runtime installs its generic reactive controller with this actor/path.
        Upstream control calls are suppressed, but triggers and lifetimes keep
        running. This is experimental until the capability matrix is validated.
        """
        self._ensure_open()
        if role not in self._roles():
            raise ValueError(f"Unsupported or uninitialized Bench2Drive role {role!r}")
        if not self.capabilities()["takeover_roles"][role]["can_takeover"]:
            raise ValueError(self.capabilities()["takeover_roles"][role]["reason"])
        scenario, actor = self._roles()[role]
        if actor.id in self.state["leased"]:
            raise ValueError("Role already has an external controller")
        start = actor.get_transform()
        target = getattr(scenario, "_collision_wp", getattr(scenario, "_other_waypoint", None))
        path = [start]
        if target is not None:
            path.append(target.transform)
            for _ in range(40):
                candidates = target.next(2.0)
                if not candidates:
                    break
                target = candidates[0]
                path.append(target.transform)
        else:
            raise RuntimeError("Native role has no usable fixed path")
        if len(path) < 2:
            raise RuntimeError("Native role path is too short for a controller")
        self.session.controls.handoff(actor, role, expected_owner="bench2drive/native")
        self.state["leased"].add(actor.id)
        self._lease_roles[role] = actor.id
        self.metadata["mechanism_replacements"].append({"role": role, "actor_id": actor.id,
                                                       "physical_frame": self.session.world.get_snapshot().frame})
        return {"actor": actor, "path": path, "role": role, "native_lifetime": True}

    def _ensure_open(self):
        if self._closed:
            raise RuntimeError("Bench2Drive episode has been closed")

    def close(self):
        if self._closed:
            return
        self._closed = True
        self.state["closed"] = True
        self.initializing = False
        errors = []
        if self.scene is not None:
            try:
                with self.scope():
                    self.scene.terminate()
            except Exception as exc:
                errors.append(str(exc))
            # Prevent delayed __del__ callbacks from touching the next episode's
            # global provider or destroying actors after a new world has loaded.
            for scenario in [self.scene, *self.scene.list_scenarios]:
                scenario.other_actors = []
            self.scene._parked_ids = []
        if self.provider is not None:
            try:
                with self.scope():
                    self.provider.cleanup()
            except Exception as exc:
                errors.append(str(exc))
        errors.extend(str(x) for x in self.session.registry.destroy(self.owned_ids))
        self.state["leased"].clear()
        self.scene = None
        if errors:
            raise RuntimeError("Bench2Drive cleanup errors: " + "; ".join(errors))
