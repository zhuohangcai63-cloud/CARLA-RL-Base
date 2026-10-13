"""Optional experiment facade over one explicit runtime port.

Owns recording, materials, identities and frozen manifests. Episode state stays
in the runtime; reconstruction is an explicit runtime operation, never a config
mutation. No branch scheduling or training labels live here.
"""
from __future__ import annotations

from copy import deepcopy
import json
import hashlib
import inspect
import math
from typing import Any, Protocol

from ..control import ReactiveNPCController
from ..observations.geometry import xyz
from .materials import INLINE_PREFIX_BYTES, PrefixMaterialStore, encode_material
from .replay import PrefixRecorder, replay_prefix, validate_prefix
from .schema import RECIPE_VERSION, validate_recipe, validate_mechanism, simulation_recipe
from .state import capture_initial_manifest, raw_state


class ExperimentRejected(ValueError, RuntimeError):
    """Preflight rejection; the active episode remains valid."""


class ExperimentRuntimePort(Protocol):
    """The extension's read surface and only permitted runtime operations."""
    config: dict
    episode: Any
    session: Any
    observer: Any
    snapshot: Any
    task: Any
    tracker: Any
    arbiter: Any
    last_obs: Any
    elapsed: float
    physical_ticks: int
    decision_steps: int
    episode_id: str
    seed: int
    state: str
    source_config: dict

    def require_ready(self) -> None: ...
    def episode_info(self) -> dict: ...
    def mark_faulted(self) -> None: ...
    def rebuild_episode(self, recipe: dict) -> tuple: ...
    def attach_controller(self, role: str, lease: dict, controller: Any) -> None: ...
    def step(self, action: Any) -> tuple: ...
    def step_trajectory(self, trajectory: dict, execute_seconds: float) -> tuple: ...


class ExperimentSession:
    def __init__(self, runtime: ExperimentRuntimePort):
        self.runtime = runtime
        self.recorder = None
        self._recording_abort = None
        self.replaying = False
        self.materials = PrefixMaterialStore(runtime.config["experiments"])
        self._identities = {"roles": {}, "semantic": {}}
        self._initial_manifest = None

    def on_episode_ready(self):
        self.recorder = None
        self._recording_abort = None
        self._identities = {"roles": {}, "semantic": {}}
        self._initial_manifest = capture_initial_manifest(self.runtime)

    def on_episode_closed(self, preserve_recording=False):
        if preserve_recording and self.recorder is not None:
            self.recorder.stop()
            self._recording_abort = {"aborted": True, "reason": "episode_faulted",
                                     "includes_faulting_command": False}
        else:
            self.recorder = None
            self._recording_abort = None
        self._identities = {"roles": {}, "semantic": {}}
        self._initial_manifest = None
        # replaying is owned by the replay scope; resets inside it preserve it.

    def close(self):
        self.on_episode_closed()
        self.materials.close()

    def _require_episode(self, ready=False):
        if self.runtime.episode is None or self.runtime.observer is None:
            raise ExperimentRejected("reset is required before using experiment operations")
        if ready:
            self.runtime.require_ready()

    def _prefix_capability(self):
        capabilities = self.runtime.episode.capabilities()
        capability = capabilities.get("prefix_replay", False)
        available = capability.get("available", False) if isinstance(capability, dict) else bool(capability)
        if not available:
            raise ExperimentRejected("This scene does not support prefix reconstruction/replay: " +
                                     "; ".join(capabilities.get("replay_restrictions", [])))
        return capability

    def capture_initial_manifest(self):
        self._require_episode()
        self._initial_manifest = capture_initial_manifest(self.runtime)
        return deepcopy(self._initial_manifest)

    def raw_state(self):
        self._require_episode()
        result = raw_state(self.runtime, self._identities)
        initial_limitations = (self._initial_manifest or {}).get("limitations", [])
        result["limitations"] = sorted(set(result["limitations"]) |
                                       {"initial_manifest: " + item for item in initial_limitations})
        return result

    def describe_episode(self):
        self._require_episode()
        runtime = self.runtime
        at_start = runtime.decision_steps == 0 and runtime.physical_ticks == 0
        if self._initial_manifest is None and at_start:
            self.capture_initial_manifest()
        return {"version": RECIPE_VERSION, "seed": runtime.seed,
                "source_config": deepcopy(runtime.source_config),
                "metadata": deepcopy(runtime.episode.metadata),
                "simulation": simulation_recipe(runtime.config["simulation"]),
                "observation_config": deepcopy(runtime.config["observations"]),
                "task_config": deepcopy(runtime.config["task"]),
                "initial_manifest": deepcopy(self._initial_manifest),
                "at_episode_start": at_start,
                "initial_controllers": {role: ctrl.state_dict() for role, (_, ctrl)
                                        in runtime.episode.controllers.items()} if at_start else {},
                "capabilities": deepcopy(runtime.episode.capabilities())}

    def _record_command(self, command):
        if self.recorder is not None and not self.replaying:
            try:
                self.recorder.append(command, self.raw_state())
            except Exception:
                self.runtime.mark_faulted()
                raise

    def begin_prefix(self):
        self._require_episode(ready=True)
        self._prefix_capability()
        runtime = self.runtime
        if runtime.decision_steps != 0 or runtime.physical_ticks != 0:
            raise ValueError("A public prefix must start at the episode beginning")
        if self.recorder is not None:
            raise ExperimentRejected("Prefix recording is already active")
        if self.replaying:
            raise ExperimentRejected("Cannot start recording while replaying")
        settings = runtime.config["experiments"]
        recorder = PrefixRecorder(settings.get("max_prefix_steps", 5000),
                                  settings.get("max_prefix_bytes", 64 * 1024 * 1024))
        recipe = self.describe_episode()
        recorder.start(recipe, self.raw_state())
        self.recorder = recorder
        self._recording_abort = None
        return {"recording": True, "max_commands": recorder.max_commands,
                "max_bytes": recorder.max_bytes, "recipe": recipe}

    def finish_prefix(self, export=False):
        if type(export) is not bool:
            raise ValueError("export must be a boolean")
        if self.recorder is None:
            raise ExperimentRejected("No prefix recording is active")
        # Validate/allocation failure leaves recording active, including when
        # the caller can release a handle and retry finishing the same prefix.
        result = self.recorder.preview()
        if self._recording_abort is not None:
            result.pop("sha256")
            result["recording_status"] = deepcopy(self._recording_abort)
            result["recording_status"]["commands_recorded"] = len(result["steps"])
            result["sha256"] = hashlib.sha256(encode_material(result)).hexdigest()
        payload = encode_material(result)
        if export:
            if len(payload) > INLINE_PREFIX_BYTES:
                raise ValueError("Inline prefix exceeds 512 KiB; use the default handle and chunk export")
            response = result
        else:
            response = self.materials.put(payload)
            if self._recording_abort is not None:
                response["recording_status"] = deepcopy(result["recording_status"])
        self.recorder.finish()
        self.recorder = None
        self._recording_abort = None
        return response

    def set_mechanism(self, role, mechanism):
        if not isinstance(role, str) or not role:
            raise ValueError("role must be a nonempty string")
        validate_mechanism(mechanism)
        self._require_episode(ready=True)
        managed = self.runtime.episode.controllers
        if role not in managed:
            raise ValueError(f"Role {role!r} does not have a managed reactive controller")
        if self.recorder is not None:
            self.recorder.ensure_capacity()
        managed[role][1].set_mechanism(mechanism)
        self._record_command({"method": "set_mechanism", "role": role, "mechanism": deepcopy(mechanism)})
        return {"role": role, "mechanism": managed[role][1].mechanism_name,
                "parameters": managed[role][1].state_dict()["mechanism"]}

    def takeover(self, role, mechanism="C", target_speed=8.0):
        if not isinstance(role, str) or not role:
            raise ValueError("role must be a nonempty string")
        self._require_episode(ready=True)
        runtime = self.runtime
        if role in runtime.episode.controllers:
            raise ValueError("Role is already managed; use set_mechanism")
        roles = runtime.episode.capabilities().get("takeover_roles", {})
        if role not in roles:
            raise ValueError(f"No takeover implementation for role {role!r}")
        if isinstance(roles, dict) and not roles[role].get("can_takeover", False):
            raise ValueError(roles[role].get("reason", "Role cannot be taken over in this phase"))
        validate_mechanism(mechanism)
        try:
            target_speed = float(target_speed)
        except (TypeError, ValueError) as exc:
            raise ValueError("target_speed must be positive and finite") from exc
        if not math.isfinite(target_speed) or target_speed <= 0:
            raise ValueError("target_speed must be positive and finite")
        if self.recorder is not None:
            self.recorder.ensure_capacity()
        # Native takeover may mutate controller state even when it raises.
        # Every error after this boundary faults the episode.
        try:
            lease = runtime.episode.takeover(role)
            route = [xyz(point.location).tolist() if hasattr(point, "location") else list(point) for point in lease["path"]]
            controller = ReactiveNPCController(role, route, mechanism=mechanism,
                                               target_speed=target_speed, seed=runtime.seed)
            runtime.attach_controller(role, lease, controller)
            self._record_command({"method": "takeover", "role": role, "mechanism": deepcopy(mechanism),
                                  "target_speed": float(target_speed)})
            return {"role": role, "mechanism": controller.mechanism_name,
                    "parameters": controller.state_dict()["mechanism"],
                    "native_lifetime": bool(lease.get("native_lifetime", True)), "validation": "not_run"}
        except Exception:
            runtime.mark_faulted()
            raise

    def _resolve_prefix(self, prefix=None, handle=None):
        if handle is not None:
            if prefix is not None:
                raise ValueError("Provide prefix or handle, not both")
            return json.loads(self.materials.read(handle))
        if isinstance(prefix, str):
            return json.loads(self.materials.read(prefix))
        if isinstance(prefix, dict) and "handle" in prefix and "version" not in prefix:
            return json.loads(self.materials.read(prefix["handle"], prefix.get("sha256")))
        if len(encode_material(prefix)) > INLINE_PREFIX_BYTES:
            raise ValueError("Inline prefix exceeds 512 KiB; import chunks and replay its handle")
        return prefix

    def replay_prefix(self, prefix=None, handle=None, tolerances=None):
        self._require_episode()
        self._prefix_capability()
        if self.recorder is not None:
            raise ExperimentRejected("Finish the active recording before replay")
        if self.replaying:
            raise ExperimentRejected("Replay is already active")
        prefix = self._resolve_prefix(prefix, handle)
        material = validate_prefix(prefix)
        settings = self.runtime.config["experiments"]
        if len(material["steps"]) > settings.get("max_prefix_steps", 5000):
            raise ValueError("Prefix exceeds configured command capacity")
        if len(encode_material(prefix)) > settings.get("max_prefix_bytes", 64 * 1024 * 1024):
            raise ValueError("Prefix exceeds configured byte capacity")
        validate_recipe(material, self.runtime.config, tolerances)
        self.replaying = True
        try:
            report = replay_prefix(prefix, self._rebuild, self._execute,
                                   self.raw_state, tolerances)
            report.pop("result", None)
            return {"observation": self.runtime.last_obs, "info": self.runtime.episode_info(), "report": report}
        except Exception:
            self.runtime.mark_faulted()
            raise
        finally:
            self.replaying = False

    def _rebuild(self, recipe):
        result = self.runtime.rebuild_episode(recipe)
        actual = self.capture_initial_manifest()
        expected = recipe["initial_manifest"]
        # Pose is tolerance-checked by raw_state; immutable physical settings,
        # attributes, actual spawn order and controller routes must match exactly.
        def invariant(manifest):
            return {**manifest, "actors": [{key: value for key, value in actor.items() if key != "transform"}
                                            for actor in manifest["actors"]]}
        if invariant(actual) != invariant(expected):
            raise RuntimeError("Rebuilt source identity, actor physics, spawn order, fixed routes or effective world settings differ from recipe")
        for role, state in recipe["initial_controllers"].items():
            if role not in self.runtime.episode.controllers:
                raise RuntimeError(f"Rebuild did not recreate controlled role {role!r}")
            self.runtime.episode.controllers[role][1].load_state_dict(state)
        return result

    def _execute(self, command):
        method = command["method"]
        if method == "step":
            return self.runtime.step(command["action"])
        if method == "step_trajectory":
            trajectory = deepcopy(command["trajectory"])
            trajectory["reference_frame"] = self.runtime.snapshot.frame
            return self.runtime.step_trajectory(trajectory, command["execute_seconds"])
        if method in ("set_mechanism", "takeover"):
            return self.dispatch(method, **{key: value for key, value in command.items() if key != "method"})
        raise ValueError(f"Unsupported prefix command {method!r}")

    def dispatch(self, method, **kwargs):
        if not isinstance(method, str):
            raise ValueError("Experiment operation must be a string")
        methods = {"query_state": self.raw_state, "describe_episode": self.describe_episode,
                   "begin_prefix": self.begin_prefix, "finish_prefix": self.finish_prefix,
                   "set_mechanism": self.set_mechanism, "takeover": self.takeover,
                   "replay_prefix": self.replay_prefix}
        transfers = ("begin_prefix_import", "release_prefix", "export_prefix_chunk",
                     "import_prefix_chunk", "finish_prefix_import")
        methods.update({name: getattr(self.materials, name) for name in transfers})
        if method not in methods:
            raise ValueError(f"Unknown experiment operation {method!r}")
        try:
            inspect.signature(methods[method]).bind(**kwargs)
        except TypeError as exc:
            raise ValueError(f"Invalid arguments for experiment operation {method!r}: {exc}") from exc
        return methods[method](**kwargs)
