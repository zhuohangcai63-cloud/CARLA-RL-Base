#!/usr/bin/env python3
"""Build/install both wheels into isolated environments and exercise POSIX IPC.

No CARLA process or repository import path is used. This is packaging/transport
acceptance, not a simulator integration test. Temporary environments are removed
on exit; --output preserves machine-readable evidence including wheel hashes.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import uuid
import zipfile


CLIENT_PROBE = r'''
import importlib.util
import json
import sys
import time
import numpy as np
from easycarla_client import CarlaEnvClient
from easycarla_client.transport import Names, read_live_record

for forbidden in ("carla", "scenic", "torch", "easycarla", "gymnasium"):
    assert importlib.util.find_spec(forbidden) is None, forbidden
name = sys.argv[1]
deadline = time.monotonic() + 20
while time.monotonic() < deadline:
    record = read_live_record(Names(name).server_lock)
    if record and record.get("ready"):
        break
    time.sleep(.05)
else:
    raise RuntimeError("Installed service never became ready")
frames = []
with CarlaEnvClient(name) as env:
    for episode in range(3):
        obs, info = env.reset(seed=episode)
        assert info["backend"] == "fake"
        held = obs["state"]
        expected = held.copy()
        for _ in range(10):
            obs, reward, terminated, truncated, info = env.step([0, .2, 0])
            assert not terminated and not truncated
            frames.append(int(obs["frame"]))
        np.testing.assert_array_equal(held, expected)
    assert env.get_static("fake-map-v1")["points"].shape == (4, 3)
with CarlaEnvClient(name) as env:
    assert not env.experiment.diagnostics()["active"]
    obs, _ = env.reset(seed=42)
    assert int(obs["frame"]) == 0
print(json.dumps({"python": sys.version, "prefix": sys.prefix, "episodes": 4,
                  "steps": len(frames), "last_frame": frames[-1],
                  "forbidden_imports_absent": True, "close_reconnect": True,
                  "observation_copy_stable": True}))
'''

CLEANUP_PROBE = r'''
import json
import sys
import posix_ipc
from easycarla_client.transport import Names
names = Names(sys.argv[1])
for slot in ("request", "response", "static"):
    try:
        resource = posix_ipc.SharedMemory(names.shm(slot))
    except posix_ipc.ExistentialError:
        continue
    resource.close_fd()
    raise AssertionError("Shared memory residue: " + slot)
for direction in ("request_ready", "result_ready"):
    try:
        resource = posix_ipc.Semaphore(names.sem(direction))
    except posix_ipc.ExistentialError:
        continue
    resource.close()
    raise AssertionError("Semaphore residue: " + direction)
print(json.dumps({"shared_memory_unlinked": True, "semaphores_unlinked": True}))
'''


def run(command, *, cwd, env, timeout=300):
    result = subprocess.run([str(arg) for arg in command], cwd=cwd, env=env,
                            text=True, capture_output=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError(f"Command failed: {command!r}\n{result.stdout}\n{result.stderr}")
    return result.stdout.strip()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--client-python", default=sys.executable,
                        help="Interpreter used to create the client venv (may differ from service Python)")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if sys.platform != "linux":
        parser.error("POSIX transport acceptance is supported on Linux")
    root = Path(__file__).resolve().parents[1]
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    env.pop("PYTHONHOME", None)
    report = {"status": "failed", "backend": "fake", "real_carla": False,
              "environment_type": "isolated_venv", "source_checkout_imports": False}
    started = time.perf_counter()
    try:
        with tempfile.TemporaryDirectory(prefix="easycarla-wheel-check-") as directory:
            work = Path(directory)
            wheels = work / "wheels"
            run([sys.executable, "-m", "pip", "wheel", "--no-deps", "--wheel-dir", wheels,
                 root / "packages/client", root], cwd=work, env=env)
            artifacts = sorted(wheels.glob("*.whl"))
            if len(artifacts) != 2:
                raise RuntimeError("Expected exactly the client and service wheels")
            report["wheels"] = [{"filename": wheel.name, "sha256": hashlib.sha256(wheel.read_bytes()).hexdigest()}
                                for wheel in artifacts]
            for wheel in artifacts:
                with zipfile.ZipFile(wheel) as archive:
                    contents = archive.namelist()
                    assert not any(name.startswith(("tests/", "torch/", "scenic/", "carla/")) for name in contents)
                    if wheel.name.startswith("easycarla_client-"):
                        assert not any(name.startswith("easycarla/") for name in contents)
                    else:
                        assert "easycarla/extensions/session.py" in contents
            service = work / "service"
            client = work / "client"
            run([sys.executable, "-m", "venv", service], cwd=work, env=env)
            run([args.client_python, "-m", "venv", client], cwd=work, env=env)
            service_python, client_python = service / "bin/python", client / "bin/python"
            run([service_python, "-m", "pip", "install", *artifacts], cwd=work, env=env)
            client_wheel = next(path for path in artifacts if path.name.startswith("easycarla_client-"))
            run([client_python, "-m", "pip", "install", client_wheel], cwd=work, env=env)
            report["service_pip_check"] = run([service_python, "-m", "pip", "check"], cwd=work, env=env)
            report["client_pip_check"] = run([client_python, "-m", "pip", "check"], cwd=work, env=env)
            for label, executable in (("service", service_python), ("client", client_python)):
                report[label + "_packages"] = json.loads(run([executable, "-m", "pip", "list", "--format=json"], cwd=work, env=env))
            report["service"] = json.loads(run([service_python, "-c",
                "import easycarla,sys,json; print(json.dumps({'python':sys.version,'prefix':sys.prefix,'package_file':easycarla.__file__}))"], cwd=work, env=env))
            config = work / "fake.json"
            config.write_text(json.dumps({"backend": "fake"}))
            name = "wheel_" + uuid.uuid4().hex
            probe = work / "client_probe.py"
            probe.write_text(CLIENT_PROBE)
            with (work / "service.log").open("w") as log:
                process = subprocess.Popen([service / "bin/easycarla", "serve", "--instance", name,
                                            "--config", config], cwd=work, env=env, stdout=log, stderr=log)
                try:
                    report["client"] = json.loads(run([client_python, probe, name], cwd=work, env=env, timeout=40))
                    assert report["client"]["prefix"] != report["service"]["prefix"]
                    if process.poll() is not None:
                        raise RuntimeError("Client close unexpectedly stopped service")
                finally:
                    process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
                    report["service_exit_code"] = process.returncode
                    report["service_log"] = (work / "service.log").read_text()
            if process.returncode != 0:
                raise RuntimeError("Installed service did not shut down cleanly")
            report["cleanup"] = json.loads(run([client_python, "-c", CLEANUP_PROBE, name], cwd=work, env=env))
            report["status"] = "passed"
    except Exception as exc:
        report["error"] = str(exc)
        raise
    finally:
        report["wall_seconds"] = time.perf_counter() - started
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"status": report["status"], "report": str(args.output), "real_carla": False}))


if __name__ == "__main__":
    main()
