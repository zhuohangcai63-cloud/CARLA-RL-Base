#!/usr/bin/env python3
"""Real owned-fleet acceptance: spaces, histories, trajectories and fault isolation."""
import argparse
import json
from pathlib import Path
import sys
import time
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packages/client"))
from easycarla_client import CarlaEnvClient
from easycarla_client.gym_env import CarlaGymEnv
from easycarla_client.vector_env import CarlaVectorEnv


def validate(instances, output):
    report = {
        "real_carla": True,
        "instances": instances,
        "checks": {},
        "status": "running",
    }
    try:
        return _validate(instances, output, report)
    except BaseException as error:
        report.update(
            status="failed",
            error=f"{type(error).__name__}: {error}",
            error_details=getattr(error, "details", {}),
        )
        Path(output).write_text(json.dumps(report, indent=2))
        raise


def _validate(instances, output, report):
    env = CarlaVectorEnv(instances, timeout=120)
    try:
        obs, info = env.reset(seed=7)
        assert env.observation_space.contains(obs)
        assert not obs["planning.history_ready"].any()
        before = obs["frame"].copy()
        for _ in range(20):
            obs, r, term, trunc, info = env.step(np.tile([0, 0, 1], (env.num_envs, 1)))
        assert (
            obs["planning.history_ready"].all() and not term.any() and not trunc.any()
        )
        assert np.all(obs["frame"] - before == 40)
        assert np.all(obs["planning.geometry.rear_axle_valid"])
        report["checks"].update(
            fixed_space=True,
            explicit_history_warmup=True,
            tick_counts=True,
            geometry_valid=True,
        )
        report["geometries"] = [
            {
                k: np.asarray(v[i]).tolist()
                for k, v in obs.items()
                if k.startswith("planning.geometry.")
            }
            for i in range(env.num_envs)
        ]
        routes = []
        for i, single in enumerate(env.envs):
            route = single.get_route()
            static = single.get_static()
            assert route["corridor"].any() and len(route["ordered_segments"]) > 1
            assert not static["boundary_is_road_mesh"]
            assert len(static["connections"]) > 0
            routes.append(
                {
                    "route_id": route["route_id"],
                    "map_id": static["map_id"],
                    "segments": len(route["ordered_segments"]),
                    "map_segments": len(static["lane_ids"]),
                }
            )
        report["routes"] = routes
        report["checks"].update(
            route_query=True, map_topology=True, approximate_boundary_provenance=True
        )
        mask = np.zeros(env.num_envs, bool)
        mask[0] = True
        held = obs["frame"].copy()
        obs, info = env.reset(mask=mask, seed=7)
        if env.num_envs > 1:
            assert int(obs["frame"][1]) == int(held[1])
        assert not bool(obs["planning.history_ready"][0])
        report["checks"]["masked_reset"] = True
    finally:
        env.close()
    for instance in instances:
        env = CarlaGymEnv(instance, timeout=120, action_mode="trajectory")
        try:
            obs, info = env.reset(seed=7)
            action = np.zeros(env.action_space.shape, np.float32)
            for _ in range(3):
                obs, r, term, trunc, info = env.step(action)
            assert env.observation_space.contains(obs) and info["physical_ticks"] == 2
            report["checks"]["fixed_trajectory_space"] = True
        finally:
            env.close()
        with CarlaEnvClient(instance, timeout=120) as client:
            raw, info = client.reset(seed=7)
            trajectory = {
                "reference_frame": raw["frame"],
                "reference_point": "rear_axle",
                "times": np.array([0.0, 0.1, 0.2]),
                "points": np.zeros((3, 4), np.float32),
            }
            result = client.step_trajectory(trajectory)
            assert result[4]["physical_ticks"] == 2
            assert result[0]["planning"]["reference_valid"]
            report["checks"]["rear_axle_trajectory"] = True
    report["status"] = "passed"
    Path(output).write_text(json.dumps(report, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--instances", nargs="+", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    validate(args.instances, args.output)
