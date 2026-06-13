# CARLA-RL-Base

<p align="center">
  <b>English</b> |
  <a href="README.zh-CN.md">简体中文</a>
</p>

<p align="center">
  <img alt="Python" src="https://img.shields.io/badge/Python-3.10-blue">
  <img alt="CARLA" src="https://img.shields.io/badge/CARLA-0.9.15-orange">
  <img alt="Gymnasium" src="https://img.shields.io/badge/Gymnasium-RL-green">
  <img alt="Scenic" src="https://img.shields.io/badge/Scenic-3.1.1-purple">
</p>

CARLA-RL-Base is a reinforcement learning environment for autonomous driving research in CARLA. It extends a standard CARLA traffic environment with long-tail, Bench2Drive-inspired scenarios so agents can be trained and evaluated under rare but safety-critical interactions.

The project keeps a Gymnasium-compatible interface while adding scenario sampling, route-aware event placement, runtime compatibility checks, and detailed long-tail event statistics.

## Highlights

- **Gymnasium API**: create the environment through `carla-v0` and plug it into standard RL training loops.
- **Multi-modal observations**: LiDAR, RGB camera, BEV semantic view, ego state, nearby vehicles, waypoints, lane info, and GPS targets.
- **Route-aware training**: global routes are generated with CARLA's `GlobalRoutePlanner`.
- **Scenic scenario sampling**: `.scenic` templates control weather, traffic density, route length, and long-tail event families.
- **Long-tail event manager**: one manager handles actor spawning, trigger logic, state updates, cleanup, and per-episode statistics.
- **Map compatibility checks**: events are skipped safely when a sampled route lacks the required adjacent lane, side lane, dynamic-door vehicle, or clear non-junction anchor.

## Long-Tail Scenarios

| Scenario | Scenic file | Description | Runtime requirement |
|---|---|---|---|
| Pedestrian Crossing | `scenarios/scenic/pedestrian_crossing.scenic` | Forced pedestrian crossing and dense pedestrian interaction | Works on most urban routes |
| Hard Brake Route | `scenarios/scenic/hard_break_route.scenic` | A lead vehicle brakes sharply in front of ego | Clear same-lane lead vehicle spawn point |
| Construction Obstacle | `scenarios/scenic/construction_obstacle.scenic` | Cones, warning signs, and debris block the lane | Clear non-junction route anchor |
| Side Cut-In | `scenarios/scenic/side_cut_in.scenic` | Adjacent-lane vehicle cuts into ego's lane | Same-direction adjacent driving lane |
| Parking Cut-In | `scenarios/scenic/parking_cut_in.scenic` | Parked or side-lane vehicle merges into ego's lane | Usable side lane |
| Vehicle Opens Door | `scenarios/scenic/vehicle_opens_door.scenic` | Parked vehicle opens a door into ego's path | Usable side lane and dynamic-door vehicle blueprint |
| Mixed Long Tail | `scenarios/scenic/mixed_long_tail.scenic` | Samples from the supported long-tail events | Selected dynamically by runtime checks |

Long-tail event status is reported in `info`, including:

```text
long_tail_event_spawned
long_tail_event_skipped
long_tail_skip_reason
long_tail_events_triggered
long_tail_events_completed
long_tail_events_failed
long_tail_last_event_type
long_tail_last_event_state
```

## Installation

CARLA server binaries are not installed by this repository. Install and start CARLA 0.9.15 separately before running the environment.

This project assumes the CARLA PythonAPI is available at:

```text
/home/czh/software/carla/PythonAPI/carla
```

Create the Conda environment:

```bash
conda env create -f environment.yml
conda activate CARLA-RL-Base
```

On the original development machine, the existing interpreter can be used directly:

```bash
/home/czh/software/miniconda3/envs/CARLA-RL-Base/bin/python
```

## Quick Start

Start the CARLA server first, then run a scenario smoke test.

Construction obstacle:

```bash
/home/czh/software/miniconda3/envs/CARLA-RL-Base/bin/python scripts/run_long_tail_scenarios.py \
  --episodes 10 \
  --max-steps 1000 \
  --town Town10HD \
  --traffic off \
  --view-mode follow \
  --scenic-file scenarios/scenic/construction_obstacle.scenic
```

Mixed long-tail sampling:

```bash
/home/czh/software/miniconda3/envs/CARLA-RL-Base/bin/python scripts/run_long_tail_scenarios.py \
  --episodes 3 \
  --max-steps 1000 \
  --town Town10HD \
  --traffic off \
  --view-mode follow \
  --scenic-file scenarios/scenic/mixed_long_tail.scenic
```

Run multiple scenario templates in one command:

```bash
/home/czh/software/miniconda3/envs/CARLA-RL-Base/bin/python scripts/run_long_tail_scenarios.py \
  --episodes 5 \
  --max-steps 1000 \
  --town Town10HD \
  --traffic off \
  --scenic-file \
    scenarios/scenic/side_cut_in.scenic \
    scenarios/scenic/parking_cut_in.scenic \
    scenarios/scenic/vehicle_opens_door.scenic
```

## Using the Gym Environment

```python
import gymnasium as gym
import easycarla

params = {
    "number_of_vehicles": 20,
    "number_of_walkers": 5,
    "dt": 0.1,
    "ego_vehicle_filter": "vehicle.tesla.model3",
    "surrounding_vehicle_spawned_randomly": True,
    "port": 2000,
    "carla_timeout": 60.0,
    "town": "Town10HD",
    "max_time_episode": 3000,
    "max_waypoints": 12,
    "visualize_waypoints": False,
    "desired_speed": 8,
    "max_ego_spawn_times": 200,
    "ego_autopilot": True,
    "view_mode": "follow",
    "traffic": "off",
    "lidar_max_range": 50.0,
    "max_nearby_vehicles": 5,
    "scenario_backend": "scenic",
    "scenic_file": "scenarios/scenic/mixed_long_tail.scenic",
    "scenic_max_iterations": 2000,
    "scenic_mode2d": False,
    "long_tail_event_max_active": 1,
}

env = gym.make("carla-v0", params=params)
obs, info = env.reset()

done = False
while not done:
    obs, reward, terminated, truncated, info = env.step([0.0, 0.0])
    done = terminated or truncated

env.close()
```

## Scenario Design Notes

Many Bench2Drive scenarios rely on hand-picked route trigger points. This project keeps randomized RL resets, so it does not force every event to appear on every map.

Examples:

- `side_cut_in` is skipped with `no_same_direction_adjacent_lane` if the sampled route has no valid same-direction adjacent lane.
- `parking_cut_in` and `vehicle_opens_door` are skipped with `no_side_lane` if no usable side lane exists.
- `construction_obstacle` is skipped with `no_clear_route_anchor` if no clear non-junction route anchor can be found.

This keeps training stable: incompatible long-tail events are skipped while the episode can continue as a normal CARLA route.

## Project Structure

```text
easycarla/
  envs/
    carla_env_test.py          # Gymnasium CARLA environment
  scenarios/
    providers.py               # random / scenic scenario providers
    long_tail_events.py        # long-tail event manager

scenarios/scenic/
  pedestrian_crossing.scenic
  hard_break_route.scenic
  construction_obstacle.scenic
  side_cut_in.scenic
  parking_cut_in.scenic
  vehicle_opens_door.scenic
  mixed_long_tail.scenic

scripts/
  run_long_tail_scenarios.py   # smoke test / scenario runner
```

## Git Workflow

Common workflow:

```bash
git status
git add .
git commit -m "Describe your change"
git push
```

Pull the latest version from GitHub:

```bash
git pull
```

## Roadmap

- Preferentially sample routes that can support side-dependent events.
- Add more Bench2Drive-style junction scenarios, such as priority violation, red-light running, and bicycle flow.
- Add finer-grained reward and cost statistics for long-tail interactions.
- Add reproducible experiment configs and batch evaluation utilities.

