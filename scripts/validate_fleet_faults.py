#!/usr/bin/env python3
"""Explicit fault injection into one fleet-owned service, never a manual server."""
import argparse
import json
import os
from pathlib import Path
import signal
import sys
import time
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packages/client"))
from easycarla_client import UnknownOutcomeError
from easycarla_client.vector_env import CarlaVectorEnv, BatchError
from easycarla.service.fleet import load_fleet, alive


def validate(instances, fleet_config, worker_id, output):
    _, _, workers = load_fleet(fleet_config)
    worker = next(w for w in workers if w["id"] == worker_id)
    record = json.loads((Path(worker["directory"]) / "status.json").read_text())
    if record["state"] != "ready" or not alive(record) or not alive(record["service"]):
        raise RuntimeError("fault injection requires a live fleet-owned worker")
    index = instances.index(worker["instance"])
    if len(instances) < 2:
        raise ValueError("at least one healthy peer is required")
    report = {
        "real_carla": True,
        "worker_id": worker_id,
        "service_epoch": record["service_epoch"],
        "status": "running",
    }
    env = None
    try:
        env = CarlaVectorEnv(instances, timeout=120)
        if env.envs[index].client.server_info["epoch"] != record["service_epoch"]:
            raise RuntimeError("service identity changed before fault injection")
        obs, _ = env.reset(seed=7)
        before = obs["frame"].copy()
        sequence = env.envs[index].client._sequence
        env.step_async(np.tile([0, 0, 1], (env.num_envs, 1)))
        deadline = time.monotonic() + 2
        while env.envs[index].client._sequence == sequence:
            if time.monotonic() >= deadline:
                raise RuntimeError("target request was not published")
            time.sleep(0.001)
        if not alive(record["service"]):
            raise RuntimeError("service exited before controlled injection")
        os.kill(record["service"]["pid"], signal.SIGKILL)
        try:
            env.step_wait()
        except BatchError as error:
            assert set(error.failures) == {index}
            assert isinstance(error.failures[index], UnknownOutcomeError)
            assert len(error.completed) == env.num_envs - 1
            results = []
            for i, result in error.completed.items():
                assert int(result[0]["frame"]) == int(before[i]) + 2
                healthy = env.envs[i].step([0, 0, 1])
                assert int(healthy[0]["frame"]) == int(result[0]["frame"]) + 2
                results.append(
                    {
                        "index": i,
                        "completed_frame": int(result[0]["frame"]),
                        "subsequent_frame": int(healthy[0]["frame"]),
                    }
                )
            report.update(
                status="passed",
                healthy_results=results,
                failure=str(error.failures[index]),
                unknown_action_not_replayed=True,
            )
        else:
            raise AssertionError("injected service loss must report a batch fault")
    except BaseException as error:
        report.update(status="failed", error=f"{type(error).__name__}: {error}")
        raise
    finally:
        try:
            if env is not None:
                env.close()
        finally:
            Path(output).write_text(json.dumps(report, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--instances", nargs="+", required=True)
    parser.add_argument("--fleet-config", required=True)
    parser.add_argument("--worker", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    validate(args.instances, args.fleet_config, args.worker, args.output)
