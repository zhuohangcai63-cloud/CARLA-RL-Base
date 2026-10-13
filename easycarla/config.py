"""Small, explicit service configuration; no simulator imports."""
from copy import deepcopy
import math
import os
from pathlib import Path
from numbers import Integral, Real
import re
import sys


DEFAULTS = {
    "backend": "carla",
    "simulation": {"host": "127.0.0.1", "port": 2000, "tm_port": 8000,
                   "timeout": 30.0, "physics_dt": 0.05, "decision_dt": 0.2,
                   "history_dt": 0.1, "reload_on_reset": True,
                   "no_rendering_mode": False, "map_layers": "all",
                   "spectator_as_ego": False,
                   "tile_stream_distance": None, "actor_active_distance": None,
                   "map_overrides": {},
                   "map_affinity": None,
                   "carla_root": os.environ.get("CARLA_ROOT") or str(Path.home() / "software" / "carla")},
    "scenario": {"source": "scenic", "town": "Town10HD", "fallback": None,
                 "file": "scenarios/scenic/mixed_long_tail.scenic", "params": {}},
    "observations": {"profile": "legacy", "max_objects": 64, "history_frames": 21, "range": 120.0,
                     "route_points": 128, "max_signals": 32, "max_obstacles": 128,
                     "rgb": False, "bev": False, "lidar": False,
                     "image_width": 320, "image_height": 180,
                     "bev_size": 256, "lidar_points": 65536, "sensor_timeout": 10.0,
                     "collision_settle_seconds": 0.002},
    "trajectory": {"points": 81, "dt": 0.1, "reference_point": "actor"},
    "performance": {"deferred_observations": False},
    "task": {"profile": "driving", "max_seconds": 300.0, "goal_radius": 5.0,
             "termination": {"collision": True, "offroad": True, "goal": True, "timeout": True},
             "weights": {"progress": 0.1, "time": -0.1, "acceleration": -0.1,
                         "jerk": -0.05, "overspeed": -1.0, "collision": -50.0,
                         "offroad": -10.0, "signal": -10.0, "goal": 10.0}},
    "experiments": {"enabled": False, "max_prefix_steps": 5000},
}


def merged(base, updates):
    if not isinstance(updates, dict):
        raise ValueError("configuration sections must be mappings")
    result = deepcopy(base)
    for key, value in updates.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = merged(result[key], value)
        else:
            result[key] = deepcopy(value)
    return result


def _mapping(value, path):
    if not isinstance(value, dict):
        raise ValueError(f"{path} must be a mapping")
    return value


def _number(value, path, *, minimum=None, inclusive=False, maximum=None):
    # bool is a subclass of int; accepting it silently turns a flag into a
    # one-second tick, one-byte buffer or reward coefficient.
    try:
        finite = not isinstance(value, bool) and isinstance(value, Real) and math.isfinite(value)
    except (OverflowError, TypeError):
        finite = False
    if not finite:
        raise ValueError(f"{path} must be a finite number (not a boolean)")
    if minimum is not None and (value < minimum if inclusive else value <= minimum):
        qualifier = "at least" if inclusive else "greater than"
        raise ValueError(f"{path} must be {qualifier} {minimum}")
    if maximum is not None and value > maximum:
        raise ValueError(f"{path} must not exceed {maximum}")
    return value


def _integer(value, path, minimum=1, maximum=sys.maxsize):
    if isinstance(value, bool) or not isinstance(value, Integral) or not minimum <= value <= maximum:
        raise ValueError(f"{path} must be an integer between {minimum} and {maximum} (not a boolean)")
    return value


def _boolean(value, path):
    if not isinstance(value, bool):
        raise ValueError(f"{path} must be a boolean")


def _scenario(config, path="scenario", depth=0):
    _mapping(config, path)
    if depth > 16:
        raise ValueError("Scenario fallback configuration is too deeply nested")
    source = config.get("source", "scenic")
    if source not in ("scenic", "bench2drive", "mixed"):
        raise ValueError(f"{path}.source must be scenic, bench2drive or mixed")
    if source == "mixed":
        entries = config.get("sources")
        if not isinstance(entries, list) or not entries:
            raise ValueError(f"{path}.sources must be a nonempty list")
        total = 0.0
        for index, entry in enumerate(entries):
            where = f"{path}.sources[{index}]"
            _mapping(entry, where)
            if entry.get("source") not in ("scenic", "bench2drive"):
                raise ValueError(f"{where}.source must be scenic or bench2drive")
            total += _number(entry.get("weight", 1), where + ".weight", minimum=0, inclusive=True)
            _scenario(entry, where, depth + 1)
        if not math.isfinite(total) or total <= 0:
            raise ValueError(f"{path}.sources weights must have a positive finite sum")
    for key in ("max_iterations", "route_attempts"):
        if key in config:
            _integer(config[key], path + "." + key)
    if "max_active_events" in config:
        _integer(config["max_active_events"], path + ".max_active_events", minimum=0)
    settings = _mapping(config.get("bench2drive", config), path + ".bench2drive")
    if "metric_history_ticks" in settings:
        _integer(settings["metric_history_ticks"], path + ".metric_history_ticks", maximum=4096)
    if "purpose" in settings and settings["purpose"] not in ("train", "dev", "eval"):
        raise ValueError(f"{path}.purpose must be train, dev or eval")
    for key in ("native_metrics", "allow_unvalidated_takeover", "terminal_statistics_only"):
        if key in settings:
            _boolean(settings[key], path + "." + key)
    npcs = config.get("reactive_npcs", [])
    if not isinstance(npcs, list):
        raise ValueError(f"{path}.reactive_npcs must be a list")
    for index, npc in enumerate(npcs):
        where = f"{path}.reactive_npcs[{index}]"
        _mapping(npc, where)
        for key in ("target_speed", "distance", "merge_length", "continuation_length", "search_distance", "approach_distance"):
            if key in npc:
                _number(npc[key], where + "." + key, minimum=0)
    fallback = config.get("fallback")
    if fallback is not None:
        _scenario(fallback, path + ".fallback", depth + 1)


def validate_config(config):
    cfg = merged(DEFAULTS, config)
    if cfg["backend"] not in ("carla", "fake"):
        raise ValueError("backend must be carla or fake")
    sim = _mapping(cfg["simulation"], "simulation")
    for key in ("physics_dt", "decision_dt", "history_dt", "timeout"):
        _number(sim[key], "simulation." + key, minimum=0)
    for key in ("port", "tm_port"):
        _integer(sim[key], "simulation." + key, maximum=65535)
    _boolean(sim["reload_on_reset"], "simulation.reload_on_reset")
    _boolean(sim["no_rendering_mode"], "simulation.no_rendering_mode")
    _boolean(sim["spectator_as_ego"], "simulation.spectator_as_ego")
    if sim["map_affinity"] is not None and (not isinstance(sim["map_affinity"], str) or
            not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", sim["map_affinity"])):
        raise ValueError("simulation.map_affinity must be an exact town basename or null")
    if sim["map_layers"] not in ("all", "none"):
        raise ValueError("simulation.map_layers must be all or none")
    for key in ("tile_stream_distance", "actor_active_distance"):
        if sim[key] is not None:
            _number(sim[key], "simulation." + key, minimum=0)
    if (sim["tile_stream_distance"] is not None and sim["actor_active_distance"] is not None
            and sim["actor_active_distance"] > sim["tile_stream_distance"]):
        raise ValueError("actor_active_distance must not exceed tile_stream_distance")
    overrides = _mapping(sim["map_overrides"], "simulation.map_overrides")
    allowed = {"tile_stream_distance", "actor_active_distance"}
    for town, override in overrides.items():
        if not isinstance(town, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", town):
            raise ValueError("simulation.map_overrides keys must be exact map basenames, without paths or wildcards")
        where = "simulation.map_overrides." + town
        _mapping(override, where)
        if not override or set(override) - allowed:
            raise ValueError(f"{where} must contain only tile_stream_distance and/or actor_active_distance")
        for key, value in override.items():
            _number(value, where + "." + key, minimum=0)
        resolved = {**sim, **override}
        if (resolved["tile_stream_distance"] is not None and resolved["actor_active_distance"] is not None
                and resolved["actor_active_distance"] > resolved["tile_stream_distance"]):
            raise ValueError(f"{where}: actor_active_distance must not exceed tile_stream_distance after merging base values")
    if sim["physics_dt"] > 0.1:
        raise ValueError("physics_dt must not exceed 0.1 seconds")
    for key in ("decision_dt", "history_dt"):
        ratio = sim[key] / sim["physics_dt"]
        if not math.isfinite(ratio) or ratio < 1 or ratio > sys.maxsize or abs(round(ratio) - ratio) > 1e-8:
            raise ValueError(f"{key} must be an integer multiple of physics_dt")
    obs = _mapping(cfg["observations"], "observations")
    if obs["profile"] not in ("legacy", "planning_v1"):
        raise ValueError("observations.profile must be legacy or planning_v1")
    trajectory = _mapping(cfg["trajectory"], "trajectory")
    _integer(trajectory["points"], "trajectory.points", minimum=2)
    _number(trajectory["dt"], "trajectory.dt", minimum=0)
    if trajectory["reference_point"] not in ("actor", "rear_axle"):
        raise ValueError("trajectory.reference_point must be actor or rear_axle")
    if (trajectory["points"] - 1) * trajectory["dt"] < sim["decision_dt"]:
        raise ValueError("trajectory must cover the decision window")
    if trajectory["reference_point"] == "rear_axle" and obs["profile"] != "planning_v1":
        raise ValueError("rear_axle trajectories require planning_v1 geometry")
    _boolean(_mapping(cfg["performance"], "performance")["deferred_observations"],
             "performance.deferred_observations")
    for key in ("max_objects", "history_frames", "route_points", "max_signals", "max_obstacles",
                "image_width", "image_height", "bev_size", "lidar_points"):
        _integer(obs[key], "observations." + key)
    for key in ("range", "sensor_timeout"):
        _number(obs[key], "observations." + key, minimum=0)
    for key in ("rgb", "bev", "lidar"):
        _boolean(obs[key], "observations." + key)
    if sim["no_rendering_mode"] and (obs["rgb"] or obs["bev"]):
        raise ValueError("RGB and BEV cameras require no_rendering_mode=false")
    _number(obs["collision_settle_seconds"], "observations.collision_settle_seconds",
            minimum=0, inclusive=True, maximum=0.1)
    # Reject sizes that cannot even be represented by NumPy's pointer-sized
    # shapes before actors or sensors are created. Actual IPC byte capacities
    # remain explicit, separately allocated service configuration.
    for name, byte_count in (("object history", obs["history_frames"] * obs["max_objects"] * 58),
                             ("RGB image", obs["image_width"] * obs["image_height"] * 4),
                             ("BEV image", obs["bev_size"] ** 2 * 4),
                             ("LiDAR points", obs["lidar_points"] * 16)):
        if byte_count > sys.maxsize:
            raise ValueError(f"{name} capacity exceeds representable array size")
    task = _mapping(cfg["task"], "task")
    if task["profile"] not in ("driving", "bench2drive_native"):
        raise ValueError("unknown task profile")
    termination = _mapping(task["termination"], "task.termination")
    if set(termination) != {"collision", "offroad", "goal", "timeout"}:
        raise ValueError("task.termination requires collision/offroad/goal/timeout booleans")
    for key, value in termination.items():
        _boolean(value, "task.termination." + key)
    if task["profile"] == "bench2drive_native" and not all(termination.values()):
        raise ValueError("Custom termination applies to driving only; preserve Bench2Drive native rules")
    for key in ("max_seconds", "goal_radius"):
        _number(task[key], "task." + key, minimum=0)
    for key, value in _mapping(task["weights"], "task.weights").items():
        _number(value, "task.weights." + str(key))
    experiments = _mapping(cfg["experiments"], "experiments")
    _boolean(experiments["enabled"], "experiments.enabled")
    for key in ("max_prefix_steps", "max_prefix_bytes", "prefix_cache_bytes", "max_prefix_handles"):
        if key in experiments:
            _integer(experiments[key], "experiments." + key)
    ipc = _mapping(cfg.get("ipc", {}), "ipc")
    for key in ("request_bytes", "response_bytes", "static_bytes"):
        if key in ipc:
            _integer(ipc[key], "ipc." + key)
    _scenario(cfg["scenario"])
    return cfg
