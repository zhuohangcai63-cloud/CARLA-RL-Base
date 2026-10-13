"""Explicitly opted-in Scenic hero/ground regression on Town12 and Town13.

Requires a dedicated server. EASYCARLA_REAL_PORT does not enable these expensive
map-changing checks; use EASYCARLA_LARGE_MAP_PORT instead. No automatic restart.
"""
from copy import deepcopy
import json
import os
from pathlib import Path
import time
import traceback

import pytest


pytestmark = [pytest.mark.carla, pytest.mark.skipif(
    not os.environ.get("EASYCARLA_LARGE_MAP_PORT"),
    reason="Set EASYCARLA_LARGE_MAP_PORT for dedicated Town12/Town13 integration")]


def write_report(town, record):
    directory = os.environ.get("EASYCARLA_LARGE_MAP_REPORT_DIR")
    if directory:
        path = Path(directory)
        path.mkdir(parents=True, exist_ok=True)
        target = path / f"scenic-large-map-{town}.json"
        temporary = target.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(record, indent=2, allow_nan=False) + "\n")
        temporary.replace(target)


@pytest.mark.parametrize("town", ["Town12", "Town13"])
def test_scenic_hero_keeps_large_map_ground_loaded(town):
    from easycarla.errors import EnvironmentFault
    from easycarla.runtime import EnvironmentRuntime

    port = int(os.environ["EASYCARLA_LARGE_MAP_PORT"])
    overrides = {name: {"tile_stream_distance": 650, "actor_active_distance": 650}
                 for name in ("Town12", "Town13")}
    cfg = {"simulation": {"port": port, "tm_port": port + 6000, "timeout": 90,
                          "spectator_as_ego": False, "no_rendering_mode": True,
                          "map_layers": "all", "tile_stream_distance": 3000,
                          "actor_active_distance": 2000, "map_overrides": overrides},
           "scenario": {"source": "scenic", "town": town,
                        "file": str(Path(__file__).resolve().parents[1] / "scenarios/scenic/interaction_follow.scenic"),
                        "reactive_npcs": [],
                        "params": {"number_of_vehicles": 0, "number_of_walkers": 0}},
           "task": {"max_seconds": 10},
           "observations": {"rgb": False, "bev": False, "lidar": False}}
    runtime = EnvironmentRuntime(cfg)
    result = {"backend": "carla", "town": town, "seed": 7, "status": "running",
              "scope": "Scenic native hero/tile-ground/clock/owned-cleanup regression; no NPC/scenario benchmark",
              "config": deepcopy(cfg), "samples": [], "phase": "reset"}
    session = world = None
    failure = None
    started = time.monotonic()
    try:
        write_report(town, result)
        obs, info = runtime.reset(seed=7)
        session, world = runtime.session, runtime.session.world
        result.update(initialization_seconds=time.monotonic() - started,
                      actual_map=session.map.name, scenario=info["scenario"],
                      effective_world_settings=info["effective_world_settings"],
                      applied_map_override=info["applied_map_override"],
                      initialization_tick_counts=info["tick_counts"],
                      ego_actor_id=runtime.episode.ego.id,
                      ego_role_name=runtime.episode.ego.attributes["role_name"])
        assert result["actual_map"].rsplit("/", 1)[-1] == town
        assert result["ego_role_name"] == "hero"
        assert session.registry.roles[runtime.episode.ego.id] == "scenic/ego/1"
        assert info["applied_map_override"] == overrides[town]
        assert info["effective_world_settings"]["spectator_as_ego"] is False
        assert info["effective_world_settings"]["no_rendering_mode"] is True
        for field in ("tile_stream_distance", "actor_active_distance"):
            assert info["effective_world_settings"][field] == 650
        assert runtime.config["simulation"]["tile_stream_distance"] == 3000
        assert runtime.config["simulation"]["actor_active_distance"] == 2000
        assert runtime.episode.controllers == {}
        assert runtime.episode.vehicles == []
        result["phase"] = "idle"
        write_report(town, result)
        frame = int(obs["frame"])
        time.sleep(.2)
        assert world.get_snapshot().frame == frame
        result["idle_frame_stable"] = True
        result["phase"] = "steps"
        for decision in range(15):
            write_report(town, result)
            obs, reward, terminated, truncated, info = runtime.step([0, 0, 1])
            assert int(obs["frame"]) == frame + 4
            assert info["executed_seconds"] == pytest.approx(.2)
            assert not terminated and not truncated
            actor = runtime.episode.ego
            assert actor.is_alive
            location = actor.get_location()
            waypoint = session.map.get_waypoint(location)
            assert waypoint is not None
            height_error = abs(location.z - waypoint.transform.location.z)
            result["samples"].append({"decision": decision + 1, "frame": int(obs["frame"]),
                "ego_world": [location.x, location.y, location.z],
                "road_height": waypoint.transform.location.z, "height_error": height_error,
                "reward": float(reward), "terminated": bool(terminated), "truncated": bool(truncated),
                "executed_seconds": info["executed_seconds"], "timing": info["timing"]})
            assert height_error < 2, "Hero fell below the loaded road or map ground did not load"
            frame = int(obs["frame"])
        result["phase"] = "close_episode"
        write_report(town, result)
        runtime.close_episode()
        assert not session.registry.actors
        assert world.get_snapshot().frame == frame
        result["owned_cleanup"] = "registry empty after acknowledged destruction; no cleanup tick"
        result["status"] = "execution_passed"
    except Exception as exc:
        failure = exc
        result.update(status="failed", error=f"{type(exc).__name__}: {exc}",
                      failure_kind="environment_fault" if isinstance(exc, EnvironmentFault) else "assertion_or_setup",
                      runtime_state=runtime.state, traceback=traceback.format_exc())
        # Native/sensor failures remain exceptions and never become normal transitions.
        raise
    finally:
        try:
            result["phase_before_shutdown"] = result["phase"]
            result["phase"] = "shutdown"
            try:
                write_report(town, result)
            except OSError as report_error:
                # A failed report sink must not prevent world/resource cleanup.
                result["report_error"] = str(report_error)
            runtime.shutdown()
            result["settings_restored"] = True
            if session is not None:
                assert not session.registry.actors
                original = session._original_settings
                actual = world.get_settings()
                for key in ("synchronous_mode", "fixed_delta_seconds", "no_rendering_mode", "spectator_as_ego",
                            "tile_stream_distance", "actor_active_distance"):
                    assert getattr(actual, key) == getattr(original, key), f"Original {key} was not restored"
                if not actual.synchronous_mode:
                    # This observes the restored asynchronous clock after the
                    # episode has closed; it never issues a physics tick.
                    before = world.get_snapshot().frame
                    after = world.wait_for_tick(5).frame
                    result["restored_async_clock"] = {"before_frame": before, "after_frame": after,
                                                      "operation": "read_only_wait_for_tick"}
                    assert after > before, "Restored asynchronous world did not resume its clock"
            if failure is None:
                result["status"] = "passed"
        except Exception as cleanup:
            result.update(status="failed", settings_restored=False,
                          cleanup_error=f"{type(cleanup).__name__}: {cleanup}")
            if failure is None:
                raise
        finally:
            result["wall_seconds"] = time.monotonic() - started
            write_report(town, result)
