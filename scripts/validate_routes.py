#!/usr/bin/env python3
"""Client-only executable-route acceptance; no CARLA or training dependency.

Example: python scripts/validate_routes.py --manifest configs/manifests/v004_eval220.json
         --instance carla_01 --output route-results.json --route 28147
The NumPy reference driver is a smoke-test driver, not a benchmark baseline.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import time

import numpy as np
from easycarla_client import CarlaEnvClient
from easycarla_client.errors import RemoteError, ServiceUnavailable, UnknownOutcomeError


def reference_control(obs):
    points = obs["route"]["points"][obs["route"]["valid"]]
    if not len(points):
        return [0.0, 0.0, 1.0]
    speed = float(obs["ego"]["speed"])
    distances = np.linalg.norm(points[:, :2], axis=1)
    ahead = np.flatnonzero((distances > max(4.0, speed * 0.6)) & (points[:, 0] > 0))
    target = points[int(ahead[0])] if len(ahead) else points[-1]
    heading = math.atan2(float(target[1]), float(target[0]))
    steer = np.clip(-math.atan2(5.6 * math.sin(heading), max(1.0, float(np.linalg.norm(target[:2])))) / 0.65, -1, 1)
    target_speed = 1.0 if obs["route"]["length"] - obs["route"]["progress"] < 5 else 5.0
    error = target_speed - speed
    return [float(steer), float(np.clip(error * 0.3, 0, 0.5)), float(np.clip(-error * 0.2, 0, 0.6))]


def write_report(path, report):
    """Atomic replace after every route so an interrupted batch keeps evidence."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(report, indent=2, allow_nan=False) + "\n")
    temporary.replace(path)


def percentile(values):
    return {"p50": float(np.percentile(values, 50)), "p95": float(np.percentile(values, 95))} if values else None


def run(args):
    content = args.manifest.read_bytes()
    manifest = json.loads(content)
    if manifest.get("version") != 1 or manifest.get("source") != "bench2drive":
        raise ValueError("An explicit version-1 Bench2Drive manifest is required")
    entries = manifest["entries"]
    requested = {v for group in args.route for v in group.split(",")}
    available = {str(e["route_id"]) for e in entries}
    if requested - available:
        raise ValueError(f"Requested route absent from manifest: {sorted(requested - available)}")
    selected = [e for e in entries if not requested or str(e["route_id"]) in requested]
    if args.limit is not None:
        selected = selected[:args.limit]
    if not selected:
        raise ValueError("No routes selected")
    selected_keys = {(e["file"], str(e["route_id"])) for e in selected}
    results = [{"route_id": str(e["route_id"]), "town": e["town"], "file": e["file"],
                "scenario_types": e["scenario_types"], "route_fingerprint": e["route_fingerprint"],
                "selected": (e["file"], str(e["route_id"])) in selected_keys,
                "execution": "not_run", "reason": "awaiting execution" if (
                    e["file"], str(e["route_id"])) in selected_keys else "not selected"} for e in entries]
    report = {"version": 1, "source": "bench2drive", "instance": args.instance,
              "manifest": str(args.manifest.resolve()), "manifest_sha256": hashlib.sha256(content).hexdigest(),
              "manifest_split": manifest["split"], "started_utc": datetime.now(timezone.utc).isoformat(),
              "reference_driver": "NumPy route following only; ignores actors and traffic rules; not a benchmark baseline",
              "seed": args.seed, "maximum_decision_steps": args.steps,
              "status": "running", "results": results}
    write_report(args.output, report)
    env = None
    stop_reason = None
    try:
        env = CarlaEnvClient(instance_name=args.instance, timeout=args.timeout)
        for result in results:
            if not result["selected"]:
                continue
            if stop_reason is not None:
                result["reason"] = stop_reason
                continue
            started = time.monotonic()
            result.update(execution="running", reason=None, seed=args.seed, steps=0)
            write_report(args.output, report)
            roundtrips, communications, simulations = [], [], []
            try:
                obs, info = env.reset(seed=args.seed, options={"scenario_id": result["route_id"]})
                actual = info["scenario"]
                if actual["source"] != "bench2drive" or str(actual["scenario_id"]) != result["route_id"]:
                    raise ValueError(f"Service returned a different scenario: {actual}")
                result["actual_scenario"] = actual
                result["capabilities"] = env.capabilities()
                result["reset_frame"] = int(obs["frame"])
                result["initialization_timing"] = info.get("timing", {})
                activated = set()
                terminated = truncated = False
                for _ in range(args.steps):
                    previous_frame = int(obs["frame"])
                    obs, reward, terminated, truncated, info = env.step(reference_control(obs))
                    if int(obs["frame"]) <= previous_frame:
                        raise ValueError("Valid step did not advance its physics frame")
                    result["steps"] += 1
                    for scenario in info["events"].get("native_scenarios", []):
                        if scenario["status"] != "Status.INVALID":
                            activated.add(scenario["name"])
                    roundtrips.append(env.last_timings["roundtrip_seconds"])
                    communications.append(env.last_timings["transport_and_copy_seconds"])
                    simulations.append(info.get("timing", {}).get("simulation_seconds", 0.0))
                    if terminated or truncated:
                        break
                result.update(execution="completed" if terminated or truncated else "step_budget_reached",
                              terminated=bool(terminated), truncated=bool(truncated), final_frame=int(obs["frame"]),
                              route_progress_m=float(obs["route"]["progress"]),
                              native_success=bool(info["events"].get("native_success")),
                              activated_scenarios=sorted(activated), events=info["events"],
                              tick_counts=info.get("tick_counts", {}),
                              timings={"roundtrip_seconds": percentile(roundtrips),
                                       "transport_and_copy_seconds": percentile(communications),
                                       "simulation_seconds": percentile(simulations)})
            except (UnknownOutcomeError, ServiceUnavailable) as exc:
                # Never replay a step whose execution is uncertain, and never
                # reconnect and silently rerun the same route after server loss.
                result.update(execution="unknown_outcome" if isinstance(exc, UnknownOutcomeError) else "service_unavailable",
                              reason=str(exc))
                stop_reason = f"batch stopped after {result['route_id']}: {result['execution']}"
            except RemoteError as exc:
                result.update(execution="failed", reason=str(exc), remote_code=exc.code)
                if not args.continue_on_error:
                    stop_reason = f"batch stopped after {result['route_id']}: remote error"
            except Exception as exc:
                result.update(execution="failed", reason=f"{type(exc).__name__}: {exc}")
                stop_reason = f"batch stopped after {result['route_id']}: local validation failure"
            finally:
                result["wall_seconds"] = time.monotonic() - started
                write_report(args.output, report)
    except (ServiceUnavailable, UnknownOutcomeError) as exc:
        stop_reason = str(exc)
    except KeyboardInterrupt:
        stop_reason = "interrupted by user"
        raise
    except Exception as exc:
        stop_reason = f"{type(exc).__name__}: {exc}"
    finally:
        if env is not None:
            try:
                env.close()
            except Exception as exc:
                report["close_error"] = str(exc)
                stop_reason = stop_reason or "client close failed"
        for result in results:
            if result["selected"] and result["execution"] == "not_run" and stop_reason:
                result["reason"] = stop_reason
        report["status"] = "stopped" if stop_reason else "finished"
        report["stop_reason"] = stop_reason
        report["finished_utc"] = datetime.now(timezone.utc).isoformat()
        write_report(args.output, report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--instance", default="carla_01")
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path("route-results.json"))
    parser.add_argument("--route", action="append", default=[], help="repeatable route ID or comma-separated IDs")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--steps", type=int, default=1500)
    parser.add_argument("--seed", type=int, default=123)
    parser.add_argument("--timeout", type=float, default=60)
    parser.add_argument("--continue-on-error", action="store_true",
                        help="continue with a new reset after a known remote error; unknown outcomes always stop")
    args = parser.parse_args()
    if args.steps <= 0 or args.timeout <= 0 or (args.limit is not None and args.limit <= 0):
        parser.error("steps, timeout and limit must be positive")
    report = run(args)
    print(json.dumps({"status": report["status"], "output": str(args.output),
                      "executed": sum(r["execution"] != "not_run" for r in report["results"])}))
    if report["status"] == "stopped" or any(r["execution"] == "failed" for r in report["results"]):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
