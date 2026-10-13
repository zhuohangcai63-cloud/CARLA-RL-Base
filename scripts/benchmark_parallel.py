#!/usr/bin/env python3
"""Valid request throughput, including resets; no policy/model or training code."""
import argparse
import hashlib
from collections import defaultdict
import json
import os
from pathlib import Path
import sys
import time
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packages/client"))
from easycarla_client import CarlaEnvClient, ClientPool


def resources():
    """Read simulator/service process usage without importing simulator modules."""
    result = {"timestamp": time.time(), "processes": []}
    page = os.sysconf("SC_PAGE_SIZE")
    for path in Path("/proc").glob("[0-9]*/stat"):
        try:
            command = (
                (path.parent / "cmdline")
                .read_bytes()
                .replace(b"\0", b" ")
                .decode(errors="replace")
            )
            if (
                "CarlaUE4-Linux-Shipping" not in command
                and "easycarla.service.cli serve" not in command
            ):
                continue
            fields = path.read_text().rsplit(")", 1)[1].split()
            if fields[0] == "Z":
                continue
            result["processes"].append(
                {
                    "pid": int(path.parent.name),
                    "pgid": int(fields[2]),
                    "rss_mb": int(fields[21]) * page / 1024**2,
                    "cpu_ticks": int(fields[11]) + int(fields[12]),
                    "threads": int(fields[17]),
                    "command": command,
                }
            )
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            continue
    memory = {
        line.split(":")[0]: int(line.split()[1]) / 1024
        for line in Path("/proc/meminfo").read_text().splitlines()
        if line.startswith(("MemAvailable:", "SwapFree:"))
    }
    result.update(memory)
    return result


def benchmark(
    instances,
    mode,
    requests,
    repetitions,
    timeout,
    seed,
    output,
    seconds=None,
    worker_seeds=None,
):
    worker_seeds = (
        list(worker_seeds)
        if worker_seeds is not None
        else [seed + i for i in range(len(instances))]
    )
    if len(worker_seeds) != len(instances) or any(value < 0 for value in worker_seeds):
        raise ValueError("one nonnegative seed per instance is required")
    clients = {
        i: CarlaEnvClient(name, timeout=timeout) for i, name in enumerate(instances)
    }
    report = {
        "schema_version": 1,
        "instances": instances,
        "mode": mode,
        "requests_per_repetition": requests,
        "repetitions": [],
        "seed": seed,
        "worker_seeds": worker_seeds,
        "action": [0.0, 0.0, 1.0],
        "workload": "stationary_brake; NPCs and scene continue ticking",
        "started_at": time.time(),
        "status": "running",
        "service_epochs": {i: c.server_info["epoch"] for i, c in clients.items()},
    }
    report["resources"] = [resources()]
    last_resource = time.monotonic()
    root = Path(__file__).resolve().parents[1]
    report["source_hashes"] = {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for folder in ("easycarla", "packages/client/easycarla_client")
        for p in (root / folder).rglob("*.py")
    }
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)

    def save():
        temporary = output.with_suffix(".tmp")
        temporary.write_text(json.dumps(report, indent=2))
        temporary.replace(output)

    save()
    try:
        with ClientPool(clients) as pool:
            for repetition in range(repetitions):
                started = time.monotonic()
                reset_seconds = 0.0
                step_seconds = 0.0
                valid = 0
                ticks = 0
                resets = 0
                durations = []
                timings = defaultdict(list)
                events = []
                reward_sum = 0.0
                reward_components = defaultdict(float)
                planning_checks = defaultdict(int)
                for i, c in clients.items():
                    t = time.monotonic()
                    obs, info = c.reset(seed=worker_seeds[i])
                    reset_seconds += time.monotonic() - t
                    resets += 1
                    report.setdefault("initialization", {})[i] = {
                        "effective_world_settings": info.get(
                            "effective_world_settings"
                        ),
                        "scenario": info.get("scenario"),
                        "geometry": {
                            k: np.asarray(v).tolist()
                            for k, v in obs.get("planning", {})
                            .get("geometry", {})
                            .items()
                        },
                    }
                while valid < requests or (
                    seconds is not None and time.monotonic() - started < seconds
                ):
                    remaining = requests - valid
                    ids = (
                        list(clients)
                        if seconds is not None
                        else list(clients)[: min(len(clients), remaining)]
                    )
                    t = time.monotonic()
                    completed = []
                    if mode == "parallel":
                        for i in ids:
                            pool.submit(i, "step", [0, 0, 1])
                        waiting = set(ids)
                        while waiting:
                            for item in pool.poll_ready(timeout=0.1):
                                waiting.remove(item.worker_id)
                                if item.error is not None:
                                    raise item.error
                                completed.append((item.worker_id, item.result))
                    else:
                        for i in ids:
                            completed.append((i, clients[i].step([0, 0, 1])))
                    duration = time.monotonic() - t
                    step_seconds += duration
                    durations.append(duration)
                    for i, result in completed:
                        obs, reward, terminated, truncated, info = result
                        valid += 1
                        ticks += info.get("physical_ticks", 0)
                        reward_sum += reward
                        for key, value in info.get("reward_components", {}).items():
                            reward_components[key] += value
                        if "planning" in obs:
                            planning = obs["planning"]
                            planning_checks["reference_valid"] += bool(
                                planning["reference_valid"]
                            )
                            planning_checks["history_ready"] += bool(
                                planning["history_ready"]
                            )
                            planning_checks["current_velocity_valid"] += bool(
                                planning["current"]["velocity_valid"]
                            )
                        for key, value in info.get("timing", {}).items():
                            if isinstance(value, (float, int)):
                                timings[key].append(value)
                        for key, value in clients[i].last_timings.items():
                            timings[key].append(value)
                        if terminated or truncated:
                            events.append(
                                {
                                    "worker": i,
                                    "frame": obs["frame"],
                                    "terminated": terminated,
                                    "truncated": truncated,
                                    "reason": info.get("termination_reason"),
                                }
                            )
                            t = time.monotonic()
                            clients[i].reset(seed=worker_seeds[i])
                            reset_seconds += time.monotonic() - t
                            resets += 1
                    if valid % 100 < len(ids):
                        report["progress"] = {
                            "repetition": repetition,
                            "valid_requests": valid,
                            "elapsed_seconds": time.monotonic() - started,
                        }
                        save()
                    if time.monotonic() - last_resource >= 30:
                        report["resources"].append(resources())
                        last_resource = time.monotonic()
                        save()
                elapsed = time.monotonic() - started
                report["repetitions"].append(
                    {
                        "valid_requests": valid,
                        "physical_ticks": ticks,
                        "resets": resets,
                        "reward_sum": reward_sum,
                        "reward_components": dict(reward_components),
                        "planning_checks": dict(planning_checks),
                        "step_seconds": step_seconds,
                        "reset_seconds": reset_seconds,
                        "elapsed_seconds": elapsed,
                        "step_throughput": valid / step_seconds,
                        "end_to_end_throughput": valid / elapsed,
                        "batch_latency_p50_seconds": float(np.median(durations)),
                        "batch_latency_p95_seconds": float(
                            np.percentile(durations, 95)
                        ),
                        "timing_mean_seconds": {
                            k: float(np.mean(v)) for k, v in timings.items()
                        },
                        "terminal_events": events,
                    }
                )
                report["service_final_timing"] = {
                    i: c.capabilities().get("service_timings", {})
                    for i, c in clients.items()
                }
                save()
        report["status"] = "completed"
    except BaseException as exc:
        report.update(status="failed", error=f"{type(exc).__name__}: {exc}")
        report["error_details"] = getattr(exc, "details", {})
        if "valid" in locals():
            report["partial_repetition"] = {
                "valid_requests": valid,
                "physical_ticks": ticks,
                "resets": resets,
                "reset_seconds": reset_seconds,
                "step_seconds": step_seconds,
            }
        raise
    finally:
        errors = []
        for c in clients.values():
            try:
                c.close()
            except Exception as exc:
                errors.append(str(exc))
        report.update(finished_at=time.time(), cleanup_errors=errors)
        report["resources"].append(resources())
        save()
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--instances", nargs="+", required=True)
    parser.add_argument("--mode", choices=("serial", "parallel"), default="parallel")
    parser.add_argument("--requests", type=int, default=1000)
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--timeout", type=float, default=120)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument(
        "--worker-seeds",
        type=int,
        nargs="+",
        help="explicit seed per worker; defaults to seed+i",
    )
    parser.add_argument(
        "--seconds", type=float, help="minimum duration for a soak repetition"
    )
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    if (
        args.requests < 1
        or args.repetitions < 1
        or (args.seconds is not None and args.seconds <= 0)
    ):
        parser.error("positive requests/repetitions/duration required")
    benchmark(
        args.instances,
        args.mode,
        args.requests,
        args.repetitions,
        args.timeout,
        args.seed,
        args.output,
        args.seconds,
        args.worker_seeds,
    )
