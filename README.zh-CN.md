# CARLA-RL-Base

<p align="center">
  <a href="README.md">English</a> |
  <b>简体中文</b>
</p>

<p align="center">
  <img alt="Python" src="https://img.shields.io/badge/Python-3.10-blue">
  <img alt="CARLA" src="https://img.shields.io/badge/CARLA-0.9.15-orange">
  <img alt="Gymnasium" src="https://img.shields.io/badge/Gymnasium-RL-green">
  <img alt="Scenic" src="https://img.shields.io/badge/Scenic-3.1.1-purple">
</p>

面向自动驾驶强化学习的 CARLA 环境，重点扩展真实复杂道路中的长尾场景，用于提升模型在突发交互、遮挡、施工、路侧风险等复杂环境下的泛化能力。

本项目在普通 CARLA 随机交通环境基础上，加入了 Bench2Drive 风格的长尾事件，并保留 Gymnasium 接口，方便接入 RL 训练代码。

## 项目亮点

- **Gymnasium 环境接口**：通过 `carla-v0` 创建环境，兼容标准 RL 训练流程。
- **多模态观测**：支持 LiDAR、RGB camera、BEV semantic、ego state、nearby vehicles、waypoints、lane info、GPS targets。
- **路线规划**：基于 CARLA `GlobalRoutePlanner` 自动生成全局路线和目标点。
- **Scenic 场景采样**：通过 `.scenic` 模板控制天气、交通密度、路线长度和长尾事件。
- **长尾事件管理器**：统一管理 actor 生成、触发、状态更新、统计和清理。
- **地图兼容性检测**：对相邻车道、侧向车道、动态车门车辆、非路口 anchor 等条件进行检查；不适配时跳过，不让 `reset()` 崩溃。

## 长尾场景

| 场景 | Scenic 文件 | 描述 | 运行时要求 |
|---|---|---|---|
| Pedestrian Crossing | `scenarios/scenic/pedestrian_crossing.scenic` | 行人横穿、强制前方横穿 | 多数城市场景可用 |
| Hard Brake Route | `scenarios/scenic/hard_break_route.scenic` | 前车急刹车 | ego 前方可生成 lead vehicle |
| Construction Obstacle | `scenarios/scenic/construction_obstacle.scenic` | 施工锥、警示牌、碎片阻挡车道 | 清晰的非路口 route anchor |
| Side Cut-In | `scenarios/scenic/side_cut_in.scenic` | 相邻车道车辆切入 ego 前方 | 同向相邻 driving lane |
| Parking Cut-In | `scenarios/scenic/parking_cut_in.scenic` | 路侧/停车车辆切入主车道 | 可用侧向 lane |
| Vehicle Opens Door | `scenarios/scenic/vehicle_opens_door.scenic` | 路侧车辆开门占道 | 可用侧向 lane 和动态车门车辆蓝图 |
| Mixed Long Tail | `scenarios/scenic/mixed_long_tail.scenic` | 混合采样多个长尾事件 | 根据运行时道路条件自动选择 |

运行时会在 `info` 中返回长尾事件统计，例如：

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

## 安装

CARLA server 需要单独安装。本项目环境文件假设 CARLA 0.9.15 已安装在本机，并且 PythonAPI 路径为：

```text
/home/czh/software/carla/PythonAPI/carla
```

创建 Conda 环境：

```bash
conda env create -f environment.yml
conda activate CARLA-RL-Base
```

如果使用当前机器上已有环境，可以直接使用：

```bash
/home/czh/software/miniconda3/envs/CARLA-RL-Base/bin/python
```

## 快速开始

先启动 CARLA server，然后运行长尾场景测试脚本。

测试施工障碍：

```bash
/home/czh/software/miniconda3/envs/CARLA-RL-Base/bin/python scripts/run_long_tail_scenarios.py \
  --episodes 10 \
  --max-steps 1000 \
  --town Town10HD \
  --traffic off \
  --view-mode follow \
  --scenic-file scenarios/scenic/construction_obstacle.scenic
```

测试混合长尾场景：

```bash
/home/czh/software/miniconda3/envs/CARLA-RL-Base/bin/python scripts/run_long_tail_scenarios.py \
  --episodes 3 \
  --max-steps 1000 \
  --town Town10HD \
  --traffic off \
  --view-mode follow \
  --scenic-file scenarios/scenic/mixed_long_tail.scenic
```

一次测试多个场景：

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

## 使用 Gym 环境

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

## 场景设计说明

Bench2Drive 的很多长尾场景依赖手工挑选的 route trigger point。本项目保持随机 RL reset，因此不会强行在所有地图上生成所有事件。

例如：

- `side_cut_in` 如果当前路线前方没有同向相邻车道，会跳过并返回 `no_same_direction_adjacent_lane`。
- `parking_cut_in` 和 `vehicle_opens_door` 如果没有侧向 lane，会跳过并返回 `no_side_lane`。
- `construction_obstacle` 如果前方没有清晰可用的非路口 anchor，会跳过并返回 `no_clear_route_anchor`。

这种策略可以保证训练过程稳定：场景不适配时继续运行普通环境，而不是让 episode 初始化失败。

## 项目结构

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

## Git 工作流

常用同步流程：

```bash
git status
git add .
git commit -m "Describe your change"
git push
```

从 GitHub 拉取最新代码：

```bash
git pull
```

## Roadmap

- 增加更稳定的 route sampling，让 side-dependent 场景优先选择合适车道。
- 加入更多 Bench2Drive 风格交叉口场景，如对向车抢行、闯红灯、自行车流。
- 为长尾事件增加更细粒度的 reward/cost 统计。
- 增加可复现实验配置和批量评估脚本。

