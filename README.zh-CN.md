# CARLA-RL-Base / EasyCARLA

[English](README.md) | 简体中文

**一个面向强化学习和交互研究的 CARLA Gymnasium 环境平台。** 环境负责仿真、场景、观测、动作执行、奖励和回合管理；你的策略、模型和训练器在独立工程中运行，通过同机共享内存连接环境。

仓库名是 `CARLA-RL-Base`，服务端包和命令名是 `easycarla`，独立客户端包名是 `easycarla-client`，Python 导入名是 `easycarla_client`。

第一次使用，建议依次完成：**安装两端 → 跑通 fake 后端 → 接入单个 CARLA → 使用 Gymnasium → 按需启用并行环境**。不必先准备 Bench2Drive，也不必安装模型或训练库。

## 阅读导航

- [项目结构与已有功能](#项目结构与已有功能)
- [运行条件与安装](#运行条件与安装)
- [第一步：不启动 CARLA，先跑通通信](#第一步不启动-carla先跑通通信)
- [第二步：运行一个真实 CARLA 环境](#第二步运行一个真实-carla-环境)
- [第三步：接入 Gymnasium](#第三步接入-gymnasium)
- [第四步：无渲染并行采样](#第四步无渲染并行采样)
- [观测、时间与轨迹动作](#观测时间与轨迹动作)
- [选择场景与配置](#选择场景与配置)
- [故障处理与常见问题](#故障处理与常见问题)
- [测试、实测范围与扩展入口](#测试实测范围与扩展入口)

## 项目结构与已有功能

```text
你的策略 / 训练工程（自己的 Python 环境）
        │
        └── easycarla-client：原始客户端 / Gymnasium / 向量接口
                    │  Linux 共享内存 + 命名信号量
             ┌──────┴──────┐
        环境服务 0       环境服务 1       …
             │               │
          CARLA 0          CARLA 1
```

一个实例由一个环境服务和一个独占的 CARLA 世界组成。每个实例同时只有一个控制客户端；并行采样通过多个独立 CARLA 进程实现。策略计算期间，世界等待下一条请求，不自行推进。

| 能力 | 当前实现 |
|---|---|
| 场景 | Scenic、Bench2Drive、按明确配置进行回合级来源混合 |
| Scenic 长尾事件 | 行人横穿、前车急刹、停车切入、车辆开门、施工障碍；记录生成、触发、完成和跳过原因 |
| 动作 | 三维车辆控制、外部轨迹跟踪，可指定 actor / 后轴参考点 |
| 观测 | 结构化真值、对象历史、路线、信号、缓存地图；可选 `planning_v1` 车辆几何、自车历史、车道拓扑和路线走廊 |
| 传感器 | 可选 RGB、语义相机 BEV、LiDAR；高效无渲染配置关闭三者 |
| 任务 | 通用驾驶奖励与终止规则、独立 Bench2Drive 原生评估 profile；RL 奖励与原生指标分别报告 |
| 并行与运行管理 | 独立 worker、fleet 启停和显式重启、Gymnasium 向量接口、按完成顺序消费结果 |
| 实验扩展 | 可选 NPC 反应控制、机制切换、公共前缀记录 / 重建 / 重演及误差比较 |

环境不包含 PPO、经验回放、模型推理或 Flow Planner。`planning_v1` 提供通用数据及外部字段映射；模型格式适配、归一化和驾驶效果验证属于你的策略工程。结构化状态来自 CARLA 真值，不等同于视觉感知输出或 Bench2Drive 传感器赛道输入。

## 运行条件与安装

| 部分 | 建议环境 | 安装内容 |
|---|---|---|
| 操作系统 | Linux，两端运行在同一机器 | POSIX 共享内存与命名信号量；当前接口不是跨机器网络接口 |
| 环境服务 | Python 3.10 | 本仓库和独立客户端；真实仿真再安装 CARLA / Scenic，可选 Bench2Drive 依赖 |
| CARLA 服务器 | 官方 CARLA 0.9.15 | 单独安装服务器、对应地图和 `PythonAPI/carla/agents`；渲染需要可用 GPU 驱动 |
| 外部客户端 | 示例使用 Python 3.11，包要求 Python ≥3.10 | NumPy、`posix_ipc`；Gymnasium 可选；不需要 CARLA、Scenic 或 Torch |

以下命令从仓库根目录执行。若已下载项目，直接进入现有目录：

```bash
git clone https://github.com/zhuohangcai63-cloud/CARLA-RL-Base.git
cd CARLA-RL-Base
```

### 安装服务端

先安装可运行 fake 后端的基础服务：

```bash
conda env create -f environments/service.yml
conda activate easycarla-service
python -m pip install -c environments/constraints-service.txt \
  -e ./packages/client -e '.[test]'
python -m pip check
easycarla --help
```

准备运行真实 Scenic 环境时，在同一服务环境追加：

```bash
python -m pip install -c environments/constraints-service.txt -e '.[carla,test]'
```

`carla` extra 安装 CARLA 0.9.15 Python API 和 Scenic 3.1.1，**不下载 CARLA 服务器或地图**。`environments/*.yml` 只创建基础 Conda 环境，后续 pip 安装不可省略。已有服务环境可以继续使用，不必重复创建；旧 `environment.yml` 保留兼容入口。

### 安装独立客户端

另开终端，从同一仓库目录执行：

```bash
conda env create -f environments/client.yml
conda activate easycarla-client
python -m pip install -c environments/constraints-client.txt ./packages/client
python -c "from easycarla_client import CarlaEnvClient; print('client import OK')"
```

你的训练工程只需要安装 `packages/client`。需要 Gymnasium 时，改为或追加安装：

```bash
python -m pip install -c environments/constraints-client.txt './packages/client[gym]'
```

无需在训练环境安装整个 `easycarla` 服务包。换目录后可使用客户端包的绝对路径安装，也支持 wheel 分发。完整依赖、路径解析和安装说明见 [installation.md](docs/installation.md)。

## 第一步：不启动 CARLA，先跑通通信

这一步验证安装、跨进程连接及 `reset/step/close`。**fake 后端没有驾驶物理、真实场景或规划观测**，无需 GPU、CARLA 或 Bench2Drive。

终端 A，服务环境，保持进程运行：

```bash
conda activate easycarla-service
easycarla serve --instance protocol_test --config configs/fake.yaml
```

终端 B，客户端环境：

```bash
conda activate easycarla-client
python examples/client_loop.py --instance protocol_test --episodes 1 --steps 5
```

成功时会输出一行 `reset` 和五行 `step`，frame 依次为 4、8、12、16、20。示例结束会释放客户端连接，服务仍运行；在终端 A 按 Ctrl-C 停止服务。若这一步失败，先解决包安装或 IPC 问题，再排查真实仿真。

## 第二步：运行一个真实 CARLA 环境

手动运行需要 **三个进程**：CARLA 服务器、环境服务、客户端。`serve` 接入已有服务器；自动管理服务器的方式在后面的 fleet 章节。

### 1. 检查路径与配置

安装好 CARLA 0.9.15 及 Town10HD 后，打开 [configs/scenic.yaml](configs/scenic.yaml)，核对：

- `simulation.carla_root`：实际 CARLA 安装目录，示例为 `~/software/carla`。
- `simulation.port`：CARLA RPC 端口，示例为 `2000`。
- `simulation.tm_port`：本实例独占的 Traffic Manager 端口，示例为 `8000`。
- `scenario.town` 与 `scenario.file`：示例为 Town10HD 和 Scenic 长尾场景。

显式 `simulation.carla_root` 优先于环境变量 `CARLA_ROOT`，再缺省为 `~/software/carla`。如果 YAML 已写了路径，仅修改环境变量不会覆盖它。服务通过该目录找到 CARLA agents，无需在客户端设置 `PYTHONPATH`。

### 2. 终端 A：启动 CARLA

```bash
# 将路径换成自己的 CARLA 安装目录
~/software/carla/CarlaUE4.sh \
  -RenderOffScreen -nosound -quality-level=Epic -carla-rpc-port=2000
```

`-RenderOffScreen` 仍然渲染，只是不显示窗口；它不是纯结构化无渲染模式。此例使用 `Epic`，因为本机曾在 `Low` 下复现 Town03 Vulkan 崩溃；该选择不保证所有地图稳定。纯结构化并行训练使用后面的 `-nullrhi` fleet 流程。

### 3. 终端 B：启动环境服务

```bash
conda activate easycarla-service
easycarla serve --instance carla_01 --config configs/scenic.yaml
```

地图和场景初始化由客户端 `reset()` 触发；服务启动成功不等于回合已经准备好。

### 4. 终端 C：连接并步进

```bash
conda activate easycarla-client
python examples/client_loop.py --instance carla_01 --episodes 1 --steps 10 --timeout 180
```

也可以在你的外部工程使用原始客户端：

```python
from easycarla_client import CarlaEnvClient

with CarlaEnvClient("carla_01", timeout=180) as env:
    obs, info = env.reset(seed=7)
    static_map = env.get_static(obs["map_id"])
    for _ in range(10):
        # 简单通信示例，不是能沿路线驾驶的策略
        obs, reward, terminated, truncated, info = env.step([0.0, 0.15, 0.0])
        print(obs["frame"], reward, terminated, truncated)
        if terminated or truncated:
            break
```

客户端库默认请求超时为 30 秒；联调脚本默认 180 秒，适合地图冷启动。客户端超时覆盖整个请求，服务的 `simulation.timeout` 则是 CARLA RPC 等待设置，两者用途不同。

结束时先退出客户端，再 Ctrl-C 停止环境服务，最后停止手动启动的 CARLA。`env.close()` 只结束本客户端会话及回合，不停止环境服务或外部 CARLA。

## 第三步：接入 Gymnasium

安装客户端 `[gym]` extra 后，可直接创建 `CarlaGymEnv`，无需 `gym.make()` 或导入服务端模块：

```python
from easycarla_client import CarlaGymEnv

with CarlaGymEnv("carla_01", timeout=180) as env:
    print(env.observation_space, env.action_space)
    obs, info = env.reset(seed=7)
    for _ in range(10):
        obs, reward, terminated, truncated, info = env.step([0.0, 0.15, 0.0])
        if terminated or truncated:
            break  # 继续下一回合时，显式调用 env.reset()
```

| 接口 | 观测格式 | 适合用途 |
|---|---|---|
| `CarlaEnvClient` | 原始嵌套字典，例如 `obs["ego"]["velocity"]` | 自定义策略适配、完整查询、低依赖客户端 |
| `CarlaGymEnv` | 固定形状数值 / 掩码 Dict，例如 `obs["ego.velocity"]` | Gymnasium 训练工程 |
| `CarlaVectorEnv` | 在单环境固定字段上增加 worker 维度 | 同步批量采样 |
| `ClientPool` | worker ID 与对应原始结果，按完成顺序消费 | 外部采样器自行处理慢 worker |

Gym 中的字符串元数据放在 `info`，如 `map_id/route_id`；用 `env.get_static(info["map_id"])`、`env.get_route(info["route_id"])` 查询。space 由服务在构造时发布，之后保持固定。不要混用原始客户端的嵌套键与 Gym 的点路径键。

## 第四步：无渲染并行采样

先完成单实例联调。fleet 为每个 worker 启动独立 CARLA 和服务，使用 `-nullrhi -nosound`，并验证 `no_rendering_mode=true`。高效 worker 关闭 RGB、语义 BEV 和 LiDAR。

### 配置实例池

在服务环境复制示例，副本保留在相同目录，以保持 worker 配置相对路径有效：

```bash
conda activate easycarla-service
cp configs/fast/fleet.yaml configs/fast/fleet.local.yaml
python -c "import sys; print(sys.executable)"
```

编辑 `configs/fast/fleet.local.yaml`：

| 字段 | 需要确认的内容 |
|---|---|
| `service_python` | 上一条命令输出的服务环境解释器绝对路径，不使用客户端解释器 |
| `carla_root` | CARLA 0.9.15 安装目录；worker 的安装路径应与其一致 |
| `log_dir` | supervisor、CARLA、服务日志和持久状态所在目录 |
| `workers[].config` | 示例两个 worker 均使用 `scenic_train.yaml`，固定 Town10HD |
| `workers[].id / instance` | 管理 ID / 连接名：`worker_0 / planning_0`、`worker_1 / planning_1` |
| `workers[].ports` | 每 worker 独占 RPC、streaming、secondary、TM 四个端口，实例间不能重复 |
| 内存和线程预算 | 默认每实例估计 6000 MiB、预留 4096 MiB、数值库线程 1；按机器调整 |

示例 `/home/czh/...` 是本机路径，换机器必须修改。内存预算是启动准入估计，不是硬上限；先运行 1～2 个 worker，不要直接扩到 4 个。

```bash
conda activate easycarla-service
easycarla fleet start --config configs/fast/fleet.local.yaml
easycarla fleet status --config configs/fast/fleet.local.yaml
```

### 从客户端并行提交

```python
import numpy as np
from easycarla_client import CarlaVectorEnv

env = CarlaVectorEnv(["planning_0", "planning_1"], timeout=180)
try:
    obs, infos = env.reset(seed=7)  # worker i 使用 seed+i，也可传种子列表
    actions = np.tile([0.0, 0.0, 1.0], (env.num_envs, 1)).astype(np.float32)
    env.step_async(actions)
    obs, rewards, terminated, truncated, infos = env.step_wait()
    done = terminated | truncated
    if done.any():
        obs, infos = env.reset(mask=done)  # 保留未重置 worker 的最后观测
finally:
    env.close()
```

同一 vector 的观测规格、动作规格和时钟必须一致。每 worker 最多一个请求在途；整批输入先完成本地检查，再发布。默认没有自动 reset，终态观测会保留。同步 vector 等待整批完成；需要按完成顺序处理快慢实例时，使用 [ClientPool](docs/parallel-environments.md#完成顺序与故障)。

### 重启和退出

先关闭对应客户端，再显式重启故障 worker，随后重新连接并 reset：

```bash
easycarla fleet restart --config configs/fast/fleet.local.yaml --worker worker_0
easycarla fleet stop --config configs/fast/fleet.local.yaml
```

fleet 只管理自己登记的进程组，不停止手动启动的 CARLA。`status/stop` 不要求 worker 配置、解释器或 CARLA 安装仍存在；fleet 中的身份、日志位置和持久进程记录仍需保留。`restart` 在停止旧 worker 前验证新配置，不重放未知动作。

官方 CARLA 0.9.15 不支持 CaRL 定制的 RPC / streaming / secondary 线程限制参数。预算会记录，状态明确报告未应用；服务数值库线程预算实际设置。默认不启用 `-nothreading`。详见 [并行运行说明](docs/parallel-environments.md)。

## 观测、时间与轨迹动作

### 控制与回合结果

`step()` 返回 `(obs, reward, terminated, truncated, info)`。reward 为窗口内逐物理 tick 奖励之和；`terminated` 是任务自然结束，`truncated` 是任务时间截断。模拟器崩溃、缺失必需传感器或结果未知抛出异常，不伪装成 truncated 样本。

动作顺序为 `[steer, throttle, brake]`：steer 范围 `[-1,1]`，**正值右转**；throttle / brake 范围 `[0,1]`。禁止同时正油门和正刹车；非法形状、NaN/Inf 和越界不静默裁剪。归一化 steer 不是弧度转角。

| 配置 | 物理步长 | 决策窗口 | 历史采样 |
|---|---:|---:|---:|
| 原有默认配置 | 0.05 s | 0.2 s，通常 4 tick | 0.1 s |
| `configs/fast/` | 0.05 s | 0.1 s，通常 2 tick | 0.1 s |

终止时提前结束窗口，实际执行量见 `info.physical_ticks` 和 `info.executed_seconds`。地图、路线、能力查询及客户端推理不推进世界。

### legacy 与 planning_v1

默认 `legacy` 保留 actor 原点语义。`planning_v1` 增量提供真实自车历史、后轴几何、包围盒中心对象历史、自行车 / 摩托车区分、静态物体、连续有向车道段和路线走廊。

规划字段以**当前自车后轴**为原点，x 前、y 左；历史统一转换到当前参考系。位置 / 尺寸用 m，速度 m/s，加速度 m/s²，角度 rad，yaw rate rad/s；尺寸顺序是 `[长,宽,高]`。邻居速度是绝对速度的坐标旋转，不减自车速度。旧字段语义不变。

21 帧、0.1 s 间隔对应过去 2 秒加当前帧。**reset 不隐式驾驶两秒，也不复制状态补齐缺失历史**。通过正常 `step/step_trajectory` 准备历史，检查 `planning.history_ready`、覆盖时长和有效掩码；准备期间奖励、事件、终止正常生效。Gym 示例：

```python
from easycarla_client import CarlaGymEnv

# planning_0 服务采用 planning_v1，并且正在运行
with CarlaGymEnv("planning_0", timeout=180) as env:
    obs, info = env.reset(seed=7)
    while not bool(obs["planning.history_ready"]):
        obs, reward, terminated, truncated, info = env.step([0.0, 0.0, 1.0])
        if terminated or truncated:
            break  # 本回合已结束，不能继续交给规划器
    print("history_ready:", bool(obs["planning.history_ready"]))
```

历史已满不代替几何 / 运动有效性检查。地图和路线按 `map_id/route_id` 查询缓存，不随 step 重传。车道边界是 waypoint 宽度估计；CARLA 路线走廊不是 nuPlan roadblock 的等价表达。字段、容量、掩码和 Flow 映射见 [观测接口](docs/observations.md)及 [planning_v1 契约](docs/planning-v1.md)。

### 使用外部轨迹

原始客户端支持 `step_trajectory()`。下面是停住车辆的接口示例，需要真实 CARLA 后端，并非规划器：

```python
import numpy as np
from easycarla_client import CarlaEnvClient

with CarlaEnvClient("carla_01", timeout=180) as env:
    obs, info = env.reset(seed=7)
    trajectory = {
        "reference_frame": int(obs["frame"]),
        "reference_point": "actor",  # rear_axle 需 planning_v1 和有效实际几何
        "times": np.array([0.0, 0.1, 0.2]),
        "points": np.zeros((3, 4)),  # [x, y, yaw, speed]，单位 m / rad / m/s
    }
    obs, reward, terminated, truncated, info = env.step_trajectory(trajectory)
```

时间从 0 开始严格递增，轨迹覆盖窗口，速度非负，参考帧必须是最近观测帧。Gym 使用 `CarlaGymEnv(..., action_mode="trajectory")`，shape 由服务发布，时间序列由配置生成。参考点、格式及显式执行时长见 [轨迹接口](docs/experiments.md)与 [客户端说明](docs/parallel-environments.md)。

## 选择场景与配置

| 配置 | 用途 |
|---|---|
| [fake.yaml](configs/fake.yaml) | 无 CARLA 的安装和通信检查 |
| [scenic.yaml](configs/scenic.yaml) / [scenic_suite.yaml](configs/scenic_suite.yaml) | 默认 Scenic 环境 / 显式长尾场景清单 |
| [fast/scenic_train.yaml](configs/fast/scenic_train.yaml) | planning_v1、Town10HD 固定地图、10 Hz、训练地图复用 |
| [fast/scenic_eval.yaml](configs/fast/scenic_eval.yaml) | planning_v1、世界重载、实验扩展 |
| [bench2drive_train.yaml](configs/bench2drive_train.yaml) | Town12 长路线训练清单，通用 driving 任务 |
| [fast/bench2drive_train.yaml](configs/fast/bench2drive_train.yaml) | 同一训练清单、p6、planning_v1、地图复用、原生任务；静态槽 64 MiB |
| [bench2drive_eval.yaml](configs/bench2drive_eval.yaml) | 锁定新版 220 条评估清单与原生评估规则 |
| [bench2drive_low_load.yaml](configs/bench2drive_low_load.yaml) | 减小 Town12/13 流送范围，保留渲染和完整图层 |
| [mixed.yaml](configs/mixed.yaml) | Scenic / Bench2Drive 回合级混合，配置明确权重 |
| [interaction.yaml](configs/interaction.yaml)、[merge](configs/interaction_merge.yaml)、[crossing](configs/interaction_crossing.yaml) | NPC 反应、固定公共路径与实验扩展 |

CLI 配置路径默认相对配置文件目录；顶层 `path_base` 改变基准。普通配置用 `..`，fast 配置用 `../..`，均指向仓库根目录。复制配置到其他目录需调整 `path_base` 或使用绝对路径；fleet 的 worker 配置路径相对 fleet 文件目录。

训练用 `reload_on_reset=false` 减少同地图重载，并清理回合资源；评估和公共前缀比较使用严格重建。地图复用不保证 Traffic Manager 隐状态完全恢复，不自动获得可比较起点。

### 准备 Bench2Drive（可选）

在服务环境安装原生依赖，从已有上游 checkout 生成隔离副本：

```bash
python -m pip install -c environments/constraints-service.txt -e '.[carla,bench2drive,test]'
python scripts/prepare_bench2drive.py --source /path/to/Bench2Drive
```

源仓库必须包含固定提交 `acbeb0dd8aa99dd0e71b9b75bb542f6fe7c7f3d8`。脚本创建 `~/.cache/easycarla/bench2drive/<commit>-p6`，保留依赖锁和补丁摘要，不修改原工作区。自定义目录需设置 `scenario.dependency_root`；旧 legacy 配置可能继续选已有旧副本，planning_v1 应使用显式 p6 路径。

原 90 条 Town12 长路线按几何分组为 **78 train / 12 dev**，不是新版 1100 条短路线。评估清单不用于隐式训练回退；来源生成失败默认报错，不自动换场景。`reset(options={"route_id": "..."})` 中的 ID 必须来自当前清单。完整步骤、原生指标及接管 / 重演限制见 [Bench2Drive 接入](docs/bench2drive.md)。

准备好副本和对应地图后，先停止旧的同名服务，再选择训练配置启动；CARLA 服务器继续由你管理：

```bash
easycarla serve --instance carla_01 --config configs/bench2drive_train.yaml
```

原生评估使用 `configs/bench2drive_eval.yaml`；无渲染 planning 训练使用 `configs/fast/bench2drive_train.yaml`，可将它设为 fleet 的 worker 配置。改变配置后需要重启对应服务，不会自动热更新。

## 故障处理与常见问题

| 现象 | 检查或处理方式 |
|---|---|
| `easycarla` 找不到 | 激活服务环境，完成根包 pip 安装；Conda YAML 本身不安装本项目 |
| 客户端或 Gymnasium 导入失败 | 在实际客户端解释器中安装 `packages/client`，需要 Gym 时安装 `[gym]` |
| 连接不到实例 | 先启动服务，确认 instance 与 `--instance` 完全一致；fake 流程可隔离 IPC 问题 |
| 实例被占用 | 同一实例只允许一个控制客户端；关闭旧连接，勿让示例与训练程序同时连接 |
| CARLA 连接 / reset 失败 | 核对 RPC 端口、服务器存活、安装路径和地图；查看服务日志及 CARLA 输出 |
| reset 冷启动超过 30 秒 | 增大客户端 `timeout`，例如 180；区分 RPC 超时与原生崩溃 |
| 纯结构化 worker 配了相机或 LiDAR | fleet 高效配置关闭三者；需要传感器则另起渲染实例 |
| `history_ready=false` | 历史未满或终止帧未对齐历史时钟；检查实际时间戳与有效掩码 |
| 静态查询容量不足 | 增大 `ipc.static_bytes`；Town12 planning 地图实测约 18.8 MB，fast 配置为 64 MiB |
| fleet 启动拒绝 | 检查路径、端口、重复 ID / instance、内存准入及无渲染设置；日志在 `log_dir` |
| Town12/13 原生崩溃 | 保留退出码与日志，参考地图诊断；增大超时不能修复引擎崩溃 |

故障处理遵循以下语义：

- **终态**：正常返回最后观测，显式 reset 开始下一回合。
- **明确预检拒绝**：`RemoteError(code="invalid_request")` 且 `details.request_rejected=true` 表示修改前拒绝，原回合和状态保留。修正参数后可继续合法 step 或 reset。
- **请求已发出但超时 / 结果未知**：不自动补发动作，按故障处理，显式重连、reset 或重启。
- **批量部分失败**：`BatchError.completed` 保留健康 worker 的有效结果，`failures` 标明失败 worker；预检拒绝不禁用 worker，其他故障需显式恢复。
- **worker 替换**：不能替换为自身或共享 client；合法替换后也需显式 reset。

服务退出后若有 IPC 残留，在服务环境执行：

```bash
easycarla cleanup --instance carla_01
```

此命令拒绝清理活服务，不是停止进程的命令。更多语义见 [协议与会话](docs/protocol.md)。

## 测试、实测范围与扩展入口

### 开发验证

在服务环境执行：

```bash
python -m pytest -q
python scripts/check_wheel_install.py --output /tmp/easycarla-wheel-install.json
```

默认测试包含跨进程 IPC，未显式启用的 CARLA 集成测试会跳过。wheel 检查在独立临时环境、源码目录外验证安装与清理；fake 结果不代表真实仿真通过。

真实内核检查要求独占 CARLA，**先停止占用同一世界的环境服务**：

```bash
EASYCARLA_REAL_PORT=2000 python -m pytest tests/test_carla_integration.py -q
```

测量已运行服务的耗时则使用客户端脚本：

```bash
python scripts/measure_service.py --instance carla_01 \
  --episodes 2 --steps 20 --output /tmp/easycarla-timing.json
```

### 如何理解验证结果

- [最近六项正确性修复](docs/testing/correctness-fixes.json)：705 项通过、19 项跳过；独立 wheel、真实 Scenic / Bench2Drive 物理清单和 reset 拒绝路径、退出清理通过。
- [并行实测报告](docs/testing/parallel-v1/README.md)：双 Scenic / 双 Town12 同配置 step 对照约 1.78× / 1.65×；混合来源约 1.11×，未达到原目标。双 Scenic 固定刹车负载通过 30 分钟；4 worker 因内存预算未准入。
- [场景能力表](docs/scenario-capabilities.md)与 [原生 Bench2Drive 报告](docs/testing/platform-bench2drive-report.md)：接口支持、短窗口执行、机制触发、真实接管与完整重演分别验收。
- [地图与 Vulkan 调查](docs/testing/town13-investigation/README.md)：保留失败证据；Town12/13 短窗口通过不代表长期稳定。完整 220 路线和全部 cut-in 接管仍不能宣称通过。

这些结果来自特定机器、配置、源码阶段和负载，不是任意策略的吞吐保证。最近正确性补丁未重新测量吞吐，也未扩大旧报告的长期稳定性范围。

### 代码目录

```text
easycarla/
  runtime.py           回合生命周期与决策窗口
  simulation/          世界设置、唯一 tick 所有者、传感器与资源
  scenarios/           场景选择、Scenic 事件、Bench2Drive 适配
  observations/        快照、历史、车辆几何、地图和路线
  control/             控制权、车辆动作、轨迹跟踪与 NPC 控制
  tasks.py             奖励、cost 与终止
  extensions/          实验材料、公共前缀、诊断与重演
  service/             CLI、IPC 服务、fake 后端与 fleet
packages/client/       独立客户端、协议、Gymnasium、向量环境
configs/               场景、训练 / 评估清单、fast 实例配置
scenarios/scenic/      Scenic 场景文件
examples/              最小客户端联调
scripts/               依赖准备、测量、诊断和验证工具
tests/                 契约、IPC、生命周期与可选真实集成测试
docs/                  接口说明、设计边界和实际验证证据
```

新增功能优先沿现有职责扩展，不在客户端引入服务端依赖，不另写物理步进或奖励。`easycarla/envs/gym_env.py` 保留本地薄适配入口，外部工程优先使用独立客户端。旧二维动作可转换，但旧观测 / 奖励 / 终止语义的版本化变化仍需迁移。

进一步阅读：[维护与扩展](docs/maintaining.md)、[架构与迁移](docs/audit/architecture.md)、[实验扩展](docs/experiments.md)、[传感器同步](docs/sensor-synchronization.md)、[地图负载](docs/map-performance.md)、[Vulkan 诊断](docs/carla-vulkan.md)、[行为变化](docs/behavior-changes.md)。
