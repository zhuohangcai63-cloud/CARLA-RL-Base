# 无渲染实例池与并行客户端

平台不包含策略模型、PPO、分布式训练或 rollout 提前结束策略。每个 worker 独占 CARLA 世界、环境服务、端口与共享内存通道。客户端只需 NumPy / POSIX；Gymnasium 为可选 extra。

## 配置与启动

`configs/fast/scenic_train.yaml` 和 `bench2drive_train.yaml` 启用 planning_v1、0.05/0.1/0.1 s 三个时钟、21 帧真实历史、同地图复用和延迟完整观测构造。现有配置仍为 0.2 s 决策窗口。Scenic worker 显式固定 Town10HD，训练清单为 Town12 的 Bench2Drive worker 固定 Town12；map_affinity 不匹配直接报错，不静默更改来源权重或过滤路线。混合源需要你显式配置相同地图内的 Scenic 文件和合法 split 清单。评估/公共前缀配置继续 reload_on_reset=true；训练复用不保证 TM 隐状态完全恢复。

高效 Bench2Drive 配置显式使用准备脚本生成的 p6 副本。它记录原生关闭物理的调用，防止对失去 PhysX 状态的车辆调用 get_physics_control；关闭期间保留 bbox、报告运动有效性，恢复后补测。旧 p4/p5 继续用于 legacy；原生 planning_v1 拒绝未提供这一状态见证的旧副本。

Town12 的完整 planning 地图实测约 18.8 MB，超过协议原默认的 16 MiB 静态槽。高效 Bench2Drive 配置显式分配 `ipc.static_bytes: 67108864`（64 MiB）；不改变协议或旧配置默认值。更大自定义地图仍需按编码体积配置容量，超出时返回故障，不能按一次 step 的动态观测大小推算静态槽。

编辑 `configs/fast/fleet.yaml` 中的解释器、CARLA 路径、log_dir、worker 配置和各自的 RPC/streaming/secondary/TM 端口：

```bash
easycarla fleet start --config configs/fast/fleet.yaml
easycarla fleet status --config configs/fast/fleet.yaml
easycarla fleet restart --config configs/fast/fleet.yaml --worker worker_0
easycarla fleet stop --config configs/fast/fleet.yaml
```

只启动官方 0.9.15 的 `-nullrhi -nosound` 独立进程，验证 no_rendering_mode，并在就绪后保持同步待机。高效实例拒绝 RGB、语义相机 BEV、LiDAR；旧手动服务仍支持已有传感器配置。普通渲染服务需另起实例，不可用 RenderOffScreen 当作无渲染基线。

启动前检查整个配置的端口/实例冲突和可用 RAM，逐 worker 启动；每个 worker 再检查内存。默认每实例预算 6000 MiB，保留 4096 MiB；这个预算是**准入估计**，不是 CARLA 硬内存上限，尤其加载 Town12/13 时需要重新测量。第一版示例为两个 worker，不自动扩容。服务启动不隐式 reset；地图和场景准备仍由调用方显式 reset 触发，端到端吞吐必须计入它。

每个 supervisor 拥有 CARLA/service 独立进程组，日志/status.json 持久保存 worker ID、PID 与 Linux 启动身份、service epoch、退出码及故障原因。CARLA 原生就绪调用置于有硬超时的临时子进程中，阻塞不会卡死管理器。SIGTERM 正常先结束服务，再结束 CARLA；Linux subreaper 回收启动 shell 的子进程。只停止登记的、启动身份仍匹配的进程，用户手工启动的服务不会被扫描并杀掉。没有自动重启或动作重放，restart 必须显式指定单 worker。

**官方 CARLA 0.9.15 不支持 CaRL 定制的服务器线程参数。** `threads.rpc/streaming/secondary=2` 记录为请求预算，但 status 中 server_thread_limits.applied=false；不会向官方引擎传入这些无效选项。其引擎 RPC 线程由 hardware_concurrency 决定。[官方 CarlaEngine.cpp](https://github.com/carla-simulator/carla/blob/0.9.15/Unreal/CarlaUE4/Plugins/Carla/Source/Carla/Game/CarlaEngine.cpp) 和 [CarlaSettings.cpp](https://github.com/carla-simulator/carla/blob/0.9.15/Unreal/CarlaUE4/Plugins/Carla/Source/Carla/Settings/CarlaSettings.cpp) 是依据。服务数值库 OMP/OpenBLAS/MKL 等线程通过子进程环境实际设为 numeric（默认 1）。`nothreading` 可显式开关用于对照，但不宣称它能将 RPC 线程精确限制为 2。

## 独立 Gymnasium 包

```bash
pip install 'easycarla-client[gym]'
```

本地构建可用 `pip wheel --no-deps ./packages/client`，再安装对应 wheel 的 `[gym]` extra。不要在外部策略环境安装 easycarla、CARLA、Scenic 或 Torch 环境依赖来使用客户端。

```python
from easycarla_client import CarlaGymEnv, CarlaVectorEnv
import numpy as np

with CarlaGymEnv('planning_0', timeout=120) as env:
    obs, info = env.reset(seed=7)
    while not bool(obs['planning.history_ready']):
        obs, reward, terminated, truncated, info = env.step([0, 0, 1])
        if terminated or truncated:
            break  # 已结束的回合不能交给规划器接管
    static_map = env.get_static(info['map_id'])
    route = env.get_route(info['route_id'])

vec = CarlaVectorEnv(['planning_0', 'planning_1'], timeout=120)
try:
    obs, infos = vec.reset(seed=7)  # worker i 使用 seed+i，也可传种子列表
    actions = np.tile([0, 0, 1], (vec.num_envs, 1))
    vec.step_async(actions)
    obs, rewards, terminated, truncated, infos = vec.step_wait()
    mask = terminated | truncated
    if mask.any():
        obs, infos = vec.reset(mask=mask)
finally:
    vec.close()
```

Gym observation 为按点路径命名的扁平 Dict（例如 planning.ego_history.position），全部固定形状数值/掩码；字符串和来源放 info、静态查询或 capabilities。原始 CarlaEnvClient 仍返回原嵌套数据。开启 LiDAR 的单环境接口会在客户端填到发布容量；count 保留实际点数。观测 space 在构造时确定，reset 后不改变。`ControlSpace` 保留三个动作，sample/contains 排除同时 throttle/brake；手动非法输入被本地拒绝，不发布请求。

轨迹模式 `CarlaGymEnv(..., action_mode='trajectory')` 是固定 `[trajectory.points,4]` Box，每点 `[x,y,yaw,speed]`，单位 m/rad/m/s、相对提交帧的指定参考点。times 自动按 trajectory.dt 构造，执行一个 decision_dt 窗口。配置 reference_point 默认为 actor；rear_axle 要求 planning_v1 和有效实际几何。原始 step_trajectory 的 trajectory 字典也支持 reference_point，省略时保持原行为。环境只跟踪外部给出的路径，不生成规划结果。

planning_v1 下 actor/rear_axle 两种轨迹参考点均要求已测得有效车辆几何，客户端在发布前检查；不可测车辆不会用名义轴距替代。legacy actor 模式保留原有跟踪参数以兼容旧配置。

每 worker 至多一个请求在途。vector batch 先检查全部动作和 worker 可用性，再提交；默认关闭 autoreset，done 后必须显式 reset，终态观测不会被新回合覆盖。同一 vector 必须规格与时钟一致，包含轨迹规格。reset(mask=...) 只改变选中 worker，其他最后观测保留。

Simulator frame ID、回合 ID、测量耗时不保证按 seed 完全相同，EnvSpec.nondeterministic=true；它不影响已有严格公共前缀校验。运行 Gym checker 时使用 `check_env(env, skip_render_check=True, skip_close_check=True)`：该检查器的额外 EnvSpec.make 不能对同一独占服务创建第二个控制客户端，关闭/重连由专用 IPC 测试验证。

## 完成顺序与故障

```python
from easycarla_client import CarlaEnvClient, ClientPool
clients = {'w0': CarlaEnvClient('planning_0'), 'w1': CarlaEnvClient('planning_1')}
try:
    for c in clients.values():
        c.reset(seed=7)
    with ClientPool(clients) as pool:
        pool.submit('w0', 'step', [0, 0, 1])
        pool.submit('w1', 'step', [0, 0, 1])
        pending = 2
        while pending:
            for completion in pool.poll_ready(timeout=0.1):
                pending -= 1
                if completion.error is not None:
                    print(completion.worker_id, completion.error)
                else:
                    consume_transition(completion.worker_id, completion.result)
finally:
    for c in clients.values():
        c.close()
```

poll_ready 返回 Completion(worker_id, result, error)，按已完成结果消费，不设 rollout 长度或抢跑比例。slot 在结果消费后释放。pool 拥有执行线程，**不拥有传入客户端**；关闭客户端由调用者负责。故障 worker 禁止新提交，显式 pool.replace 或 vector.replace_worker 后重新 reset。

pool 在提交线程中复制轨迹、数组和嵌套 options，返回后调用方可以复用输入。整批请求先完成参数形式、数值和 worker 可用性检查；依赖当前物理帧、车辆几何和时钟的检查仍在服务端执行。Gym 轨迹转换为 float32 后再次检查有限值，溢出不会发布请求。替换 worker 必须使用不同的 env 和 client，也不能复用其他 worker 的 client。

服务确认在修改回合前拒绝的请求，继续使用 `invalid_request`，并附带 `details.request_rejected=true`。这类错误仍由 Completion.error 或 BatchError.failures 返回，但不禁用 worker、不覆盖原观测，原回合可以继续 step 或再次 reset。没有该标记的错误继续沿用原故障处理；客户端不会猜测动作是否已经执行。

`fleet status/stop` 只读取 fleet 中的管理身份、日志位置和持久化状态，worker 环境配置、服务解释器或 CARLA 安装缺失不阻止管理。`start` 仍完整验证启动条件，`restart` 在停止原 worker 前验证新配置。fleet 配置本身和匹配的进程身份记录仍是管理所需信息。

向量 BatchError.completed 保存健康 worker 的完整有效结果，failures 保存失败 worker/异常；读取这些结果后外部采样器决定如何继续。强杀、超时、epoch 变化、结果未知都抛错，绝不伪造 truncated。未知请求对应的客户端不能继续请求；需要关闭旧会话、必要时显式重启、重连和 reset。不要重放未知动作。

## 验证与测量

`pytest -q` 覆盖数值观测、物理奖励语义、真实 POSIX 多服务并发/强杀和进程身份保护。`scripts/check_wheel_install.py` 验证独立 wheel 无源码路径、无模拟器依赖。`scripts/validate_parallel_runtime.py` 验证运行中的两个实例的真实观测/地图/路线/轨迹契约。

`scripts/benchmark_parallel.py` 对固定 seed 和 brake 请求测量串行/并行、至少 3 次每次 1000 个有效请求，记录物理 tick、step/reset/端到端吞吐、p50/p95、分项计时和终态事件。`--seconds 1800 --repetitions 1` 用于至少 30 分钟 soak。任务不动自车，但 NPC、场景、碰撞、标准和物理持续更新；吞吐不能解释为主动驾驶或模型效果。源代码指纹与独立原始报告见 [实际报告](testing/parallel-v1/README.md)。
