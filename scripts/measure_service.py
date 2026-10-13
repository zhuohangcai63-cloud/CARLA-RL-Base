#!/usr/bin/env python3
"""Measure an independently started service using only the standalone client.

No policy, training framework, CARLA import, or automatic action retry is involved.
"""
import argparse
import json
from pathlib import Path
import platform
import sys
import time

import numpy as np
from easycarla_client import CarlaEnvClient


def percentiles(samples):
    if not samples:
        return {"count": 0}
    return {"count": len(samples), "p50": float(np.percentile(samples, 50)),
            "p95": float(np.percentile(samples, 95)), "mean": float(np.mean(samples)),
            "min": float(np.min(samples)), "max": float(np.max(samples))}


def array_bytes(value):
    if isinstance(value, np.ndarray):
        return value.nbytes
    if isinstance(value, dict):
        return sum(array_bytes(item) for item in value.values())
    if isinstance(value, (tuple, list)):
        return sum(array_bytes(item) for item in value)
    return 0


def measure(args):
    records = {"rpc_roundtrip_seconds": [], "rpc_backend_seconds": [],
               "transport_and_copy_seconds": [], "simulation_seconds": [],
               "control_commit_seconds": [], "observation_seconds": [],
               "sensor_wait_seconds": [], "environment_seconds": [],
               "reset_roundtrip_seconds": [], "initialization_seconds": [],
               "observation_array_bytes": []}
    report = {"status": "running", "measurement_kind": "independent_client_service_measurement",
              "instance": args.instance, "client_python": platform.python_version(),
              "client_prefix": sys.prefix, "control": args.action,
              "copy_observations": not args.borrow, "requested_episodes": args.episodes,
              "requested_steps_per_episode": args.steps, "episodes": [],
              "simulation_timing_source": "service-provided info.timing; not inferred from wall-clock duration"}
    env = None
    start = time.monotonic()
    try:
        options = json.loads(args.reset_options) if args.reset_options else None
        env = CarlaEnvClient(args.instance, timeout=args.timeout, copy=not args.borrow)
        for episode in range(args.episodes):
            obs, info = env.reset(seed=args.seed + episode, options=options)
            records["reset_roundtrip_seconds"].append(env.last_request_seconds)
            if "initialization_seconds" in info.get("timing", {}):
                records["initialization_seconds"].append(info["timing"]["initialization_seconds"])
            episode_report = {"index": episode, "seed": args.seed + episode,
                              "backend_reported": info.get("backend", "not_reported"),
                              "scenario": info.get("scenario", {}), "steps": 0,
                              "terminated": False, "truncated": False}
            report["episodes"].append(episode_report)
            for _ in range(args.steps):
                obs, reward, terminated, truncated, info = env.step(args.action)
                episode_report["steps"] += 1
                episode_report.update(terminated=bool(terminated), truncated=bool(truncated))
                records["observation_array_bytes"].append(array_bytes(obs))
                records["rpc_roundtrip_seconds"].append(env.last_timings["roundtrip_seconds"])
                records["rpc_backend_seconds"].append(env.last_timings["backend_seconds"])
                records["transport_and_copy_seconds"].append(env.last_timings["transport_and_copy_seconds"])
                for key in ("simulation_seconds", "control_commit_seconds", "observation_seconds",
                            "sensor_wait_seconds", "environment_seconds"):
                    if key in info.get("timing", {}):
                        records[key].append(info["timing"][key])
                if terminated or truncated:
                    episode_report["termination_reason"] = info.get("termination_reason", "not_reported")
                    break
        report["status"] = "passed"
    except Exception as exc:
        report["status"] = "failed"
        report["error"] = {"type": type(exc).__name__, "message": str(exc)}
    finally:
        if env is not None:
            try:
                env.close()
            except Exception as exc:
                report["status"] = "failed"
                report["close_error"] = {"type": type(exc).__name__, "message": str(exc)}
        report["wall_seconds"] = time.monotonic() - start
        report["metrics"] = {key: percentiles(values) for key, values in records.items()}
        report["notes"] = ["Passing this runner verifies successful communication, not driving task success.",
                           "Empty timing samples mean the backend did not supply that measurement.",
                           "Transport time includes scheduling, encoding, shared-memory transfer and copying."]
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--instance", default="carla_01")
    parser.add_argument("--episodes", type=int, default=3)
    parser.add_argument("--steps", type=int, default=100)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--action", nargs=3, type=float, default=[0, .2, 0], metavar=("STEER", "THROTTLE", "BRAKE"))
    parser.add_argument("--timeout", type=float, default=120)
    parser.add_argument("--reset-options", help="JSON options passed explicitly to reset")
    parser.add_argument("--borrow", action="store_true", help="Use short-lived shared observation views")
    parser.add_argument("--output", type=Path, help="Write the complete JSON report in addition to stdout")
    args = parser.parse_args(argv)
    if args.episodes < 1 or args.steps < 1:
        parser.error("episodes and steps must be positive")
    report = measure(args)
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + "\n")
    print(text)
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
