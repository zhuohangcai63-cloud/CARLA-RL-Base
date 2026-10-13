#!/usr/bin/env python3
"""Real event completion checks with explicit test-driver responses.

This is a simulator validation utility, not a training/candidate-generation API.
The driver stops after brake/door triggers and uses a declared adjacent-lane
bypass for construction. Scenario configuration and every applied action are
retained in the report; no teleporting or direct actor state edits occur.
"""
import argparse
import json
from pathlib import Path
import time
import numpy as np

from easycarla.runtime import EnvironmentRuntime
from easycarla.control import PathTracker
from easycarla.observations.geometry import xyz


def bypass_route(runtime):
    event = runtime.episode.events.events[0]
    anchor = event["anchor_location"]
    waypoint = runtime.session.map.get_waypoint(anchor)
    left, right = waypoint.get_left_lane(), waypoint.get_right_lane()
    c = runtime.session.carla
    def usable(candidate):
        if candidate is None or candidate.lane_type != c.LaneType.Driving:
            return False
        return abs((candidate.transform.rotation.yaw - waypoint.transform.rotation.yaw + 180) % 360 - 180) < 30
    if usable(left):
        direction, shift = 1., (waypoint.lane_width + left.lane_width) / 2
    elif usable(right):
        direction, shift = -1., (waypoint.lane_width + right.lane_width) / 2
    else:
        raise RuntimeError("No adjacent driving lane for the construction validation bypass")
    # The visual prop can extend beyond its nominal lane. Compute clearance
    # from actual world-space bounding boxes rather than assuming a centerline
    # shift of one lane is sufficient.
    right_axis = waypoint.transform.get_right_vector()
    lateral_edges = []
    for actor_id in event["actor_ids"]:
        actor = runtime.session.registry.actors[actor_id]
        for vertex in actor.bounding_box.get_world_vertices(actor.get_transform()):
            delta = vertex - anchor
            lateral_edges.append(delta.x * right_axis.x + delta.y * right_axis.y)
    if lateral_edges:
        obstacle_edge = -min(lateral_edges) if direction > 0 else max(lateral_edges)
        clearance = obstacle_edge + runtime.episode.ego.bounding_box.extent.y + .7
        shift = max(shift, clearance)
        candidate_location = anchor - right_axis * (direction * shift)
        if runtime.session.map.get_waypoint(candidate_location, project_to_road=False,
                                           lane_type=c.LaneType.Driving) is None:
            raise RuntimeError("Construction props leave no legal driving-lane clearance on the chosen side")
    original = runtime.episode.route
    route = np.asarray([xyz(transform.location) for transform in original])
    distances = np.r_[0., np.cumsum(np.linalg.norm(np.diff(route, axis=0), axis=1))]
    anchor_index = int(np.argmin(np.linalg.norm(route - xyz(anchor), axis=1)))
    anchor_distance = distances[anchor_index]
    for index, transform in enumerate(original):
        approach = np.clip((distances[index] - (anchor_distance - 28)) / 15, 0, 1)
        departure = np.clip(((anchor_distance + 40) - distances[index]) / 15, 0, 1)
        blend = min(approach, departure)
        # Smooth lane change; no change in the environment's configured route.
        blend = blend * blend * (3 - 2 * blend)
        route[index] -= xyz(transform.get_right_vector()) * direction * shift * blend
    return route, {"kind": "adjacent_lane_bypass", "side": "left" if direction > 0 else "right",
                   "lateral_m": float(shift), "anchor_route_distance_m": float(anchor_distance)}


def run(scenario, port, seed, max_steps, directory):
    config = {"simulation": {"port": port, "tm_port": port + 6000, "timeout": 60},
              "scenario": {"source": "scenic", "town": "Town10HD",
                           "file": f"scenarios/scenic/{scenario}.scenic",
                           "params": {"number_of_vehicles": 0, "number_of_walkers": 0,
                                      "traffic_light_policy": "off"}}}
    runtime = EnvironmentRuntime(config)
    result = {"backend": "carla", "scenario": scenario, "seed": seed, "config": config,
              "execution_passed": False, "completion_passed": False, "actions": [], "episodes": []}
    begin = time.perf_counter()
    try:
        _, initial = runtime.reset(seed=seed)
        route = np.asarray([xyz(transform.location) for transform in runtime.episode.route])
        driver = {"kind": "stop_after_event_trigger"}
        if scenario == "construction_obstacle":
            route, driver = bypass_route(runtime)
        result["driver"] = driver
        result["driver_route_world_rh"] = route.tolist()
        result["scenario_metadata"] = runtime.episode.metadata
        tracker = PathTracker(route)
        info = initial
        terminated = truncated = False
        for step in range(max_steps):
            state = info["events"]
            triggered = (state.get("lead_brake_events", 0) > 0 if scenario == "hard_break_route"
                         else state.get("long_tail_events_triggered", 0) > 0)
            if triggered and scenario != "construction_obstacle":
                action = np.array([0., 0., 1.], np.float32)
            else:
                action = tracker.control(runtime.observer.get_state(runtime.episode.ego), 5.0 if scenario == "construction_obstacle" else 8.0, .2)
            _, _, terminated, truncated, info = runtime.step(action)
            result["actions"].append(action.tolist())
            state = info["events"]
            complete = (state.get("lead_brake_completed", 0) > 0 if scenario == "hard_break_route"
                        else state.get("long_tail_events_completed", 0) > 0)
            if complete or terminated or truncated:
                result["completion_passed"] = bool(complete)
                break
        result["execution_passed"] = True
        result["final_ego_state"] = dict(runtime.observer.get_state(runtime.episode.ego))
        result["collision_events"] = list(runtime.sensors.collisions)
        result["episodes"].append({"source": "scenic", "scenario": scenario, "seed": seed,
            "steps": step + 1, "terminated": terminated, "truncated": truncated,
            "reason": info["termination_reason"], "events": info["events"], "reset_events": initial["events"]})
    except Exception as exc:
        result["error"] = str(exc)
    finally:
        try:
            runtime.shutdown()
        except Exception as exc:
            result["cleanup_error"] = str(exc)
            result["execution_passed"] = False
        result["wall_seconds"] = time.perf_counter() - begin
        path = directory / f"carla-completion-{scenario}.json"
        path.write_text(json.dumps(result, indent=2, default=str) + "\n")
    print(json.dumps({key: result.get(key) for key in ("scenario", "execution_passed", "completion_passed", "error", "wall_seconds")}))
    return result["execution_passed"] and result["completion_passed"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--max-steps", type=int, default=220)
    parser.add_argument("--scenarios", nargs="+", choices=("hard_break_route", "vehicle_opens_door", "construction_obstacle"),
                        default=["hard_break_route", "vehicle_opens_door", "construction_obstacle"])
    parser.add_argument("--report-dir", type=Path, required=True)
    args = parser.parse_args()
    args.report_dir.mkdir(parents=True, exist_ok=True)
    results = [run(scenario, args.port, args.seed, args.max_steps, args.report_dir) for scenario in args.scenarios]
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
