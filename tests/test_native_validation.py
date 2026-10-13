"""Native validation report failures, without importing a live simulator client."""
import importlib
import json
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace

import pytest

from easycarla.errors import EnvironmentFault, WorldInitializationTimeout


ROOT = Path(__file__).resolve().parents[1]


def manifest_file(tmp_path, towns):
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps({"split": "dev", "entries": [
        {"route_id": str(index), "town": town, "scenario_types": ["TestScenario"]}
        for index, town in enumerate(towns, start=1)
    ]}))
    return path


@pytest.mark.parametrize("load_times_out, expected_status, expected_execution", [
    (False, "failed_cleanup", "completed"),
    (True, "stopped_unknown_outcome", "unknown_outcome"),
])
@pytest.mark.parametrize("spectator_as_ego", [False, True])
@pytest.mark.parametrize("reload_args, expected_reload", [([], True), (["--reload"], True), (["--no-reload"], False)])
def test_route_cli_fails_final_cleanup_and_preserves_prior_stop(
        tmp_path, monkeypatch, load_times_out, expected_status, expected_execution, spectator_as_ego,
        reload_args, expected_reload):
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    validator = importlib.import_module("validate_native_routes")
    manifest = manifest_file(tmp_path, ["Town01"])
    output = tmp_path / "routes.json"
    calls = []
    session_configs = []

    class Session:
        def __init__(self, config):
            session_configs.append(config)
            self.frame = 10
            self.map = SimpleNamespace(name="Town01")
            self.world = SimpleNamespace(
                get_snapshot=lambda: SimpleNamespace(frame=self.frame), get_actors=lambda: [])
            self.registry = SimpleNamespace(actors={})
            self.controls = SimpleNamespace(handoff=lambda *a: None, apply_control=lambda *a, **kw: None)
            self.ticks = {"step": 0}
            self.timings = {"simulation_seconds": 0.0}

        def load(self, town, seed):
            if load_times_out:
                raise TimeoutError("load outcome is unknown")

        def tick(self):
            self.frame += 1
            self.ticks["step"] += 1
            return self.world.get_snapshot()

        def close(self):
            calls.append("session.close")
            raise RuntimeError("settings restoration failed")

    episode = SimpleNamespace(
        ego=object(), metadata={}, route=[], capabilities=lambda: {},
        provider=SimpleNamespace(get_global_route_planner=lambda: object()),
        before_tick=lambda: None, after_tick=lambda snapshot: None,
        resolve_control=lambda values: SimpleNamespace(values=values, allow_combined=False),
        info=lambda: {"native_done": True, "native_success": True, "native_scenarios": []},
        close=lambda: calls.append("episode.close"))
    adapter = SimpleNamespace(town=lambda seed, options: "Town01", prepare=lambda *a: episode)
    driver = SimpleNamespace(set_global_plan=lambda plan: None, done=lambda: True)
    modules = {name: ModuleType(name) for name in (
        "agents", "agents.navigation", "agents.navigation.behavior_agent",
        "agents.navigation.local_planner", "carla")}
    modules["agents.navigation.behavior_agent"].BehaviorAgent = lambda *a, **kw: driver
    modules["agents.navigation.local_planner"].RoadOption = SimpleNamespace(LANEFOLLOW=object())
    modules["carla"].VehicleControl = lambda brake: SimpleNamespace(steer=0, throttle=0, brake=brake)
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.setattr(validator, "WorldSession", Session)
    monkeypatch.setattr(validator, "Bench2DriveAdapter", lambda config: adapter)
    monkeypatch.setattr(validator, "wait_for_server", lambda *a: None)
    monkeypatch.setattr(sys, "argv", ["validate_native_routes", "--manifest", str(manifest),
                                    "--output", str(output), "--ticks", "1",
                                    "--spectator-as-ego" if spectator_as_ego else "--no-spectator-as-ego",
                                    *reload_args])

    with pytest.raises(SystemExit) as error:
        validator.main()

    assert error.value.code == 1
    report = json.loads(output.read_text())
    assert report["status"] == expected_status
    assert report["config"]["spectator_as_ego"] is spectator_as_ego
    assert report["config"]["reload_on_reset"] is expected_reload
    assert session_configs[0]["simulation"]["reload_on_reset"] is expected_reload
    assert report["session_close_error"] == "settings restoration failed"
    assert report["results"][0]["execution"] == expected_execution
    assert calls == (["session.close"] if load_times_out else ["episode.close", "session.close"])


@pytest.mark.parametrize("error_type, expected_status, expected_loads", [
    (WorldInitializationTimeout, "stopped_unknown_outcome", 1),
    (EnvironmentFault, "finished", 2),
])
def test_initialization_timeout_stops_before_next_route_without_reclassifying_scene_errors(
        tmp_path, monkeypatch, error_type, expected_status, expected_loads):
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    validator = importlib.import_module("validate_native_routes")
    manifest = manifest_file(tmp_path, ["Town12", "Town13"])
    output = tmp_path / "routes.json"
    calls = []

    class Session:
        def __init__(self, config):
            self.world = SimpleNamespace(get_actors=lambda: [])
            self.registry = SimpleNamespace(actors={})

        def load(self, town, seed):
            calls.append(("load", town))
            # No timeout substring: stopping must depend on the fault type.
            if error_type is WorldInitializationTimeout:
                raise error_type("native initialization is incomplete")
            self.map = SimpleNamespace(name=town)

        def close(self):
            calls.append(("close",))

    modules = {name: ModuleType(name) for name in (
        "agents", "agents.navigation", "agents.navigation.behavior_agent",
        "agents.navigation.local_planner", "carla")}
    modules["agents.navigation.behavior_agent"].BehaviorAgent = object
    modules["agents.navigation.local_planner"].RoadOption = object
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)
    def prepare(*args):
        raise EnvironmentFault("scenario construction failed")

    adapter = SimpleNamespace(
        town=lambda seed, options: {"1": "Town12", "2": "Town13"}[options["route_id"]],
        prepare=prepare)
    monkeypatch.setattr(validator, "WorldSession", Session)
    monkeypatch.setattr(validator, "Bench2DriveAdapter", lambda config: adapter)
    monkeypatch.setattr(validator, "wait_for_server", lambda *a: None)
    monkeypatch.setattr(sys, "argv", ["validate_native_routes", "--manifest", str(manifest),
                                    "--output", str(output), "--ticks", "1"])

    with pytest.raises(SystemExit) as error:
        validator.main()

    assert error.value.code == 1
    report = json.loads(output.read_text())
    assert report["status"] == expected_status
    assert calls == [("load", town) for town in ["Town12", "Town13"][:expected_loads]] + [("close",)]
    assert report["results"][0]["execution"] == ("unknown_outcome" if expected_loads == 1 else "failed")
    assert report["results"][0]["ticks"] == 0
    assert report["results"][1]["execution"] == ("not_run" if expected_loads == 1 else "failed")


@pytest.mark.parametrize("failure", ["legacy_cleanup_error", "native_abort"])
@pytest.mark.parametrize("reload_args, expected_reload", [([], True), (["--reload"], True), (["--no-reload"], False)])
def test_matrix_cli_stops_after_child_cleanup_failure_or_native_abort(
        tmp_path, monkeypatch, failure, reload_args, expected_reload):
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    matrix = importlib.import_module("validate_native_matrix")
    manifest = manifest_file(tmp_path, ["Town01", "Town02"])
    output = tmp_path / "matrix.json"
    calls = []
    if failure == "legacy_cleanup_error":
        child_report = {"status": "finished", "session_close_error": "settings restoration failed",
                        "results": [{"route_id": "1", "execution": "completed", "ticks": 17}]}
        exit_code, expected_child_status = 0, "failed_cleanup"
    else:
        child_report = {"status": "running", "results": [
            {"route_id": "1", "execution": "running", "ticks": 17}]}
        exit_code, expected_child_status = -6, "running"

    def child(command, **kwargs):
        calls.append(command)
        child_output = Path(command[command.index("--output") + 1])
        child_output.write_text(json.dumps(child_report))
        return SimpleNamespace(returncode=exit_code)

    monkeypatch.setattr(matrix.subprocess, "run", child)
    monkeypatch.setattr(sys, "argv", ["validate_native_matrix", "--manifest", str(manifest),
                                    "--output", str(output), "--ticks", "20", *reload_args])

    with pytest.raises(SystemExit) as error:
        matrix.main()

    assert error.value.code == 1
    assert len(calls) == 1
    assert "--no-spectator-as-ego" in calls[0]
    assert ("--reload" if expected_reload else "--no-reload") in calls[0]
    assert ("--no-reload" if expected_reload else "--reload") not in calls[0]
    report = json.loads(output.read_text())
    assert report["reload_on_reset"] is expected_reload
    assert report["status"] == "stopped_uncertain_child"
    assert report["children"][0]["status"] == expected_child_status
    assert report["children"][0]["exit_code"] == exit_code
    expected_execution = "unknown_outcome" if failure == "native_abort" else "completed"
    assert report["results"][0]["execution"] == expected_execution
    if failure == "native_abort":
        assert report["results"][0]["observed_child_execution"] == "running"
        assert report["results"][0]["child_exit_code"] == -6
        assert "no terminal operation result" in report["results"][0]["uncertainty_reason"]
    assert report["results"][0]["ticks"] == 17
    assert report["results"][1]["execution"] == "not_run"
    assert json.loads(Path(report["children"][0]["report"]).read_text()) == child_report
