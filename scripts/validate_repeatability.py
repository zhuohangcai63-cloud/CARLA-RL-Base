#!/usr/bin/env python3
"""Repeat real reconstruction and an identical continuation; never relax tolerances."""
import argparse
import json
from pathlib import Path
import time

from easycarla.runtime import EnvironmentRuntime
from easycarla.service.cli import load_config
from easycarla.extensions.replay import compare_states


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="configs/interaction.yaml")
    parser.add_argument("--port", type=int, default=2200)
    parser.add_argument("--trials", type=int, default=5)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.trials < 1:
        parser.error("trials must be positive")
    config = load_config(args.config)
    config.setdefault("simulation", {}).update(port=args.port, tm_port=args.port + 6000, timeout=60)
    tolerances = dict(position=.2, velocity=.2, yaw=.03, actor_state=.2,
                      controllers=1e-5, history=.2, events=1e-5, signals=1e-5, task=.01)
    report = {"backend": "carla", "status": "running", "trials": [], "tolerances": tolerances,
              "prefix_decisions": 5, "continuation_decisions": 3, "action": [0, .4, 0],
              "seed": 7, "config": config, "settling_ticks_added": 0}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    def save():
        args.output.write_text(json.dumps(report, indent=2) + "\n")
    save()
    runtime = EnvironmentRuntime(config)
    try:
        for index in range(args.trials):
            result = {"trial": index, "status": "running"}
            report["trials"].append(result)
            save()
            runtime.reset(seed=7)
            runtime.experiment("begin_prefix")
            for _ in range(report["prefix_decisions"]):
                runtime.step(report["action"])
            prefix = runtime.experiment("finish_prefix")
            for _ in range(report["continuation_decisions"]):
                runtime.step(report["action"])
            continuation = runtime.raw_state()
            started = time.perf_counter()
            replay = runtime.experiment("replay_prefix", prefix=prefix, tolerances=tolerances)
            result["replay_seconds"] = time.perf_counter() - started
            result["prefix"] = replay["report"]
            for _ in range(report["continuation_decisions"]):
                runtime.step(report["action"])
            result["continuation"] = compare_states(continuation, runtime.raw_state(), tolerances)
            result["status"] = "passed" if result["prefix"]["comparable"] and result["continuation"]["comparable"] else "failed"
            save()
            print(json.dumps({"trial": index, "status": result["status"], "replay_seconds": result["replay_seconds"]}), flush=True)
        report["status"] = "passed" if all(trial["status"] == "passed" for trial in report["trials"]) else "failed"
    except Exception as exc:
        report.update(status="failed", error={"type": type(exc).__name__, "message": str(exc)})
        raise
    finally:
        try:
            runtime.shutdown()
        finally:
            save()
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
