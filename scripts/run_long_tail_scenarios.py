import argparse
from collections import Counter
from pathlib import Path
import sys

import gymnasium as gym

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import easycarla  # noqa: F401


DEFAULT_SCENARIO_NAME = "pedestrian_crossing"
DEFAULT_SCENIC_FILE = str((PROJECT_ROOT / "scenarios" / "scenic" / "pedestrian_crossing.scenic").resolve())
DEFAULT_SCENIC_FILES = [DEFAULT_SCENIC_FILE]


def parse_args():
    parser = argparse.ArgumentParser(description="Run pedestrian-crossing long-tail scenic tests.")
    parser.add_argument("--episodes", type=int, default=3)
    parser.add_argument("--max-steps", type=int, default=2000)
    parser.add_argument("--port", type=int, default=2000)
    parser.add_argument("--town", type=str, default="Town01")
    parser.add_argument("--view-mode", type=str, default="follow", choices=["top", "follow"])
    parser.add_argument("--traffic", type=str, default="off", choices=["on", "off"])
    parser.add_argument("--scenic-max-iterations", type=int, default=2000)
    parser.add_argument("--scenic-file", type=str, nargs="+", default=DEFAULT_SCENIC_FILES)
    return parser.parse_args()


def resolve_scenic_file(path_value: str) -> str:
    raw_path = Path(path_value).expanduser()
    if raw_path.is_absolute():
        return str(raw_path.resolve())
    return str((PROJECT_ROOT / raw_path).resolve())


def build_base_params(args):
    return {
        "number_of_vehicles": 0,
        "number_of_walkers": 0,
        "dt": 0.1,
        "ego_vehicle_filter": "vehicle.tesla.model3",
        "surrounding_vehicle_spawned_randomly": True,
        "port": args.port,
        "carla_timeout": 60.0,
        "town": args.town,
        "max_time_episode": max(args.max_steps * 3, 600),
        "max_waypoints": 12,
        "visualize_waypoints": False,
        "desired_speed": 8,
        "max_ego_spawn_times": 200,
        "ego_autopilot": True,
        "view_mode": args.view_mode,
        "traffic": args.traffic,
        "lidar_max_range": 50.0,
        "max_nearby_vehicles": 5,
        "scenario_backend": "scenic",
        "scenic_max_iterations": args.scenic_max_iterations,
        "scenic_mode2d": False,
    }


def actor_is_alive(actor):
    if actor is None:
        return False
    try:
        return bool(actor.is_alive)
    except Exception:
        return False


def run_one_scenario(name, scenic_file, args):
    params = build_base_params(args)
    params["scenic_file"] = scenic_file

    print("\n" + "=" * 90)
    print(f"Scenario: {name}")
    print(f"Scenic file: {scenic_file}")
    print(f"Episodes: {args.episodes}, Max steps: {args.max_steps}")
    print("=" * 90)

    env = gym.make("carla-v0", params=params)
    base_env = env.unwrapped

    backend_counter = Counter()
    route_reason_counter = Counter()
    collision_eps = 0
    offroad_eps = 0
    truncated_eps = 0
    long_tail_spawned_eps = 0
    long_tail_skipped_eps = 0
    long_tail_completed_eps = 0
    total_reward_sum = 0.0
    total_steps_sum = 0

    try:
        for episode in range(args.episodes):
            obs, reset_info = env.reset()
            backend = reset_info.get("scenario_backend", "unknown")
            route_reason = reset_info.get("route_plan_reason", "unknown")
            scenario_metadata = reset_info.get("scenario_metadata", {})
            ped_cross = reset_info.get("pedestrian_cross_factor", None)
            walker_speed_min = reset_info.get("walker_speed_min", None)
            walker_speed_max = reset_info.get("walker_speed_max", None)
            forced_crossing_walkers = reset_info.get("forced_crossing_walkers", None)
            forced_crossing_distance_min = reset_info.get("forced_crossing_distance_min", None)
            forced_crossing_distance_max = reset_info.get("forced_crossing_distance_max", None)
            forced_crossing_prob = reset_info.get("forced_crossing_trigger_prob", None)
            forced_crossing_cooldown = reset_info.get("forced_crossing_cooldown_steps", None)
            forced_crossing_max_total = reset_info.get("forced_crossing_max_total", None)
            lead_brake_enabled = reset_info.get("lead_brake_enabled", False)
            lead_candidate_distance_min = reset_info.get("lead_candidate_distance_min", None)
            lead_candidate_distance_max = reset_info.get("lead_candidate_distance_max", None)
            lead_candidate_speed_min = reset_info.get("lead_candidate_speed_min", None)
            lead_candidate_speed_max = reset_info.get("lead_candidate_speed_max", None)
            lead_trigger_distance_min = reset_info.get("lead_trigger_distance_min", None)
            lead_trigger_distance_max = reset_info.get("lead_trigger_distance_max", None)
            lead_brake_hold_steps = reset_info.get("lead_brake_hold_steps", None)
            lead_brake_strength = reset_info.get("lead_brake_strength", None)
            lead_brake_min_ego_speed = reset_info.get("lead_brake_min_ego_speed", None)
            hard_break_enabled = reset_info.get("hard_break_route_enabled", False)
            hard_break_spawned = reset_info.get("hard_break_lead_spawned", False)
            hard_break_vehicle_id = reset_info.get("hard_break_lead_vehicle_id", None)
            hard_break_spawn_distance = reset_info.get("hard_break_lead_spawn_distance", None)
            hard_break_spawn_failure = reset_info.get("hard_break_lead_spawn_failure", "")
            long_tail_configured = reset_info.get("long_tail_events_configured", [])
            long_tail_spawned = reset_info.get("long_tail_event_spawned", 0)
            long_tail_skipped = reset_info.get("long_tail_event_skipped", 0)
            long_tail_skip_reason = reset_info.get("long_tail_skip_reason", "")
            backend_counter[backend] += 1
            route_reason_counter[route_reason] += 1

            if actor_is_alive(base_env.ego):
                try:
                    base_env.ego.set_autopilot(True)
                    base_env.world.tick()
                except RuntimeError:
                    pass

            done = False
            step_count = 0
            total_reward = 0.0
            last_info = {}
            last_terminated = False
            last_truncated = False

            while not done and step_count < args.max_steps:
                if not actor_is_alive(base_env.ego):
                    done = True
                    last_terminated = True
                    last_info = {
                        "is_collision": True,
                        "is_off_road": False,
                        "ego_destroyed": True,
                        "ego_destroyed_reason": "script_detected_before_action",
                    }
                    break
                obs, reward, terminated, truncated, info = env.step([0.0, 0.0])
                done = terminated or truncated
                step_count += 1
                total_reward += reward
                last_info = info
                last_terminated = terminated
                last_truncated = truncated

            if actor_is_alive(base_env.ego):
                try:
                    base_env.ego.set_autopilot(False)
                    base_env.world.tick()
                except RuntimeError:
                    pass

            if last_info.get("is_collision", False):
                collision_eps += 1
            if last_info.get("is_off_road", False):
                offroad_eps += 1
            if last_truncated or (step_count >= args.max_steps and not last_terminated):
                truncated_eps += 1
            if long_tail_spawned:
                long_tail_spawned_eps += 1
            if long_tail_skipped:
                long_tail_skipped_eps += 1
            if last_info.get("long_tail_events_completed", 0) > 0:
                long_tail_completed_eps += 1

            total_reward_sum += total_reward
            total_steps_sum += step_count

            print(
                f"[{name}] ep={episode:02d} backend={backend} route={route_reason} "
                f"steps={step_count} reward={total_reward:.2f} "
                f"collision={last_info.get('is_collision', False)} "
                f"offroad={last_info.get('is_off_road', False)} "
                f"ped_cross={ped_cross} walker_speed=[{walker_speed_min},{walker_speed_max}] "
                f"forced_crossing_total={last_info.get('forced_crossing_spawned_total', 0)} "
                f"events={last_info.get('forced_crossing_events', 0)} "
                f"target=[1,{forced_crossing_walkers}] "
                f"max_total={forced_crossing_max_total} "
                f"distance=[{forced_crossing_distance_min},{forced_crossing_distance_max}] "
                f"trigger_prob={forced_crossing_prob} cooldown={forced_crossing_cooldown} "
                f"disabled={last_info.get('forced_crossing_disabled', False)} "
                f"disable_reason={last_info.get('forced_crossing_disable_reason', '')} "
                f"lead_enabled={lead_brake_enabled} "
                f"lead_state={last_info.get('lead_brake_last_state', 'idle')} "
                f"lead_triggered={last_info.get('lead_brake_triggered', False)} "
                f"lead_events={last_info.get('lead_brake_events', 0)} "
                f"lead_completed={last_info.get('lead_brake_completed', 0)} "
                f"lead_aborted={last_info.get('lead_brake_aborted', 0)} "
                f"lead_trigger_step={last_info.get('lead_brake_last_trigger_step', None)} "
                f"lead_trigger_ego_speed={last_info.get('lead_brake_trigger_ego_speed', None)} "
                f"lead_distance={last_info.get('lead_brake_last_distance', None)} "
                f"lead_rel_speed={last_info.get('lead_brake_last_relative_speed', None)} "
                f"lead_candidate={last_info.get('lead_brake_last_candidate_id', None)} "
                f"lead_candidate_window=[{lead_candidate_distance_min},{lead_candidate_distance_max}] "
                f"lead_candidate_speed=[{lead_candidate_speed_min},{lead_candidate_speed_max}] "
                f"lead_trigger_window=[{lead_trigger_distance_min},{lead_trigger_distance_max}] "
                f"lead_brake=[strength={lead_brake_strength},hold={lead_brake_hold_steps},min_ego_speed={lead_brake_min_ego_speed}] "
                f"hard_break_enabled={hard_break_enabled} "
                f"hard_break_spawned={hard_break_spawned} "
                f"hard_break_vehicle={hard_break_vehicle_id} "
                f"hard_break_spawn_distance={hard_break_spawn_distance} "
                f"hard_break_spawn_failure={hard_break_spawn_failure} "
                f"long_tail_configured={long_tail_configured} "
                f"long_tail_spawned={long_tail_spawned} "
                f"long_tail_skipped={long_tail_skipped} "
                f"long_tail_skip_reason={long_tail_skip_reason} "
                f"long_tail_active={last_info.get('long_tail_events_active', 0)} "
                f"long_tail_triggered={last_info.get('long_tail_events_triggered', 0)} "
                f"long_tail_completed={last_info.get('long_tail_events_completed', 0)} "
                f"long_tail_failed={last_info.get('long_tail_events_failed', 0)} "
                f"long_tail_last={last_info.get('long_tail_last_event_type', '')}:"
                f"{last_info.get('long_tail_last_event_state', '')}:"
                f"{last_info.get('long_tail_last_event_reason', '')} "
                f"meta_scenario={scenario_metadata.get('scenario_name', 'n/a')}"
            )
    finally:
        env.close()

    episodes = max(args.episodes, 1)
    return {
        "name": name,
        "episodes": args.episodes,
        "avg_steps": total_steps_sum / episodes,
        "avg_reward": total_reward_sum / episodes,
        "collision_eps": collision_eps,
        "offroad_eps": offroad_eps,
        "truncated_eps": truncated_eps,
        "long_tail_spawned_eps": long_tail_spawned_eps,
        "long_tail_skipped_eps": long_tail_skipped_eps,
        "long_tail_completed_eps": long_tail_completed_eps,
        "backend_counter": dict(backend_counter),
        "route_reason_counter": dict(route_reason_counter),
    }


def main():
    args = parse_args()
    summaries = []
    scenic_files = args.scenic_file
    if isinstance(scenic_files, str):
        scenic_files = [scenic_files]
    for scenic_file_arg in scenic_files:
        scenic_file = resolve_scenic_file(scenic_file_arg)
        scenario_name = Path(scenic_file).stem if scenic_file else DEFAULT_SCENARIO_NAME
        summaries.append(run_one_scenario(scenario_name, scenic_file, args))

    print("\n" + "#" * 90)
    print("Long-tail scenario summary")
    print("#" * 90)
    for summary in summaries:
        print(
            f"{summary['name']}: episodes={summary['episodes']} avg_steps={summary['avg_steps']:.1f} "
            f"avg_reward={summary['avg_reward']:.2f} collision_eps={summary['collision_eps']} "
            f"offroad_eps={summary['offroad_eps']} truncated_eps={summary['truncated_eps']} "
            f"long_tail_spawned_eps={summary['long_tail_spawned_eps']} "
            f"long_tail_skipped_eps={summary['long_tail_skipped_eps']} "
            f"long_tail_completed_eps={summary['long_tail_completed_eps']} "
            f"backend={summary['backend_counter']} route_reason={summary['route_reason_counter']}"
        )


if __name__ == "__main__":
    main()
