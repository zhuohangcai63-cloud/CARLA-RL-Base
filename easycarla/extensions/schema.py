"""Versioned recipe and command preflight; rejection never rebuilds a world."""
from collections.abc import Mapping
from copy import deepcopy
import hashlib
import math
from pathlib import Path

from ..control import ReactiveNPCController, TrajectoryTracker, validate_action, mechanism_preset
from ..control.npc import Mechanism
from .replay import validate_tolerances, compare_states

RECIPE_VERSION = 2
STATE_VERSION = 1
SIMULATION_RECIPE_FIELDS = ("physics_dt", "decision_dt", "history_dt", "map_layers",
                            "no_rendering_mode", "tile_stream_distance", "actor_active_distance",
                            "reload_on_reset", "spectator_as_ego", "map_overrides")


def simulation_recipe(settings):
    """Physical world settings, independent of local RPC ports and file paths."""
    return {key: deepcopy(settings.get(key)) for key in SIMULATION_RECIPE_FIELDS}


def validate_recipe(material, config, tolerances):
    validate_tolerances(tolerances)
    recipe = material["recipe"]
    if not isinstance(recipe, Mapping) or type(recipe.get("version")) is not int or recipe["version"] != RECIPE_VERSION:
        raise ValueError("Unsupported episode recipe version; legacy v1 used implicit Python state. Record a new prefix with recipe v2")
    source_config = recipe.get("source_config", {})
    if source_config.get("source") not in ("scenic", "bench2drive"):
        raise ValueError("Unsupported episode recipe source")
    required = ("seed", "metadata", "simulation", "observation_config", "task_config",
                "initial_manifest", "initial_controllers", "capabilities")
    if any(key not in recipe for key in required) or not recipe.get("at_episode_start"):
        raise ValueError("Prefix recipe does not describe a complete episode starting point")
    if type(recipe["seed"]) is not int:
        raise ValueError("Recipe seed must be an integer")
    prefix_cap = recipe["capabilities"].get("prefix_replay", False)
    if not (prefix_cap.get("available", False) if isinstance(prefix_cap, dict) else prefix_cap):
        raise ValueError("Recorded source does not support replay")
    manifest = recipe["initial_manifest"]
    if not isinstance(manifest, dict) or type(manifest.get("schema_version")) is not int or manifest["schema_version"] != 1:
        raise ValueError("Prefix has no versioned frozen initial actor/physics manifest")
    if not all(key in manifest for key in ("actors", "controller_routes", "controller_version", "ego_route",
                                          "effective_world_settings", "applied_map_override")):
        raise ValueError("Prefix initial manifest is incomplete")
    for state in (material["initial_state"], *(step["state"] for step in material["steps"])):
        if not isinstance(state, dict) or type(state.get("schema_version")) is not int or state["schema_version"] != STATE_VERSION:
            raise ValueError("Unsupported diagnostic state schema_version; record a new prefix")
        if any(key not in state for key in ("actors", "signals", "controllers", "events", "history", "task")):
            raise ValueError("Prefix diagnostic state is incomplete")
        # Exercise physical shapes/types before reconstruction as well. A bad
        # signed/imported record must not advance before discovering malformed
        # actor vectors or section data.
        compare_states(state, state, tolerances)
    clock = simulation_recipe(config["simulation"])
    if (recipe["simulation"] != clock or recipe["observation_config"] != config["observations"]
            or recipe["task_config"] != config["task"]):
        raise ValueError("Replay requires the same simulation, observation and task configuration")
    source = recipe["metadata"]
    if source.get("source") != source_config["source"]:
        raise ValueError("Recipe source metadata and configuration disagree")
    if source.get("source") == "scenic" and source.get("source_sha256"):
        path = Path(source_config["file"]).expanduser()
        if hashlib.sha256(path.read_bytes()).hexdigest() != source["source_sha256"]:
            raise ValueError("Scenic template changed since the prefix was recorded")
    # Validate controller loads on isolated throwaway controllers before reset.
    for role, state in recipe["initial_controllers"].items():
        route = manifest["controller_routes"].get(role)
        if route is None:
            raise ValueError(f"No frozen route for initial controller {role!r}")
        controller = ReactiveNPCController(role, route, seed=recipe["seed"])
        controller.load_state_dict(state)
    managed_roles = set(recipe["initial_controllers"])
    takeover_roles = recipe["capabilities"].get("takeover_roles", {})
    for step in material["steps"]:
        command = step["command"]
        validate_command(command, clock)
        if command["method"] == "set_mechanism" and command["role"] not in managed_roles:
            raise ValueError(f"Recorded mechanism role {command['role']!r} is not managed")
        if command["method"] == "takeover":
            if command["role"] in managed_roles or command["role"] not in takeover_roles:
                raise ValueError("Recorded takeover role is unsupported or already managed")
            managed_roles.add(command["role"])
    return recipe


def validate_command(command, clock):
    if not isinstance(command, Mapping):
        raise ValueError("Prefix command must be an object")
    method = command.get("method")
    if method == "step":
        validate_action(command.get("action"))
    elif method == "step_trajectory":
        trajectory = command.get("trajectory")
        if not isinstance(trajectory, Mapping):
            raise ValueError("Recorded trajectory must be an object")
        frame = trajectory.get("reference_frame")
        if type(frame) is not int or frame < 0:
            raise ValueError("Recorded trajectory reference_frame must be a nonnegative integer")
        duration = command.get("execute_seconds")
        if not isinstance(duration, (int, float)) or isinstance(duration, bool):
            raise ValueError("Recorded trajectory duration must be numeric")
        ticks = duration / clock["physics_dt"]
        if not math.isfinite(ticks) or ticks <= 0 or abs(round(ticks) - ticks) > 1e-8:
            raise ValueError("Trajectory duration must be a positive integer number of physics ticks")
        TrajectoryTracker().start(trajectory, frame, {"position": [0, 0, 0], "yaw": 0, "speed": 0}, duration)
    elif method in ("set_mechanism", "takeover"):
        if not isinstance(command.get("role"), str) or not command["role"]:
            raise ValueError("Mechanism role must be a nonempty string")
        mechanism = command.get("mechanism")
        validate_mechanism(mechanism)
        if method == "takeover":
            speed = command.get("target_speed", 8.0)
            if not isinstance(speed, (int, float)) or not math.isfinite(speed) or speed <= 0:
                raise ValueError("target_speed must be positive and finite")
    else:
        raise ValueError(f"Prefix includes an unsupported command {method!r}")


def validate_mechanism(mechanism):
    if isinstance(mechanism, str):
        return mechanism_preset(mechanism)
    if not isinstance(mechanism, Mapping):
        raise ValueError("mechanism must be a preset name or parameter object")
    try:
        return Mechanism(**mechanism)
    except TypeError as exc:
        raise ValueError(f"Invalid mechanism parameters: {exc}") from exc
