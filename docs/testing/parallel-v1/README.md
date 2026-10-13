# 并行环境实际验证报告

本目录记录 2026-10-12 在当前工作站上的实际运行证据。本次实现、三轮吞吐对照、双实例 30 分钟持续运行已完成；未通过／未运行项分别列出。

## 机器与负载

官方 CARLA 0.9.15，31 GiB 系统内存、20 逻辑 CPU、i5-14600KF、RTX 5060 Ti 16 GB。服务使用 CARLA-RL-Base Conda 的 Python 3.10；独立客户端环境使用 NumPy 2.4.6 / Gymnasium 1.4.0，未安装 CARLA、Scenic、Torch 或环境服务包。

所有性能组使用 `-nullrhi -nosound`、同步世界、禁用 RGB/BEV/LiDAR、0.05 s 物理 tick、0.1 s 决策、0.1 s 历史、21 帧。服务数值库线程实际为 1；CaRL 的 RPC/streaming/secondary=2 参数在官方引擎中不受支持，status 明确记录 `applied=false`。没有将 RenderOffScreen 计为无渲染。

吞吐负载为固定 seed 的 `[0,0,1]` 刹车，NPC/原生行为树/碰撞/任务标准继续按物理 tick 更新。每个完整组为 3 次、每次 1,000 个有效决策请求，报告同时包括纯 step 和含 reset 的端到端吞吐。这个负载不证明主动驾驶、全部原生机制或模型驾驶效果。静态地图/路线查询不加入 step 吞吐；其契约另行测试。

改造前基线是开始实现时的**原有未提交工作区快照**，不是 git HEAD。对应 [源码指纹](baseline-source-hashes.json)、[基线测试](baseline-tests.txt)，本机原始归档保留于 `artifacts/baseline/source.tar.gz`。每组 JSON 附源码指纹、service epoch、真实设置、reset 耗时及终态事件；后续组另附资源采样。不同源码阶段的结果分别保留。

## 已完成的正确性检查

- [完整测试](tests-final.txt)：661 passed / 18 skipped；跳过项需要独立指定的原有 live 验证条件。两个 Gym checker 提示针对无界坐标 Box。
- [真实优化前后对照](real-before-after-equivalence.json)：同 seed、200 请求，先 50 次 throttle=0.3 再刹车。奖励/分项、实际控制、物理 tick、结束标记、位置逐项一致；最大位置和奖励差均为 0。
- [Scenic 真实契约](real-contract-final-scenic.json)：固定 space、外部正常 step 准备两秒历史、后轴/质心、地图拓扑、路线查询、mask reset、控制/轨迹和后轴轨迹通过。
- [独立 wheel](wheels-core-final.json) 和 [真实 Gym 客户端](wheel-real-gym-final.txt)：无源码路径及模拟器依赖的客户端环境可运行；核心 IPC 仍为协议 v1。
- 原生 p4 的 ActorTransformSetter 直接写 TM 车辆速度被控制所有权检查拒绝，保留 [原始失败](bench2drive-p4-failure.json)。p5 只对该类初始化零速操作执行显式所有权交接，单 tick 后恢复原 TM；通用速度写入检查仍保留。上游和 p4 副本未修改。

## 单实例性能

下表为三次纯 step 吞吐，单位有效决策请求/秒。reset/端到端数据见链接。

| 配置 / 阶段 | 三次结果 | 说明 |
|---|---|---|
| [原快照，legacy](scenic-baseline-single.json) | 70.96 / 71.85 / 71.54 | Town10HD，两辆受控 Scenic NPC |
| [当前，legacy 延迟组装/信号缓存](scenic-optimized-legacy-cached-single.json) | 110.12 / 106.38 / 101.31 | 相同负载，数值观测契约保留 |
| [当前，planning_v1](scenic-final-single.json) | 75.61 / 75.05 / 75.44 | 增加实际车辆几何、自车/对象历史、信号车道关联 |
| [Town12 p5，信号几何缓存前](bench2drive-final-single.json) | 5.85 / 5.57 / 5.51 | train78，seed 7 → route 45，后轴有效 |
| [Town12 p5，信号几何缓存后](bench2drive-cached-single.json) | 10.74 / 10.12 / 9.93 | 同路线，全部真实掩码保留 |

Town12 的每决策窗口 frame/history 采集均值由约 102 ms 降至约 26 ms。不可变信号几何复用；每物理 tick 仍读取状态并执行规则检查。profile 将 frame capture、history append、场景、仿真、观测组装、编码及通信分别记录；`history_seconds` 是采集与采样总量，`transport_and_copy_seconds` 是 roundtrip 减后端时间的残差，不能当作纯共享内存复制耗时。服务编码/发布耗时由最终 capabilities 查询单独记录。

部分单实例组与另一个空闲/运行中的服务器并存，资源快照记录其他进程；应以随后同配置的双实例串行/并行对照判断并行收益。

## 同配置并行与混合负载

| 配置 | 串行三轮 step 请求/s | 并行三轮 step 请求/s | 均值比 |
|---|---|---|---|
| [Scenic planning_v1 串行](scenic-final-serial2.json) / [并行](scenic-final-parallel2.json) | 74.89 / 75.15 / 73.56 | 133.68 / 135.59 / 128.94 | **1.78，通过 ≥1.5 目标** |
| [Town12 p6 串行](bench2drive-final-serial2.json) / [并行](bench2drive-final-parallel2.json) | 9.95 / 9.34 / 9.08 | 15.72 / 15.59 / 15.40 | **1.65，通过 ≥1.5 目标** |
| [Scenic + Town12 串行](mixed-final-serial2.json) / [并行](mixed-final-parallel2.json) | 14.98 / 14.78 / 14.86 | 16.57 / 16.53 / 16.16 | **1.11，未通过目标** |

Town12 使用 train78 原清单，seed 7 / 8 对应 route 45 / 33，没有路线过滤或权重改变。串行组首次 reset 含冷态地图/依赖准备，双实例首轮累计约 67 s；并行组在已加载同地图的世界上 reset。因此纯 step 是此处同配置收益的主要对照；两组 JSON 的端到端吞吐同时提供，不将冷/热 reset 差异归因于并行。混合负载由较慢的原生 Town12 约束同步 batch，环境保留完成顺序接口，采样调度由外部训练工程决定。

[四实例冷态准入](four-worker-admission.json)：无运行中的 CARLA/服务时，MemAvailable = 25,873.84 MiB，配置预算 = 28,096 MiB（4 × 6,000 + 4,096）。管理器在启动任何 worker 前拒绝，状态为 not_admitted；四实例吞吐和稳定性为 **not_run**。这是当前保守配置的准入结论，不是硬件绝对无法运行四个轻量地图实例的证明；没有降低预算来绕过检查。

## Town13 专项短窗口

[Town13 三轮报告](town13-final-single.json) 使用 v0.0.4 eval220 中显式选出的单条诊断路线 34183，原 route/trigger 指纹保留，`purpose=eval`、`reload_on_reset=true`。三轮各 334 请求，共 1,002；纯 step 为 10.49 / 10.44 / 10.24 请求/s，含 reset 为 4.73 / 5.03 / 5.00。[地图／历史查询](town13-static-contract.json) 通过：9,276 个地图车道段、有向拓扑、5 段有序路线、实际后轴/质心和正常 20 次 step 后两秒历史均有效。没有把该子集移入 train78 或改变正式混合权重。它只证明此路线、无渲染、短窗口负载通过，不证明 Town13 长期稳定或全部场景机制。

## 线程对照

[`-nothreading` 双 Scenic](scenic-nothreading-parallel2.json) 三轮各 1,000 个有效请求，纯 step 为 142.66 / 141.47 / 145.40 请求/s；CARLA 进程约 19 线程，服务约 24 线程（含 libcarla 回调/RPC 线程，数值库环境预算为 1）。首轮包含冷态 Scenic/世界准备，端到端 56.97 请求/s，热态两轮 138.78 / 142.61。默认线程双 Scenic 为 133.68 / 135.59 / 128.94 请求/s，CARLA 约 98 线程。nothreading 短窗口均值高约 8%；首轮冷态 reset 影响其端到端吞吐，不能据此承诺所有地图更快。默认配置保持 false；30 分钟验收使用默认线程配置，不支持自动把所有配置改为 nothreading。

## 30 分钟持续运行

[完整原始报告](scenic-soak30.json)、[摘要](soak-summary.json) 和 [句柄采样](soak-fd-samples.json)：默认线程、两个 Scenic Town10HD 实例实际运行 **1,800.004 s**，完成 **243,154 个有效决策请求、486,308 个物理 tick、204 次显式 reset**，记录 202 次 timeout 截断终态；没有崩溃、通信超时、隐式重启或客户端清理错误。纯 step **136.58 请求/s**，含 reset 端到端 **135.09 请求/s**，batch p95 约 20.12 ms。

所有请求的后轴参考和当前点速度均有效；history_ready 有 239,278 次，另外 3,876 次恰好为 204 次 reset 后各 19 次正常准备动作。没有在 reset 中暗中驾驶或补齐原始缺失掩码。

| 进程 | 起始 → 结束 RSS MiB | 峰值 MiB | 平均 CPU 核数 |
|---|---|---|---|
| CARLA 0 | 3,217.46 → 3,221.56 | 3,222.91 | 1.09 |
| 服务 0 | 185.16 → 185.31 | 185.31 | 0.60 |
| CARLA 1 | 3,220.91 → 3,224.06 | 3,224.46 | 1.09 |
| 服务 1 | 182.14 → 183.86 | 183.86 | 0.60 |

61 次资源采样中，最低 MemAvailable 为 19,436.40 MiB；CARLA RSS 增量约 3–4 MiB，服务增量约 0.16–1.72 MiB，没有观察到持续扩张。四次句柄采样中 CARLA 始终 20、服务始终 22。基准进程保留延迟样本，整机可用内存不能单独解释为服务泄漏。[一次 GPU 采样](gpu-headless-sample.json) 未发现 CARLA 计算进程，整卡约 498 MiB，包含桌面占用；它不是长期 GPU 监控。

该结论只适用于此双 Scenic、固定刹车负载。Town12/13、nothreading 和主动驾驶没有 30 分钟验收，不扩大为全场景稳定性保证。

## 已观察到的故障与范围

[早期混合组](mixed-cached-serial2.json) 在 Town12 seed 8 / route 33 reset 时触发 CARLA 段错误。冷启动和 RPC 跟踪定位到隐藏 Ford 救护车已关闭物理，观测仍调用 `get_physics_control()`；旧 legacy 相同路线可以运行，说明是新增观测路径引入的问题。p6 的隔离依赖副本记录原生直接/批量关闭物理，几何采集跳过未启用物理、休眠和未登记的车辆，恢复物理后重新测量；没有替换原生场景机制、筛掉路线或重放动作。[RPC 失败轨迹](disabled-physics-rpc-trace.txt)、[引擎失败记录](native-engine-failures.json)、[p6 冷启动回归](bench2drive-p6-cold-seed8.json) 和 [完整 Town12 契约](real-contract-final-native.json) 保留。

Town12 静态地图编码实测 18,759,785 字节，超过旧默认 16 MiB 槽。[原始容量失败](native-static-slot-failure.json) 后，高效 Bench2Drive 配置显式使用 64 MiB；协议 v1 和旧默认不变。

[服务强杀测试](real-fleet-fault.json) 通过：未知 STEP 返回故障，健康实例完成转移并继续推进；没有伪造 truncated 或重放。随后显式单 worker restart，服务 epoch 改变。

短窗口 Town12 通过不代表跨路线复用或长期稳定，也不代表全部原生子场景完成。`native_validation` / `repeatability_validation` 未经完整机制验证仍为 `not_run`。Flow 模型未安装、未执行；[观测映射](../../planning-v1.md)只说明外部适配所需数据。

## 退出清理

[完整清理审计](final-cleanup.json) 和 [实例池退出记录](scenic-final-fleet-stop.json) 通过：所有记录的自有 CARLA／服务／管理器均无活进程，所有已启动实例及四实例准入配置均无 request/response/static 共享内存或 request_ready/result_ready 信号量残留，没有活控制客户端／服务占用锁。正常服务退出码为 0，CARLA 收到预期 SIGTERM。持久、未加锁的锁文件保留原 inode，属于既有生命周期协议，不是 IPC 资源泄漏。

## 复现与实现范围

数值汇总见 [throughput-summary.json](throughput-summary.json)，相对开始实现时未提交工作区的源码变更列表见 [implementation-delta.json](implementation-delta.json)。[实际诊断配置](reproduction-configs/) 保留本机绝对路径及临时路径；换机器需更新解释器、CARLA、依赖副本和日志目录，不能直接照搬路径。训练／评估清单和上游仓库未改写。

```bash
easycarla fleet start --config configs/fast/fleet.yaml
python scripts/benchmark_parallel.py --instances planning_0 planning_1 \
  --mode serial --requests 1000 --repetitions 3 --output artifacts/serial.json
python scripts/benchmark_parallel.py --instances planning_0 planning_1 \
  --mode parallel --requests 1000 --repetitions 3 --output artifacts/parallel.json
python scripts/benchmark_parallel.py --instances planning_0 planning_1 \
  --mode parallel --requests 1000 --repetitions 1 --seconds 1800 --output artifacts/soak.json
easycarla fleet stop --config configs/fast/fleet.yaml
```
