"""Optional installation-level test with a genuinely independent client interpreter.

EASYCARLA_CLIENT_PYTHON=/path/to/client-conda/bin/python pytest -s tests/test_ipc_conda.py
Uses a fake backend; this test makes no real-CARLA claim.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

import pytest

from easycarla_client.transport import Names, read_live_record


CLIENT_SCRIPT = r'''
import importlib.util
import json
import sys
import time
import numpy as np
from easycarla_client import CarlaEnvClient

for forbidden in ("carla", "scenic", "torch", "easycarla"):
    assert importlib.util.find_spec(forbidden) is None, forbidden

summary = {"backend": "fake", "python": sys.version, "prefix": sys.prefix,
           "episodes": 3, "steps": 0, "independent_client": True}
with CarlaEnvClient(sys.argv[1]) as env:
    samples = []
    for episode in range(3):
        obs, info = env.reset(seed=episode)
        assert info["backend"] == "fake"
        for step in range(100):
            obs, reward, terminated, truncated, info = env.step([0, .2, 0])
            assert int(obs["frame"]) == (step + 1) * 4
            assert not terminated and not truncated
            summary["steps"] += 1
            samples.append(dict(env.last_timings))
    summary["structured_ms"] = {key: {"p50": float(np.percentile([s[key] for s in samples], 50)*1000),
                                     "p95": float(np.percentile([s[key] for s in samples], 95)*1000)}
                                for key in samples[0]}
    bulk = np.zeros(512*1024, dtype=np.uint8)
    samples = []
    for _ in range(100):
        assert env.experiment.echo(payload=bulk)["payload"].shape == bulk.shape
        samples.append(dict(env.last_timings))
    summary["synthetic_512kib_echo_ms"] = {key: {"p50": float(np.percentile([s[key] for s in samples], 50)*1000),
                                               "p95": float(np.percentile([s[key] for s in samples], 95)*1000)}
                                          for key in samples[0]}
with CarlaEnvClient(sys.argv[1]) as env:
    assert not env.experiment.diagnostics()["active"]
    env.reset()
    assert int(env.step([0, 0, 0])[0]["frame"]) == 4
summary["close_then_reconnect"] = True
print(json.dumps(summary))
'''


def test_independent_conda_client(tmp_path):
    client_python = os.environ.get("EASYCARLA_CLIENT_PYTHON")
    if not client_python:
        pytest.skip("Set EASYCARLA_CLIENT_PYTHON to an independently installed client Conda interpreter")
    name = "conda_" + uuid.uuid4().hex
    root = Path(__file__).resolve().parents[1]
    config = tmp_path / "fake.json"
    config.write_text(json.dumps({"backend": "fake"}))
    client_script = tmp_path / "client_probe.py"
    client_script.write_text(CLIENT_SCRIPT)
    server_env = os.environ.copy()
    server_env["PYTHONPATH"] = str(root / "packages/client") + os.pathsep + str(root)
    client_env = os.environ.copy()
    client_env.pop("PYTHONPATH", None)
    with (tmp_path / "service.log").open("w") as log:
        process = subprocess.Popen([sys.executable, "-m", "easycarla.service.cli", "serve",
                                    "--instance", name, "--config", str(config)],
                                   cwd=root, env=server_env, stdout=log, stderr=log)
        try:
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                record = read_live_record(Names(name).server_lock)
                if record and record.get("ready"):
                    break
                assert process.poll() is None, (tmp_path / "service.log").read_text()
                time.sleep(.05)
            else:
                pytest.fail("Service did not become ready")
            result = subprocess.run([client_python, str(client_script), name], cwd=tmp_path,
                                    env=client_env, capture_output=True, text=True, timeout=30)
            assert result.returncode == 0, result.stderr
            report = json.loads(result.stdout)
            assert report["prefix"] != sys.prefix
            assert report["steps"] == 300
            assert process.poll() is None, "Client close must leave environment service alive"
            report["server_python"] = sys.version
            report["server_prefix"] = sys.prefix
            print(json.dumps(report, ensure_ascii=False, indent=2))
            if os.environ.get("EASYCARLA_IPC_REPORT"):
                Path(os.environ["EASYCARLA_IPC_REPORT"]).write_text(json.dumps(report, indent=2))
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            assert process.returncode == 0, (tmp_path / "service.log").read_text()
