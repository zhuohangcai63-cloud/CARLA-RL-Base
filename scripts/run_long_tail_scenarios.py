"""Client-only event smoke runner for configs/scenic_suite.yaml.

This deliberately simple reference controller is for plumbing checks, not an
assessment of driving ability. Actual event states are reported independently.
"""
import argparse
import json
from pathlib import Path
import numpy as np
from easycarla_client import CarlaEnvClient

SCENARIOS = ["pedestrian_crossing", "hard_break_route", "parking_cut_in",
             "vehicle_opens_door", "construction_obstacle"]


def reference_action(obs, speed=6.0):
    route = obs["route"]
    points = route["points"][route["valid"]]
    ahead = points[points[:, 0] > 3]
    target = ahead[0] if len(ahead) else np.array([3, 0, 0])
    # Geometry is left-positive while CARLA steer is right-positive.
    steer = float(np.clip(-np.arctan2(2 * 2.8 * target[1], max(np.dot(target[:2], target[:2]), 1)) / .7, -1, 1))
    error = speed - obs["ego"]["speed"]
    return [steer, float(np.clip(.3 * error, 0, .6)), float(np.clip(-.3 * error, 0, 1))]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--instance", default="carla_01")
    parser.add_argument("--scenarios", nargs="+", choices=SCENARIOS, default=SCENARIOS)
    parser.add_argument("--episodes", type=int, default=2)
    parser.add_argument("--max-steps", type=int, default=150)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--output", default="/tmp/easycarla-events.json")
    args = parser.parse_args()
    rows = []
    try:
        with CarlaEnvClient(args.instance, timeout=120) as env:
            for name in args.scenarios:
                for episode in range(args.episodes):
                    row = {"scenario": name, "seed": args.seed+episode, "status": "running"}
                    rows.append(row)
                    obs, info = env.reset(seed=row["seed"], options={"scenario_id": name})
                    row["generation"] = info["events"]
                    for i in range(args.max_steps):
                        obs, reward, terminated, truncated, info = env.step(reference_action(obs))
                        if terminated or truncated:
                            break
                    row.update(status="executed", steps=i+1, terminated=terminated, truncated=truncated,
                               events=info["events"], reason=info["termination_reason"])
                    Path(args.output).write_text(json.dumps(rows, indent=2, default=str))
    except Exception as exc:
        if rows:
            rows[-1].update(status="failed", error=f"{type(exc).__name__}: {exc}")
        raise
    finally:
        Path(args.output).write_text(json.dumps(rows, indent=2, default=str))


if __name__ == "__main__":
    main()
