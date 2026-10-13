# 独立 CARLA 诊断进程包装器

`scripts/run_carla_diagnostic.py` 仅用于测试：启动并拥有一个 CARLA 原生进程和一个客户端，采样资源，保存退出阶段与日志，最后回收自身进程组。它不重启、不重放、不发送 CARLA 请求、不推进世界；CARLA readiness 由后面的客户端处理。

例如在服务 Conda 环境执行一次纯 API Town13 排查：

```bash
python scripts/run_carla_diagnostic.py \
  --carla-root /home/czh/software/carla \
  --port 2600 \
  --output-dir /tmp/carla-town13-diagnostic-01 \
  --timeout 900 --stop-timeout 25 \
  -- python scripts/diagnose_carla_maps.py \
     --port '{PORT}' --output '{OUTPUT_DIR}/probe.json' \
     --town Town13 --repeats 3 --ticks 100 --no-rendering
```

输出目录必须不存在。端口及后续两个端口必须空闲；包装器使用独立的 `diagnostic-port-*` 占用锁，环境客户端仍可取得 `world-port-*` 锁。稳定锁文件不会删除。启动时检查已有监听端口，发现占用直接失败，不会连接或终止已有 CARLA。不能保证其他不遵循锁的程序在检查后不争抢端口，因此仍应给诊断分配专用端口。

默认直接执行安装内的 shipping 二进制，传入 `-RenderOffScreen -quality-level=Epic -nosound -ResX=400 -ResY=300` 及三个明确端口。窗口大小、传感器和地图参数仍由诊断显式声明，不能当作完整基准评测。使用 `--quality-level Low` 可显式复现 Low 对照；该选项仅接受 `Low` / `Epic`，默认 `Epic`，最终命令只包含一个画质参数。单变量线程诊断可增加 `--server-arg=-norhithread`；重复该选项可传多个引擎参数，但禁止覆盖画质或 CARLA 端口/连接/设置文件。参数仅作为诊断候选，不代表已修复引擎。

`--sample-interval` 控制采样间隔，默认 1 秒。默认尝试调用 `nvidia-smi`；没有该程序或采样失败会如实写入样本错误，不阻止无 GPU 的工具自测。`--no-gpu-monitor` 可关闭 GPU 采样。采样会有墙钟及 I/O 开销，因此这些运行不等价于无监控性能基线。

纯 API 探针的 `--camera` 使用 `sensor_tick=0.0`，与生产传感器管理器一致，表示每个物理 tick 采集；在 0.05 秒固定步长下为仿真时间 20 Hz，不是按墙钟自由运行。CARLA 0.9.15 将该属性作为浮点数交给 [SetActorTickInterval](https://github.com/carla-simulator/carla/blob/0.9.15/Unreal/CarlaUE4/Plugins/Carla/Source/Carla/Sensor/Sensor.cpp#L33-L42)，非零值走独立间隔调度，不应用 `0.05` 代替逐帧声明。[UE4 TickInterval 文档](https://dev.epicgames.com/documentation/en-us/unreal-engine/API/Runtime/Engine/Engine/FTickFunction/TickInterval?application_version=4.27)明确零间隔逐帧执行。旧探针曾在第二帧缺失时超时，该结果保留为探针失败，不能算原生崩溃或地图不可用证据；修正后须启动独立测试。

客户端还会收到 `EASYCARLA_DIAGNOSTIC_PORT`、`EASYCARLA_DIAGNOSTIC_OUTPUT_DIR` 和 `EASYCARLA_DIAGNOSTIC_SERVER_PID` 三个环境变量。不经过 shell 解释客户端命令。`{PORT}` / `{OUTPUT_DIR}` 是参数文本替换，不是 shell 展开。

输出包括：

- `summary.json`：原子替换的状态、命令、PID、退出码、清理动作、采样次数及资源峰值。默认峰值是采到的原生服务 RSS 和每块 GPU 整体显存占用，不代表独占服务显存，也不能捕获短于采样间隔的峰值。
- `samples.jsonl`：逐样本追加并 flush 的 RSS、可用内存、GPU 数据、PCIe correctable AER 计数及读取错误。
- `server.txt` / `client.txt`：UTF-8 日志，无法解码字节替换为 Unicode replacement character。写入失败会记录 `log_error`、使 `log_complete=false`，同时继续排空管道以避免堵塞被误认作 CARLA 挂起。
- `crashes/`：从 `--saved-dir`（默认 `~/.config/Epic/CarlaUE4/Saved`）复制本次 PID 对应、时间匹配的 `crashinfo-CarlaUE4-pid-*` 目录。没有匹配文件只表示没有找到该格式的报告，不能证明没有崩溃。

本调查目录中的 XML 分享副本已去除 `MachineId`、`LoginId` 设备标识；[redaction-manifest.json](redaction-manifest.json)记录每份副本修改前后的 SHA-256 和移除字段。堆栈及退出信息保持不变，安装目录对应的 `Saved` 原件未修改。

`status=client_passed` 仅表示客户端退出 0；总结果 `passed` 还要求服务清理成功和完整日志。`server_exit_before_stop` 非空表示显式停止服务前已退出；`shutdown_crash` 仅表示停止后观测到 SIGSEGV/SIGABRT 等异常信号码；`shutdown_forced_kill` 表示 SIGTERM 后未及时退出而被强制回收。后者属于停止挂起/超时，不能据此推断 Vulkan 崩溃。即使主进程退出 0，只要任一拥有的进程组需要 SIGKILL 回收，总结果也为失败。客户端应清理场景并恢复原异步设置；在同步世界等待 tick 时停止服务可能触发不同的退出行为。

`shutdown_outcome` 将 `exited_zero`（停止后退出 0）、`terminated_by_sigterm`（直接被 SIGTERM 终止）、`exited_with_sigterm_status`（处理 SIGTERM 后退出 143）、`crash_signal`、`forced_kill` 与 `server_exited_before_stop` 分开。只有包装器显式停止后，0、-15、143 才是可接受的拥有进程停止结果；运行期间自行退出 143 仍然失败。任一退出码都不能证明引擎完成内部清理或完整释放资源。停止信号前最后一次 poll 检出的退出不会归为停止期崩溃；信号与故障几乎同时发生时只能按观测阶段归类，不能证明信号造成故障。旧报告保留当时的原始判定，不按新规则覆盖历史证据。

无 CARLA 自测：

```bash
python -m pytest tests/test_carla_diagnostic.py -q
```

测试使用临时 Python 可执行文件，包括客户端失败、服务运行期异常、停止期异常、超时强制回收、包装器被中断、端口/实例冲突、既有输出保护、启动客户端失败、同组遗留子进程及日志写入失败。它们不是 CARLA 地图稳定性证据。

默认画质改为 Epic：本机 Town03 在 Low 下复现原生 Vulkan 崩溃，Epic 下的有限对照通过。需要复现旧负载时显式传 `--quality-level Low`；历史日志保留实际启动参数，不追改旧结果。详见[本次调查](README.md)。

## 复现本次覆盖配置

以下命令在仓库根目录、`CARLA-RL-Base` 服务环境中运行，使用现有 CARLA 安装；两个输出目录必须尚不存在。端口 2600–2602 必须没有其他服务占用，原生场景测试另使用 TM 端口 8600。每条命令独立启动一个 CARLA，命令内部不重启。先完成[安装与 Bench2Drive 依赖准备](../../installation.md)。

全部 12 张地图的 RGB 配置与 [rgb-all-epic/summary.json](rgb-all-epic/summary.json) 中记录的地图顺序、画质及客户端参数一致：每图两次加载、每次 100 帧，320×180 RGB，流送/激活距离均为 650 m。

```bash
python scripts/run_carla_diagnostic.py \
  --carla-root /home/czh/software/carla --port 2600 \
  --output-dir /tmp/easycarla-rgb12-epic-repro \
  --quality-level Epic --timeout 1800 --stop-timeout 25 \
  -- python scripts/diagnose_carla_maps.py \
     --port '{PORT}' --output '{OUTPUT_DIR}/probe.json' \
     --town Town13 Town12 Town03 Town01 Town02 Town04 Town05 Town06 \
            Town07 Town10HD Town11 Town15 \
     --repeats 2 --ticks 100 --timeout 90 --distance 650 --camera
```

Town13 原生场景使用显式 [41 条评估清单](town13-eval41.json)，客户端参数对应 [native-town13-epic/summary.json](native-town13-epic/summary.json)。下面复现的是旧批次的**同世界复用**条件，因此必须显式指定 `--no-reload`；该批次在第 39 条路线初始化时崩溃，不能作为通过结果。每路线最多 1000 个物理 tick，自带参考驾驶器；不启用相机，也不在每条路线后重启 CARLA。路线驾驶成功与达到 tick 上限分别记录，不等于官方基准通过。

```bash
python scripts/run_carla_diagnostic.py \
  --carla-root /home/czh/software/carla --port 2600 \
  --output-dir /tmp/easycarla-town13-eval41-epic-repro \
  --quality-level Epic --timeout 7200 --stop-timeout 25 \
  -- python scripts/validate_native_routes.py \
     --port '{PORT}' --tm-port 8600 \
     --manifest docs/testing/town13-investigation/town13-eval41.json \
     --output '{OUTPUT_DIR}/probe.json' \
     --seed 123 --ticks 1000 --timeout 90 --no-rendering --map-layers all --no-reload \
     --tile-stream-distance 650 --actor-active-distance 650
```

包装器的 `--timeout` 是整次运行的墙钟预算，客户端的 `--timeout 90` 是 CARLA 请求/初始化等待预算。以上直接使用仓库工具，不依赖 `/tmp` 脚本。本次引擎变量调查使用过的[只读 v3 包装器源码](readonly-cvar-v3-wrapper-source.txt)仅保留为诊断过程材料：它读取特定二进制地址，不属于复现正常场景运行的必要步骤。

正常验收的原生路线和矩阵脚本现默认逐回合重新加载世界，与生产运行内核和锁定的原生 evaluator 一致；可显式写 `--reload`。与上面的复用诊断比较时，应使用独立输出目录，并保持路线顺序、seed、tick 预算及渲染配置不变。重新加载世界不等于重新启动 CARLA，也不代表原生崩溃已经修复；对照依据见 [transition-analysis.md](transition-analysis.md)。
