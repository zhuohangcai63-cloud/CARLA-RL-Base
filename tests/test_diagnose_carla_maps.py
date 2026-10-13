"""Native-map probe contracts without CARLA or any scenario adapter imports."""
from copy import deepcopy
import hashlib
import importlib.util
import json
from pathlib import Path
import queue
from types import SimpleNamespace as NS

import numpy as np
import pytest

spec = importlib.util.spec_from_file_location("map_probe", Path(__file__).parents[1] / "scripts/diagnose_carla_maps.py")
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


def args(tmp_path, *extra):
    return probe.parse_args(["--port", "29997", "--output", str(tmp_path / "report.json"), *extra])


@pytest.mark.parametrize("extra", [[], ["--town", "Town13", "--distance", "nan"],
    ["--town", "Town13", "--camera", "--no-rendering"], ["--town", "Town13", "--ticks", "0"],
    ["--town", "Town13", "--spawn", "1,2,inf"], ["--route-id", "7"]])
def test_invalid_cli_fails_before_connecting(tmp_path, extra):
    with pytest.raises(SystemExit):
        args(tmp_path, *extra)


def test_multiple_towns_and_explicit_boolean_modes(tmp_path):
    config = args(tmp_path, "--town", "Town12", "Town13", "--town", "Town01",
                  "--spectator-as-ego", "--spawn", "1,2,3,90")
    assert config.town == ["Town12", "Town13", "Town01"]
    assert config.spectator_as_ego is True and config.spawn == [1., 2., 3., 90.]
    assert args(tmp_path, "--town", "Town13", "--no-spectator-as-ego").spectator_as_ego is False


def test_route_start_verifies_file_and_town_without_loading_upstream_code(tmp_path):
    source = tmp_path / "route.xml"
    source.write_text('<routes><route id="7" town="Town13"><waypoints>'
                      '<position x="1" y="2" z="3"/><position x="1" y="3" z="3"/>'
                      '</waypoints></route></routes>')
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"entries": [{"route_id": "7", "town": "Town13", "file": "route.xml",
                                                   "file_sha256": hashlib.sha256(source.read_bytes()).hexdigest()}]}))
    config = args(tmp_path, "--route-manifest", str(manifest), "--route-id", "7", "--route-root", str(tmp_path))
    spawn, record = probe.route_start(config)
    assert spawn == [1., 2., 3.5, 90.]
    assert record["town"] == "Town13"
    config.town = ["Town12"]
    with pytest.raises(ValueError, match="different town"):
        probe.route_start(config)
    source.write_text("changed")
    with pytest.raises(ValueError, match="digest"):
        probe.route_start(config)


def test_lease_excludes_other_owner_and_retains_stable_file(tmp_path, monkeypatch):
    monkeypatch.setattr(probe, "Path", lambda _: tmp_path)
    with probe.world_lease(29997):
        with pytest.raises(RuntimeError, match="world owner"):
            with probe.world_lease(29997):
                pytest.fail("conflicting lease was allowed")
    with probe.world_lease(29997):
        pass
    assert (tmp_path / "world-port-29997.lock").is_file()


def image(frame, uniform=False):
    pixels = np.zeros((180, 320, 4), dtype=np.uint8)
    if not uniform:
        pixels[:, 160:, :3] = 255
    return NS(frame=frame, width=320, height=180, raw_data=pixels.tobytes())


def test_camera_requires_exact_nonempty_frame_without_extra_ticks():
    images = queue.Queue()
    images.put(image(3))
    images.put(image(4))
    assert probe.wait_camera(images, 4, .01)["std"] == 127.5
    images.put(image(6))
    with pytest.raises(RuntimeError, match="skipped required"):
        probe.wait_camera(images, 5, .01)
    with pytest.raises(TimeoutError, match="physical frame"):
        probe.wait_camera(images, 5, .001)
    with pytest.raises(RuntimeError, match="uniform/empty"):
        probe.camera_stats(image(1, uniform=True), 1)
    solid = image(1)
    pixels = np.zeros((180, 320, 4), dtype=np.uint8)
    pixels[:, :, 1] = 255  # Distinct RGB channels still form a spatially solid frame.
    solid.raw_data = pixels.tobytes()
    with pytest.raises(RuntimeError, match="uniform/empty"):
        probe.camera_stats(solid, 1)
    broken = image(1)
    broken.raw_data = b"bad"
    with pytest.raises(RuntimeError, match="byte count"):
        probe.camera_stats(broken, 1)


def test_readiness_uses_fresh_clients_and_read_only_attempts(monkeypatch):
    created, timeouts = [], []
    world = object()
    class Client:
        def __init__(self, host, port):
            self.attempt = len(created)
            created.append(self)
        def set_timeout(self, timeout):
            timeouts.append(timeout)
        def get_server_version(self):
            return "0.9.15"
        def get_world(self):
            if self.attempt < 2:
                raise RuntimeError("startup connection not ready")
            return world
    monkeypatch.setattr(probe.time, "sleep", lambda seconds: None)
    record = {}
    client, result = probe.wait_server(NS(Client=Client), "localhost", 29997, 5., record)
    assert len(created) == 3 and client is created[-1] and result is world
    assert record["attempts"] == 3 and record["server_version"] == "0.9.15"
    assert all(0 < value <= 1 for value in timeouts)
    assert "startup" in record["last_error"]


class Location:
    def __init__(self, x=0, y=0, z=0):
        self.x, self.y, self.z = x, y, z

    def __add__(self, other):
        return Location(self.x + other.x, self.y + other.y, self.z + other.z)


def fake_carla(fail_tick=False, fail_destroy=False, fail_stop=False):
    state = {"ticks": 0, "destroyed": [], "settings": [], "world": None,
             "blueprint_attributes": {}, "camera_callback": None}
    class World:
        def __init__(self, town, episode_id):
            self.town, self.id, self.frame = town, episode_id, 0
            self.settings = NS(synchronous_mode=False, fixed_delta_seconds=None, no_rendering_mode=False,
                               spectator_as_ego=True, tile_stream_distance=3000., actor_active_distance=2000.)

        def get_settings(self):
            return deepcopy(self.settings)

        def apply_settings(self, settings):
            self.settings = deepcopy(settings)
            state["settings"].append(deepcopy(settings))
            self.frame += 1  # Model native settings' hidden initialization tick.

        def get_snapshot(self):
            return NS(frame=self.frame, timestamp=NS(elapsed_seconds=self.frame * .05))

        def get_map(self):
            return NS(name=self.town, to_opendrive=lambda: "map", get_spawn_points=lambda: [
                NS(location=Location(), rotation=NS(yaw=0))])

        def get_blueprint_library(self):
            def find(name):
                attributes = state["blueprint_attributes"].setdefault(name, {})
                return NS(id=name, set_attribute=lambda key, value: attributes.update({key: value}))
            return NS(find=find)

        def spawn_actor(self, *args, **kwargs):
            if args[0].id == "sensor.camera.rgb":
                return NS(id=18, type_id="sensor.camera.rgb", is_alive=True, stop=lambda: None,
                          listen=lambda callback: state.update(camera_callback=callback),
                          destroy=lambda: state["destroyed"].append(18) or True)
            def stop():
                raise RuntimeError("sensor stop failed")
            return NS(id=17, type_id="sensor.test" if fail_stop else "vehicle.test", is_alive=True,
                      destroy=lambda: state["destroyed"].append(17) or not fail_destroy,
                      stop=stop)

        def get_spectator(self):
            return NS(set_transform=lambda value: None)

        def tick(self, timeout):
            state["ticks"] += 1
            self.frame += 1
            if fail_tick:
                raise TimeoutError("unknown native tick outcome")
            if state["camera_callback"] is not None:
                state["camera_callback"](image(self.frame))
            return self.frame

    class Client:
        def __init__(self, host, port):
            state["world"] = World("Town01", 1)

        def set_timeout(self, timeout): pass
        def get_world(self): return state["world"]
        def get_server_version(self): return "fake"
        def get_client_version(self): return "fake"
        def get_available_maps(self): return ["/Game/Maps/Town13"]
        def apply_batch_sync(self, commands, do_tick):
            assert do_tick is False
            return [NS(error="")]

        def load_world(self, name, **kwargs):
            state["world"] = World(name, 2)
            return state["world"]

    api = NS(Client=Client, MapLayer=NS(All="all"), Location=Location,
             Rotation=lambda **kwargs: NS(**kwargs), Transform=lambda *values: values,
             VehicleControl=lambda **kwargs: kwargs,
             command=NS(ApplyVehicleControl=lambda *values: values))
    return api, state


@pytest.mark.parametrize("fail_tick", [False, True])
def test_probe_reports_native_frames_no_replay_and_restores_original_settings(tmp_path, fail_tick):
    config = args(tmp_path, "--town", "Town13", "--ticks", "2", "--repeats", "1",
                  "--idle-seconds", ".001", "--no-rendering")
    carla, state = fake_carla(fail_tick)
    result = probe.run(config, carla)
    report = json.loads(config.output.read_text())
    assert result == int(fail_tick)
    assert state["ticks"] == (1 if fail_tick else 2)
    assert state["destroyed"] == [17]
    assert state["settings"][0].spectator_as_ego is False
    assert state["world"].settings.spectator_as_ego is True
    assert state["world"].settings.no_rendering_mode is False
    record = report["episodes"][0]
    assert record["api_phases"][0]["same_episode_frame_delta"] == 1
    assert record["load_frame_change"]["same_episode_frame_delta"] is None
    assert record["settings"]["spectator_as_ego"] is False
    assert report["cleanup"][0]["status"] == "passed"
    if fail_tick:
        assert record["stage"] == "control_and_tick"
        assert "never replayed" in record["outcome"]


def test_rgb_probe_configures_every_physical_tick_instead_of_sensor_timer(tmp_path):
    config = args(tmp_path, "--town", "Town13", "--ticks", "2", "--repeats", "1",
                  "--idle-seconds", ".001", "--camera")
    carla, state = fake_carla()
    assert probe.run(config, carla) == 0
    assert state["blueprint_attributes"]["sensor.camera.rgb"]["sensor_tick"] == "0.0"
    record = json.loads(config.output.read_text())["episodes"][0]
    assert record["camera_config"]["capture_schedule"] == "every_physical_tick"
    assert len(record["camera_frames"]) == state["ticks"] == 2
    assert record["camera_frames"][1]["frame"] == record["camera_frames"][0]["frame"] + 1
    assert state["destroyed"] == [18, 17]


def test_report_disk_failure_cannot_skip_owned_cleanup_or_settings_restore(tmp_path, monkeypatch):
    config = args(tmp_path, "--town", "Town13", "--ticks", "2", "--repeats", "1", "--idle-seconds", ".001")
    carla, state = fake_carla()
    original_save = probe.save
    failed = []
    def save(path, report):
        if report["episodes"] and report["episodes"][-1].get("stage") == "control_and_tick":
            failed.append(True)
        if failed:
            raise OSError("disk is full")
        original_save(path, report)
    monkeypatch.setattr(probe, "save", save)
    assert probe.run(config, carla) == 1
    assert state["destroyed"] == [17]
    assert state["world"].settings.synchronous_mode is False
    assert state["world"].settings.spectator_as_ego is True


@pytest.mark.parametrize("failure", ["fail_destroy", "fail_stop"])
def test_actor_cleanup_failure_still_attempts_destruction_and_restores_settings(tmp_path, failure):
    config = args(tmp_path, "--town", "Town13", "--ticks", "1", "--repeats", "1", "--idle-seconds", ".001")
    carla, state = fake_carla(**{failure: True})
    assert probe.run(config, carla) == 1
    assert state["destroyed"]  # stop() failure cannot skip destroy()
    assert state["world"].settings.synchronous_mode is False
    assert state["world"].settings.spectator_as_ego is True
    report = json.loads(config.output.read_text())
    assert report["status"] == "failed"
    assert report["cleanup"][-1]["status"] == "passed"  # settings, not entire cleanup
