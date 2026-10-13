"""Owned fleet preflight and process-identity protection, no CARLA launches."""

import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import pytest
import yaml
from easycarla.service.fleet import (
    load_fleet,
    process_start,
    alive,
    check_ports,
    fleet_command,
)


@pytest.fixture
def fleet_config(tmp_path):
    root = tmp_path / "carla"
    root.mkdir()
    (root / "CarlaUE4.sh").write_text("#!/bin/sh\n")
    config = tmp_path / "env.yaml"
    config.write_text(yaml.safe_dump({"simulation": {"no_rendering_mode": True}}))
    data = {
        "service_python": sys.executable,
        "carla_root": str(root),
        "log_dir": str(tmp_path / "logs"),
        "workers": [
            {
                "id": "one",
                "instance": "fleet_fixture",
                "config": str(config),
                "ports": {
                    "rpc": 31100,
                    "streaming": 31101,
                    "secondary": 31102,
                    "tm": 31103,
                },
            }
        ],
    }
    path = tmp_path / "fleet.yaml"

    def write(**changes):
        content = {**data, **changes}
        path.write_text(yaml.safe_dump(content))
        return path

    return write, data, config


def test_fleet_explicit_ports_and_memory_admission(fleet_config, monkeypatch):
    write, data, _ = fleet_config
    path = write()
    _, _, workers = load_fleet(path)
    assert workers[0]["config"]["simulation"]["port"] == 31100
    assert workers[0]["config"]["simulation"]["tm_port"] == 31103
    monkeypatch.setattr("easycarla.service.fleet.memory_available_mb", lambda: 100)
    monkeypatch.setattr("easycarla.service.fleet.check_ports", lambda p: None)
    with pytest.raises(RuntimeError, match="RAM"):
        fleet_command("start", path)


def test_fleet_rejects_shared_ports_rendering_and_unsafe_ids(fleet_config):
    write, data, config = fleet_config
    worker = data["workers"][0]
    other = {**worker, "id": "two", "instance": "different"}
    with pytest.raises(ValueError, match="distinct"):
        load_fleet(write(workers=[worker, other]))
    with pytest.raises(ValueError, match="path"):
        load_fleet(write(workers=[{**worker, "id": "../escape"}]))
    config.write_text(
        yaml.safe_dump(
            {"simulation": {"no_rendering_mode": True}, "observations": {"lidar": True}}
        )
    )
    with pytest.raises(ValueError, match="disabled"):
        load_fleet(write())


def test_port_preflight_does_not_kill_existing_owner():
    with socket.socket() as owner:
        owner.bind(("127.0.0.1", 0))
        port = owner.getsockname()[1]
        with pytest.raises(OSError):
            check_ports([port])
        assert owner.getsockname()[1] == port


def test_stop_refuses_reused_pid_identity(fleet_config):
    write, data, _ = fleet_config
    path = write()
    _, directory, workers = load_fleet(path)
    wd = Path(workers[0]["directory"])
    wd.mkdir(parents=True)
    owned = {
        "pid": os.getpid(),
        "process_start": "wrong_start",
        "state": "failed",
        "worker_id": "one",
        "instance": "fleet_fixture",
    }
    (wd / "status.json").write_text(json.dumps(owned))
    result = fleet_command("stop", path)
    assert result[0]["state"] == "failed" and not alive(owned)
    assert process_start(os.getpid()) is not None


@pytest.mark.parametrize("damage", ["config", "python", "carla", "settings"])
def test_management_does_not_require_startup_resources(fleet_config, damage):
    write, data, config = fleet_config
    if damage == "config":
        config.unlink()
    elif damage == "python":
        data["service_python"] = str(config.parent / "missing-python")
    elif damage == "carla":
        (Path(data["carla_root"]) / "CarlaUE4.sh").unlink()
    else:
        config.write_text("simulation:\n  no_rendering_mode: false\n")
    path = write()
    assert fleet_command("status", path)[0]["state"] == "not_started"
    assert fleet_command("stop", path)[0]["state"] == "not_started"
    with pytest.raises((FileNotFoundError, ValueError)):
        fleet_command("start", path)


def test_stop_uses_owned_process_identity_with_minimal_management_config(fleet_config):
    write, data, _ = fleet_config
    path = write()
    _, _, workers = load_fleet(path)
    worker = workers[0]
    wd = Path(worker["directory"])
    wd.mkdir(parents=True)
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        record = {"pid": process.pid, "process_start": process_start(process.pid),
                  "state": "ready", "worker_id": worker["id"], "instance": worker["instance"]}
        (wd / "status.json").write_text(json.dumps(record))
        path.write_text(yaml.safe_dump({"log_dir": data["log_dir"],
                                       "workers": [{"id": worker["id"], "instance": worker["instance"]}]}))
        assert fleet_command("status", path)[0]["state"] == "ready"
        fleet_command("stop", path)
        assert process.wait(timeout=3) != 0
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=3)


def test_restart_preflight_failure_does_not_stop_existing_worker(fleet_config):
    write, data, config = fleet_config
    path = write()
    _, _, workers = load_fleet(path)
    wd = Path(workers[0]["directory"])
    wd.mkdir(parents=True)
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        (wd / "status.json").write_text(json.dumps({
            "pid": process.pid, "process_start": process_start(process.pid), "state": "ready",
            "worker_id": "one", "instance": "fleet_fixture"}))
        config.unlink()
        with pytest.raises(FileNotFoundError):
            fleet_command("restart", path, "one")
        assert process.poll() is None
    finally:
        process.terminate()
        process.wait(timeout=3)


def test_management_refuses_status_identity_mismatch(fleet_config):
    write, _, _ = fleet_config
    path = write()
    _, _, workers = load_fleet(path)
    wd = Path(workers[0]["directory"])
    wd.mkdir(parents=True)
    (wd / "status.json").write_text(json.dumps({
        "worker_id": "one", "instance": "different_instance", "state": "stopped"}))
    with pytest.raises(RuntimeError, match="identity"):
        fleet_command("stop", path)
