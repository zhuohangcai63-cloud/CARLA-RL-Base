"""Opt-in real CARLA checks. Never substituted with the fake IPC backend."""
import json
import os
from pathlib import Path
import time
import numpy as np
import pytest

pytestmark = [pytest.mark.carla, pytest.mark.skipif(not os.environ.get("EASYCARLA_REAL_PORT"),
                                                 reason="Set EASYCARLA_REAL_PORT for a dedicated running CARLA")]


def config():
    port = int(os.environ["EASYCARLA_REAL_PORT"])
    return {"simulation": {"port": port, "tm_port": port + 6000, "timeout": 60},
            "scenario": {"source": "scenic", "town": "Town10HD", "file": "scenarios/scenic/interaction_follow.scenic"},
            "experiments": {"enabled": True}}


def report(name, value):
    directory = os.environ.get("EASYCARLA_REAL_REPORT_DIR")
    if directory:
        path = Path(directory)
        path.mkdir(parents=True, exist_ok=True)
        (path / (name + ".json")).write_text(json.dumps(value, indent=2, default=str))


def test_real_sensor_alignment_idle_and_final_transition():
    from easycarla.runtime import EnvironmentRuntime
    cfg = config()
    cfg["observations"] = {"rgb": True, "bev": True, "lidar": True}
    cfg["task"] = {"max_seconds": 0.6}
    runtime = EnvironmentRuntime(cfg)
    frames = []
    try:
        obs, _ = runtime.reset(seed=7)
        assert set(obs["sensors"]) == {"rgb", "bev", "lidar"}
        frame = obs["frame"]
        time.sleep(.2)
        assert runtime.session.world.get_snapshot().frame == frame
        for i in range(3):
            obs, reward, terminated, truncated, info = runtime.step([0, .2, 0])
            assert obs["frame"] == frame + 4
            assert all(packet["frame"] == obs["frame"] for packet in obs["sensors"].values())
            frames.append({"frame": obs["frame"], "sensor_frames": {k: v["frame"] for k, v in obs["sensors"].items()},
                           "timing": info["timing"]})
            frame = obs["frame"]
        assert truncated and not terminated
        with pytest.raises(RuntimeError):
            runtime.step([0, 0, 0])
        assert runtime.session.world.get_snapshot().frame == frame
        report("carla-sensors", {"backend": "carla", "passed": True, "frames": frames})
    finally:
        runtime.shutdown()


def test_real_external_trajectory_tracking_and_braking():
    from easycarla.runtime import EnvironmentRuntime
    runtime = EnvironmentRuntime(config())
    result = {"backend": "carla", "passed": False, "samples": []}
    try:
        obs, _ = runtime.reset(seed=7)
        initial = obs["ego"]["position_world"].copy()
        for _ in range(15):
            frame = obs["frame"]
            trajectory = {"reference_frame": frame, "times": [0, .5, 1.0],
                          "points": [[0, 0, 0, 2], [1, 0, 0, 2], [2, 0, 0, 2]]}
            obs, _, terminated, truncated, info = runtime.step_trajectory(trajectory, .2)
            assert not terminated and not truncated
            assert obs["frame"] == frame + 4
            assert info["executed_seconds"] == .2
            result["samples"].append({"frame": obs["frame"], "speed": obs["ego"]["speed"]})
        assert obs["ego"]["speed"] > 1
        assert np.linalg.norm(obs["ego"]["position_world"] - initial) > 1
        frame = obs["frame"]
        with pytest.raises(ValueError, match="stale"):
            runtime.step_trajectory(trajectory, .2)
        assert runtime.session.world.get_snapshot().frame == frame
        for _ in range(15):
            obs, _, terminated, truncated, _ = runtime.step_trajectory(
                {"reference_frame": obs["frame"], "times": [0, 1],
                 "points": [[0, 0, 0, 0], [0, 0, 0, 0]]}, .2)
            assert not terminated and not truncated
        assert obs["ego"]["speed"] < .2
        result.update(passed=True, final_speed=obs["ego"]["speed"], final_frame=obs["frame"])
    finally:
        report("carla-trajectory", result)
        runtime.shutdown()


@pytest.mark.parametrize("scenario", ["pedestrian_crossing", "hard_break_route", "parking_cut_in", "vehicle_opens_door", "construction_obstacle"])
def test_real_scenic_event_episodes(scenario):
    from easycarla.runtime import EnvironmentRuntime
    from easycarla.control import PathTracker
    from easycarla.observations import actor_state
    from easycarla.observations.geometry import xyz
    cfg = config()
    cfg["scenario"]["file"] = f"scenarios/scenic/{scenario}.scenic"
    cfg["scenario"]["params"] = {"number_of_vehicles": 0, "number_of_walkers": 0, "traffic_light_policy": "off"}
    if scenario == "pedestrian_crossing":
        cfg["scenario"]["params"].update(forced_crossing_trigger_prob=1.0, forced_crossing_warmup_steps=0,
                                           forced_crossing_cooldown_steps=10, forced_crossing_min_speed=0,
                                           forced_crossing_max_total=2)
    runtime = EnvironmentRuntime(cfg)
    results = []
    try:
        for seed in (42, 43):
            obs, reset_info = runtime.reset(seed=seed)
            tracker = PathTracker([xyz(t.location) for t in runtime.episode.route])
            times = []
            for step in range(120):
                action = tracker.control(actor_state(runtime.episode.ego), 8.0, .2)
                obs, reward, terminated, truncated, info = runtime.step(action)
                times.append(info["timing"])
                if terminated or truncated:
                    break
            result = {"source": "scenic", "scenario": scenario, "seed": seed, "steps": step+1,
                      "terminated": terminated, "truncated": truncated, "reason": info["termination_reason"],
                      "events": info["events"], "reset_events": reset_info["events"],
                      "simulation_ms_p50": float(np.median([t["simulation_seconds"]*1000 for t in times])),
                      "environment_ms_p50": float(np.median([t["environment_seconds"]*1000 for t in times]))}
            results.append(result)
        report("carla-"+scenario, {"backend": "carla", "execution_passed": True, "episodes": results})
    except Exception as exc:
        report("carla-"+scenario, {"backend": "carla", "execution_passed": False, "episodes": results, "error": str(exc)})
        raise
    finally:
        runtime.shutdown()


def test_real_two_npc_prefix_reconstruction():
    from easycarla.runtime import EnvironmentRuntime
    cfg = config()
    cfg["scenario"]["reactive_npcs"] = [dict(role="near", distance=22, target_speed=5, mechanism="C"),
                                           dict(role="far", distance=40, target_speed=4, mechanism="C")]
    runtime = EnvironmentRuntime(cfg)
    try:
        runtime.reset(seed=7)
        runtime.experiment("begin_prefix")
        for _ in range(5):
            runtime.step([0, .4, 0])
        prefix = runtime.experiment("finish_prefix")
        tolerances = dict(position=.2, velocity=.2, yaw=.03, actor_state=.2,
                          controllers=1e-5, history=.2, events=1e-5, signals=1e-5, task=.01)
        replay = runtime.experiment("replay_prefix", prefix=prefix, tolerances=tolerances)
        report("carla-prefix", {"backend": "carla", "passed": replay["report"]["comparable"], "report": replay["report"], "tolerances": tolerances})
        assert replay["report"]["comparable"], replay["report"]
    finally:
        runtime.shutdown()


def test_real_two_npc_mechanism_switches():
    """Validate real control hand-offs independently of replay repeatability."""
    from easycarla.runtime import EnvironmentRuntime
    from easycarla.observations import actor_state
    cfg = config()
    cfg["scenario"]["reactive_npcs"] = [dict(role="near", distance=22, target_speed=6, mechanism="C"),
                                        dict(role="far", distance=40, target_speed=2, mechanism="C")]
    runtime = EnvironmentRuntime(cfg)
    results = {"backend": "carla", "passed": False, "frames": [],
               "scope": "live mechanism switch, controller continuity, exclusive writer and actual actor control; no replay claim"}
    try:
        runtime.reset(seed=7)
        for _ in range(10):
            runtime.step([0, .2, 0])
        before = {role: controller.state_dict() for role, (_, controller) in runtime.episode.controllers.items()}
        runtime.experiment("set_mechanism", role="near", mechanism="L")
        runtime.experiment("set_mechanism", role="far", mechanism="G")
        after = {role: controller.state_dict() for role, (_, controller) in runtime.episode.controllers.items()}
        for role in ("near", "far"):
            assert before[role]["tracker"] == after[role]["tracker"]
            assert before[role]["history"] == after[role]["history"]
            assert before[role]["time"] == after[role]["time"]
        for _ in range(25):
            _, _, terminated, truncated, _ = runtime.step([0, .2, 0])
            sample = {"frame": runtime.snapshot.frame, "physical_ticks": runtime.physical_ticks, "actors": {}}
            for role, (actor, controller) in runtime.episode.controllers.items():
                assert runtime.arbiter.owner(actor.id) == role
                assert controller.last_tick == runtime.physical_ticks - 1
                sample["actors"][role] = {"state": actor_state(actor), "mechanism": controller.mechanism_name,
                                          "mode": controller.mode, "last_tick": controller.last_tick}
            results["frames"].append(sample)
            if terminated or truncated:
                break
        assert results["frames"]
        assert runtime.episode.controllers["near"][1].mechanism_name == "L"
        assert runtime.episode.controllers["far"][1].mechanism_name == "G"
        results["trigger_counts"] = {role: controller.trigger_counts
                                     for role, (_, controller) in runtime.episode.controllers.items()}
        results["mechanism_activation_note"] = "Preset changes may remain untriggered when this follow geometry has no applicable crossing/gap event."
        results["passed"] = True
    except Exception as exc:
        results["error"] = str(exc)
        raise
    finally:
        report("carla-mechanism-switch", results)
        runtime.shutdown()


@pytest.mark.parametrize("kind", ["merge", "crossing"])
def test_real_reactive_route_execution(kind):
    import yaml
    from easycarla.runtime import EnvironmentRuntime
    from easycarla.control import PathTracker
    from easycarla.observations import actor_state
    from easycarla.observations.geometry import xyz
    cfg = yaml.safe_load(Path(f"configs/interaction_{kind}.yaml").read_text())
    cfg["simulation"] = config()["simulation"]
    runtime = EnvironmentRuntime(cfg)
    try:
        obs, info = runtime.reset(seed=7)
        assert len(runtime.episode.controllers) == 2
        tracker = PathTracker([xyz(t.location) for t in runtime.episode.route])
        for step in range(80):
            obs, reward, terminated, truncated, info = runtime.step(tracker.control(actor_state(runtime.episode.ego), 6, .2))
            if terminated or truncated:
                break
        states = {role: ctrl.state_dict() for role, (_, ctrl) in runtime.episode.controllers.items()}
        assert all(ctrl.last_tick == runtime.physical_ticks - 1 for _, ctrl in runtime.episode.controllers.values())
        report("carla-reactive-" + kind, {"backend": "carla", "execution_passed": True,
               "seed": 7, "steps": step+1, "terminated": terminated, "truncated": truncated,
               "reason": info["termination_reason"], "controllers": states, "capabilities": runtime.capabilities()})
    finally:
        runtime.shutdown()
