"""Invalid numeric configuration is rejected before connecting to CARLA."""
from copy import deepcopy
from pathlib import Path
import sys

import pytest
import yaml

from easycarla.config import DEFAULTS, validate_config


def override(path, value):
    result = cursor = {}
    for key in path.split(".")[:-1]:
        cursor[key] = {}
        cursor = cursor[key]
    cursor[path.split(".")[-1]] = value
    return result


@pytest.mark.parametrize("path", ["simulation.physics_dt", "simulation.decision_dt", "simulation.history_dt",
                                  "simulation.timeout", "observations.sensor_timeout", "observations.range",
                                  "task.max_seconds", "task.goal_radius"])
@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf"), 0, -0.1, True, "0.05"])
def test_required_positive_numbers_reject_nonfinite_nonpositive_and_booleans(path, value):
    with pytest.raises(ValueError, match=path.replace(".", r"\.")):
        validate_config(override(path, value))


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf"), True, "1"])
def test_reward_weights_are_finite_numbers_including_extra_components(value):
    for key in ("progress", "custom"):
        with pytest.raises(ValueError, match="task.weights"):
            validate_config({"task": {"weights": {key: value}}})


@pytest.mark.parametrize("path", ["observations.max_objects", "observations.history_frames", "observations.route_points",
                                  "observations.max_signals", "observations.max_obstacles", "observations.image_width",
                                  "observations.image_height", "observations.bev_size", "observations.lidar_points",
                                  "experiments.max_prefix_steps", "experiments.max_prefix_bytes",
                                  "experiments.prefix_cache_bytes", "experiments.max_prefix_handles",
                                  "ipc.request_bytes", "ipc.response_bytes", "ipc.static_bytes",
                                  "simulation.port", "simulation.tm_port"])
@pytest.mark.parametrize("value", [True, 0, -1, 1.5, float("nan"), sys.maxsize + 1])
def test_capacities_and_ports_require_positive_bounded_integers(path, value):
    with pytest.raises(ValueError, match=path.replace(".", r"\.")):
        validate_config(override(path, value))


def test_extreme_derived_values_and_port_range_rejected_cleanly():
    with pytest.raises(ValueError, match="integer multiple"):
        validate_config({"simulation": {"physics_dt": 1e-320}})
    with pytest.raises(ValueError, match="capacity"):
        validate_config({"observations": {"image_width": sys.maxsize}})
    with pytest.raises(ValueError, match="simulation.port"):
        validate_config({"simulation": {"port": 65536}})
    with pytest.raises(ValueError, match="physics_dt"):
        validate_config({"simulation": {"physics_dt": 0.2}})
    with pytest.raises(ValueError, match="integer multiple"):
        validate_config({"simulation": {"decision_dt": 0.11}})


@pytest.mark.parametrize("value", [-0.1, .10001, float("nan"), float("inf"), True])
def test_collision_settle_window_bounds(value):
    with pytest.raises(ValueError, match="collision_settle_seconds"):
        validate_config({"observations": {"collision_settle_seconds": value}})


def test_valid_boundaries_and_zero_negative_rewards_are_preserved():
    config = {"simulation": {"physics_dt": .1, "history_dt": .1, "decision_dt": .2, "port": 1, "tm_port": 65535},
              "task": {"weights": {"progress": 0, "goal": -1}},
              "observations": {"collision_settle_seconds": 0},
              "scenario": {"max_active_events": 0}}
    validated = validate_config(config)
    assert validated["task"]["weights"]["progress"] == 0
    assert validated["task"]["weights"]["goal"] == -1
    assert validate_config({"observations": {"collision_settle_seconds": .1}})


@pytest.mark.parametrize("path", ["simulation.reload_on_reset", "simulation.spectator_as_ego", "observations.rgb", "observations.bev",
                                  "observations.lidar", "experiments.enabled"])
def test_flags_cannot_be_mistyped_numbers_or_strings(path):
    for value in (1, "false", None):
        with pytest.raises(ValueError, match=path.replace(".", r"\.")):
            validate_config(override(path, value))


@pytest.mark.parametrize("weights", [[0, 0], [-1, 2], [True, 1], [float("nan"), 1], [float("inf"), 1], [1e308, 1e308]])
def test_mixed_source_weights_have_a_positive_finite_sum(weights):
    config = {"scenario": {"source": "mixed", "sources": [
        {"source": "scenic", "weight": weights[0]}, {"source": "bench2drive", "weight": weights[1]}]}}
    with pytest.raises(ValueError, match="weight"):
        validate_config(config)


def test_scenario_optional_bounds_and_nested_fallback_are_validated():
    for setting in ({"route_attempts": True}, {"max_iterations": 0}, {"max_active_events": -1},
                    {"source": "bench2drive", "metric_history_ticks": 4097},
                    {"source": "bench2drive", "bench2drive": {"metric_history_ticks": True}},
                    {"reactive_npcs": [{"target_speed": float("nan")}]},
                    {"fallback": {"source": "mixed", "sources": []}}):
        with pytest.raises(ValueError):
            validate_config({"scenario": setting})
    assert validate_config({"scenario": {"source": "mixed", "sources": [
        {"source": "scenic", "weight": 0}, {"source": "bench2drive", "weight": 1}]}})


@pytest.mark.parametrize("config", [None, [], {"task": None}, {"task": {"weights": []}},
                                    {"simulation": "bad"}, {"observations": False},
                                    {"scenario": []}, {"experiments": None}, {"ipc": []},
                                    {"backend": "unknown"}, {"task": {"profile": "unknown"}}])
def test_bad_sections_fail_as_configuration_errors(config):
    with pytest.raises(ValueError):
        validate_config(config)


def test_checked_in_configs_stay_valid_and_input_is_not_mutated():
    for path in Path("configs").glob("*.yaml"):
        original = yaml.safe_load(path.read_text())
        before = deepcopy(original)
        validate_config(original)
        assert original == before
    snapshot = deepcopy(DEFAULTS)
    validate_config({"task": {"weights": {"progress": 2.0}}})
    assert DEFAULTS == snapshot


def test_runtime_rejects_config_without_constructing_a_world(monkeypatch):
    from easycarla import runtime
    calls = []
    monkeypatch.setattr(runtime, "WorldSession", lambda config: calls.append(config))
    with pytest.raises(ValueError, match="task.weights.progress"):
        runtime.EnvironmentRuntime({"task": {"weights": {"progress": float("nan")}}})
    assert calls == []


@pytest.mark.parametrize("settings", [
    {"tile_stream_distance": float("nan")}, {"actor_active_distance": 0},
    {"tile_stream_distance": 500, "actor_active_distance": 501},
    {"no_rendering_mode": "true"}, {"map_layers": "automatic"},
])
def test_invalid_world_modes_rejected(settings):
    with pytest.raises(ValueError):
        validate_config({"simulation": settings})


def test_no_rendering_rejects_camera_and_reduced_streaming_is_explicit():
    with pytest.raises(ValueError, match="require no_rendering"):
        validate_config({"simulation": {"no_rendering_mode": True}, "observations": {"rgb": True}})
    cfg = validate_config({"simulation": {"tile_stream_distance": 500, "actor_active_distance": 500}})
    assert cfg["simulation"]["tile_stream_distance"] == 500
    assert validate_config({})["simulation"]["tile_stream_distance"] is None


@pytest.mark.parametrize("overrides", [
    None, [], {"Town13": {}}, {"Town13": {"map_layers": "none"}},
    {"Town*": {"tile_stream_distance": 500}}, {"/Game/Town13": {"tile_stream_distance": 500}},
    {13: {"tile_stream_distance": 500}}, {"..": {"tile_stream_distance": 500}},
    {"Town13": {"tile_stream_distance": None}}, {"Town13": {"tile_stream_distance": True}},
    {"Town13": {"tile_stream_distance": float("nan")}},
    {"Town13": {"actor_active_distance": -1}}, {"Town13": {"actor_active_distance": 0}},
    {"Town13": {"tile_stream_distance": 500, "actor_active_distance": 600}},
])
def test_invalid_map_overrides_are_rejected(overrides):
    with pytest.raises(ValueError, match="map_overrides"):
        validate_config({"simulation": {"map_overrides": overrides}})


def test_map_override_cross_validation_includes_base_values():
    with pytest.raises(ValueError, match="after merging base"):
        validate_config({"simulation": {"actor_active_distance": 2000,
                        "map_overrides": {"Town13": {"tile_stream_distance": 650}}}})
    original = {"simulation": {"tile_stream_distance": 3000, "actor_active_distance": 2000,
                              "map_overrides": {"Town13": {"actor_active_distance": 650}}}}
    before = deepcopy(original)
    result = validate_config(original)
    assert original == before
    assert result["simulation"]["actor_active_distance"] == 2000
    assert validate_config({})["simulation"]["map_overrides"] == {}
