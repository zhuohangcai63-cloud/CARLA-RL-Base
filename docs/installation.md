# 独立安装与启动

首版使用同机 Linux 的 POSIX 共享内存和命名信号量。CARLA 仿真器、环境服务、外部客户端分别启动；一个环境实例独占一个 CARLA 世界，一个实例同时只有一个控制客户端。训练工程使用自己的仓库和 Conda 环境，只安装客户端包。

环境服务使用 Python 3.10、CARLA 0.9.15 和 Scenic 3.1.1；这里的客户端环境示例使用 Python 3.11。两端不需要安装相同的 NumPy 版本，但必须遵守同一协议版本。服务依赖里没有 PyTorch，也不安装 CUDA 训练库；已有 `CARLA-RL-Base` 环境中的 Torch 不会被这些命令主动卸载。

以下安装命令从本仓库根目录运行。CARLA 根目录可通过配置中的 `simulation.carla_root` 指定；未指定时使用环境变量 `CARLA_ROOT`，再缺省为当前用户的 `~/software/carla`。显式配置优先于环境变量。示例 Scenic 配置使用 `~/software/carla`，换机器时按实际安装位置修改。CARLA 发行版提供的 `PythonAPI/carla/agents` 也需要存在；服务会从 `carla_root` 添加这个路径，不要求客户端设置 `PYTHONPATH`。

## 1. 环境服务安装

创建新环境，使用 conda-forge 的系统库，再安装两个本地包：

```bash
conda env create -f environments/service.yml
conda activate easycarla-service
python -m pip install -c environments/constraints-service.txt \
  -e ./packages/client -e '.[carla,bench2drive,test]'
python -m pip check
```

如果继续使用已有环境：

```bash
conda activate /home/czh/software/miniconda3/envs/CARLA-RL-Base
python -m pip install -c environments/constraints-service.txt \
  -e ./packages/client -e '.[carla,bench2drive,test]'
```

只使用 Scenic 可以将最后一个安装项换成 `'.[carla,test]'`。Bench2Drive 可选依赖包含其原生运行需要的 `py-trees==0.8.3`、`dictor`、`ephem`、`xmlschema`、`transforms3d`、`simple-watchdog-timer`、`requests` 等；不要直接安装上游整份旧 requirements。

`environments/*.yml` 只负责 Conda 基础环境，后续 pip 命令是必需的。constraints 记录本机已安装的依赖版本，服务侧 `psutil/tabulate` 已按约束补装并通过 `pip check`；它们不是跨平台二进制哈希锁，也不代表全新 Conda 重建已实际验收。实际测试范围以 `docs/testing/` 的报告为准。

## 2. 外部客户端独立安装

在另一个终端创建独立环境：

```bash
conda env create -f environments/client.yml
conda activate easycarla-client
python -m pip install -c environments/constraints-client.txt ./packages/client
```

外部研究仓库只需要安装这个包路径，随后可回到自己的目录工作：

```bash
python -m pip install /path/to/CARLA-RL-Base/packages/client
```

客户端的运行依赖仅为 NumPy 和 `posix_ipc`，没有 CARLA、Scenic、Gymnasium、Torch 或模型代码。也可以在环境仓库构建 wheel，再把 wheel 放入外部工程的依赖分发位置：

```bash
python -m pip wheel --no-deps --wheel-dir dist ./packages/client
```

## 3. 分别启动 CARLA、服务与客户端

终端 A 启动独立 CARLA 进程：

```bash
/home/czh/software/carla/CarlaUE4.sh \
  -RenderOffScreen -nosound -quality-level=Epic -carla-rpc-port=2000
```

CARLA 需要本机可用的 GPU 驱动和对应地图资产，服务不会替用户下载服务器或地图。CARLA 的启动失败会作为错误报告，不会转换成训练终止样本。

2026-09-28 本机对照中，Town03 在 `Low` 下复现 Vulkan 崩溃，`Epic` 下两次加载与逐帧 RGB 检查通过，因此示例不再默认降低画质。两种画质改变相机输入，不能视为相同实验配置；应把外部 CARLA 启动命令与数据记录在一起。大地图先使用显式流送距离配置，见[按地图配置负载](map-performance.md)与[实际覆盖和限制](testing/town13-investigation/README.md)。

Town12/13 还保留使用隔离用户目录的 [Vulkan 诊断配置](carla-vulkan.md)，该组合未通过长批次验收，不能作为稳定运行方案。普通 Epic 长批次仍有失败记录；该说明分别列出地图渲染、原生场景运行和进程退出结果，不承诺消除所有原生故障。

终端 B 激活服务环境，从本仓库启动 Scenic 环境：

```bash
conda activate easycarla-service
easycarla serve --instance carla_01 --config configs/scenic.yaml
```

终端 C 激活外部客户端环境，运行最小联调：

```python
import numpy as np
from easycarla_client import CarlaEnvClient

env = CarlaEnvClient(instance_name="carla_01", timeout=120.0)
try:
    obs, info = env.reset(seed=42)
    for _ in range(10):
        action = np.array([0.0, 0.2, 0.0], dtype=np.float32)
        obs, reward, terminated, truncated, info = env.step(action)
        if terminated or truncated:
            break
finally:
    env.close()
```

动作顺序为转向、油门、刹车，正转向表示右转。这段联调验证数据协议，不提供驾驶策略。外部计算期间 CARLA 不自动推进。`env.close()` 清理这个客户端的回合并释放连接；环境服务继续运行，外部启动的 CARLA 继续存在。服务可用 Ctrl-C 结束；服务崩溃遗留的 IPC 资源可执行 `easycarla cleanup --instance carla_01`，命令拒绝清理仍活动的实例。

客户端的 `timeout` 覆盖整个请求，包括 `reset()` 的地图加载和场景构造，与服务端单次 CARLA RPC 超时是两个设置。大地图冷启动可能超过客户端默认的 30 秒；`examples/client_loop.py` 默认 180 秒，也可显式传入 `--timeout 180`。请求发出后超时仍按结果未知处理，不能自动重放动作。

多实例使用不同实例名、CARLA RPC 端口及 Traffic Manager 端口；例如第二个实例使用 2100/8100，并连接另一个独立 CARLA 进程。不要用两个环境服务推进同一个世界。

## 4. 准备 Bench2Drive 与锁定清单

已有本地 Bench2Drive checkout 时，执行：

```bash
python scripts/prepare_bench2drive.py --source /home/czh/workspace/Bench2Drive
```

脚本只读取固定提交 `acbeb0dd8aa99dd0e71b9b75bb542f6fe7c7f3d8`，在 `~/.cache/easycarla/bench2drive/<commit>-p6` 创建隔离副本，不修改原工作区。它生成 `easycarla-lock.json`，记录提交、集中补丁版本及文件摘要，并生成路线清单和内容重叠审计。源 checkout 必须包含这个提交；可从 [Bench2Drive 上游仓库](https://github.com/Thinklab-SJTU/Bench2Drive) 获取。使用自定义副本目录时，同时设置配置中的 `scenario.dependency_root`。

选择明确用途启动服务：

```bash
easycarla serve --instance carla_01 --config configs/bench2drive_train.yaml
```

原生评估使用另一个启动配置（先停止同名环境服务）：

```bash
easycarla serve --instance carla_01 --config configs/bench2drive_eval.yaml
```

| 配置或清单 | 用途 |
|---|---|
| `bench2drive_train.yaml` / `town12_long_train78.json` | 原 90 条长训练路线按几何分组后的 78 条训练路线 |
| `town12_long_dev12.json` | 同来源 12 条开发路线；使用 `purpose: dev` |
| `bench2drive_eval.yaml` / `v004_eval220.json` | 锁定的新版 220 条评估清单，`bench2drive_native` 任务规则 |
| `legacy_eval220.json`、`legacy_dev10_reserved.json` | 保留评估；Dev10 不能因为名字含 dev 而作为训练集 |
| `mixed.yaml` | 每回合在显式 Scenic 和训练清单之间选择 |

原 90 条长训练路线不是新版 1100 条短路线。配置缺少训练清单会报错，不会借用最终评估路线。回退默认关闭。指定清单内路线可使用 `env.reset(seed=42, options={"route_id": "..."})`；可用 ID 以清单为准。地图缺失必须补充对应资产或显式配置经过验证的地图变体，不能静默替换。

原生执行、接管和公共前缀重演分别报告能力，详见 [Bench2Drive 接入说明](bench2drive.md)。目前原生 Bench2Drive 隐藏控制状态不能完整导出，不能宣称所有原生场景支持前缀重演。通用研究扩展配置使用 `configs/interaction.yaml`，接口见 [实验扩展说明](experiments.md)。

## 5. 分开验证协议和真实仿真

不启动 CARLA，也可以运行纯协议、控制和 fake-world 测试：

```bash
python -m pytest -m 'not carla' -q
easycarla serve --instance protocol_test --config configs/fake.yaml
```

第二个终端用独立客户端环境测量：

```bash
python scripts/measure_service.py --instance protocol_test \
  --episodes 3 --steps 50 --output /tmp/easycarla-protocol-timing.json
```

真实 CARLA 测试要使用专用、没有其他服务占用的 CARLA 端口：

```bash
EASYCARLA_REAL_PORT=2000 EASYCARLA_REAL_REPORT_DIR=/tmp/easycarla-real-report \
  python -m pytest tests/test_carla_integration.py -q
```

这组集成测试自行建立环境内核，因此先停止占用这个 CARLA 的环境服务。服务与独立客户端的真实联调则运行前述 `serve` 和 `measure_service.py --instance carla_01`。测量报告分别记录通信/复制、环境执行和仿真推进耗时；fake 后端通过不表示真实 CARLA 通过，少数路线通过也不表示整套 220 路线完成验收。

## 6. Wheel 安装验收与持续集成

编辑安装可能掩盖漏打包文件或源码目录依赖。下面的脚本构建两个 wheel，创建两个临时隔离环境，从 wheel 安装后切换到源码目录外运行，并验证 `reset/step/close`、重连、观测副本和 IPC 清理：

```bash
python scripts/check_wheel_install.py \
  --client-python /path/to/client-conda/bin/python \
  --output /tmp/easycarla-wheel-install.json
```

`--client-python` 可省略，默认两端使用相同 Python 版本但不同环境。脚本安装基础服务依赖和独立客户端，不安装 CARLA、Scenic、Gymnasium 到客户端，也不安装 Torch。报告明确标注 `environment_type=isolated_venv`、`backend=fake`；这项验收不代表真实 CARLA 验证，也不替代另行执行的跨 Conda 测试。临时环境和服务进程结束后会清理，报告保留 wheel SHA-256、实际依赖版本和结果。

`.github/workflows/tests.yml` 提供 Linux 上 Python 3.10/3.11 的核心测试、Python 3.10 的 Scenic 模板测试，以及从 wheel 独立安装的进程联调。真实 GPU 仿真需要专用服务器和显式启用环境变量；CI 不会将跳过的真实仿真测试记为通过。当前仓库未在远程执行该 workflow；本地执行证据见 `docs/testing/`。

配置通过 CLI 加载时，`scenario.file/files/manifest/dependency_root` 和 `simulation.carla_root` 默认相对于配置文件所在目录；`~` 会展开。顶层 `path_base` 可改变基准目录，本仓库示例明确设置 `path_base: ..`，因此可以从其他目录使用绝对配置路径启动：

```bash
easycarla serve --instance carla_01 \
  --config /path/to/CARLA-RL-Base/configs/scenic.yaml
```

混合来源与显式 fallback 的路径采用相同规则。Bench2Drive 清单内部的路线文件路径仍相对于其隔离依赖副本，由适配器解释。直接向 `EnvironmentRuntime` 传入 Python 字典时不经过文件加载器，相对路径仍由调用者当前工作目录解释；建议使用绝对路径或先调用 `easycarla.service.cli.load_config`。

## 专用无渲染验收实例

早期 Low 画质测试中，部分地图在恢复渲染时发生过原生崩溃；后续 Epic 的全 12 图
逐帧 RGB 检查已通过，详情见[地图调查](testing/town13-investigation/README.md)。
下面保留仅用于结构化状态验收的无渲染流程，不是 Town13 原生崩溃的解决方案。
在专用 CARLA 启动后、任何环境服务接入之前，明确设置初始模式：

```bash
# CARLA 已在 2100 端口启动；使用装有 CARLA API 的服务环境
python - <<'PY'
import carla
client = carla.Client("127.0.0.1", 2100)
client.set_timeout(60)
world = client.get_world()
settings = world.get_settings()
settings.no_rendering_mode = True
world.apply_settings(settings)
PY
```

对应环境配置同时指定 `simulation.no_rendering_mode: true`，关闭 RGB/BEV 摄像头。
服务仍按原合同恢复它接入时读取的世界设置；此初始化使恢复目标也是无渲染。
它不绕过所有 Vulkan 资源装卸，也不能修复已复现的原生内存错误。
不要对已有活环境服务的世界执行上述操作。

`map_layers: none` 与 `tile_stream_distance: 500` / `actor_active_distance: 500` 是
本机大地图结构化验收使用的显式可选设置，默认不启用。它们改变场景加载与远场
物理激活范围，不能作为官方全地图渲染评测的等价替代。Town13 仍有原生崩溃证据，
具体覆盖见最新验收矩阵。

已有 p4/p5 副本和显式 dependency_root 配置继续支持 legacy 观测，旧默认优先保留现有 p4。planning_v1 的 Bench2Drive 场景需要 p6，高效配置已显式选择它：p5 的 ActorTransformSetter 单 tick TM 交接保留；p6 另外记录原生直接/批量关闭物理的状态，避免观测读取已经销毁的 PhysX 车辆。它不改变原生物理开关的调用顺序，不放宽通用控制冲突。prepare 创建新隔离副本，不覆盖旧副本或上游工作区。
