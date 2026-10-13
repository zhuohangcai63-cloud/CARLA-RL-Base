# 已结束 RGB 地图诊断汇总

核对日期：2026-09-28。Epic 配置下，12 张地图连续两轮共 **24 回合、2,400 个显式 tick、2,400 张逐帧 RGB 图像通过**，本轮进程生命周期也通过。耗时 **193.37 秒**，采样 GPU 总占用峰值 **8,496 MiB**，服务 RSS 峰值 **8,090,380 KiB（7.716 GiB）**。这是本机有限地图/主车/摄像头测试结果，不代表 Bench2Drive 场景路线、长时间训练或任意画质配置均已稳定。

此次汇总原十组及追加的一组、共十一组已结束的渲染诊断；不包含另行报告的 `native-town13-epic` 原生路线批次。详细原始数值、完整绝对路径、命令及输入文件 SHA256 均保存在 [render-results.json](render-results.json)。没有修改任何原始 probe 或 summary。

## 有效范围与执行差异

每个完成回合为静止主车 + 320×180 RGB，`sensor_tick=0.0`，物理步长 0.05 秒，100 tick；相机帧号逐回合连续。所有探针运行都对每张地图统一设置 650/650 米、关闭 spectator 流送驱动、保留渲染与全图层。该探针设置与只覆盖 Town12/Town13 的 `bench2drive_low_load.yaml` 不完全相同。`-RenderOffScreen` 隐藏窗口但仍渲染；渲染质量 Low/Epic 是此轮对照的启动参数。

下表中的“完成回合/图像”只计客户端已返回的完整回合。原始 `running` 行若伴随服务 `-11`，表示崩溃中断后的未完成记录，不能当作仍在运行；初始化的 `ticks=0` 也不能证明没有内部加载帧。

| 运行 | 修改/有效性 | 完成回合 / RGB | 原始总 passed | 服务退出 | 解释 |
|---|---|---:|---|---:|---|
| [rgb-all-every-frame-default](rgb-all-every-frame-default/summary.json) | Low，逐帧有效基线 | 2 / 200 | false | -11 | 有效基线；Town13/Town12首轮通过，Town03初始化SIG11 |
| [rgb-all-defrag-off](rgb-all-defrag-off/summary.json) | Low + -ini Defrag=0 | 2 / 200 | false | -11 | 参数未生效：只读CVar仍1；不能算关闭defrag的实验 |
| [rgb-all-defrag-exec](rgb-all-defrag-exec/summary.json) | Low + -ExecCmds Defrag 0 | 2 / 200 | false | -11 | 参数未生效：只读CVar仍1；不能算关闭defrag的实验 |
| [rgb-all-no-rhi-thread-2610](rgb-all-no-rhi-thread-2610/summary.json) | Low + -norhithread | 2 / 200 | false | -11 | 54次线程采样均无RHIThread名称；CVar仍[1,1]不能当线程存在；仍SIG11 |
| [rgb-all-defrag-userdir](rgb-all-defrag-userdir/summary.json) | Low + 隔离UserDir关闭defrag | 2 / 200 | false | -11 | 启动首样本1，随后实际0；修改生效仍SIG11 |
| [rgb-all-sync-pso-userdir](rgb-all-sync-pso-userdir/summary.json) | Low + 隔离UserDir同步PSO | 2 / 200 | false | -11 | r.AsyncPipelineCompile实际[0,0]；修改生效仍SIG11 |
| [rgb-all-one-thread](rgb-all-one-thread/summary.json) | Low + -onethread | 2 / 200 | false | -11 | 线程名采样无RHIThread；仍SIG11，不能宣称所有任务均单线程 |
| [rgb-town03-fresh-low](rgb-town03-fresh-low/summary.json) | 新服务直接Town03，Low | 0 / 0 | false | -11 | 首回合初始化SIG11；不依赖先加载Town12/Town13 |
| [rgb-town03-fresh-epic](rgb-town03-fresh-epic/summary.json) | 新服务直接Town03，Epic | 2 / 200 | false | 143 | 2回合/200帧通过；旧runner把显式SIGTERM返回143误标失败，原值保留 |
| [rgb-all-epic](rgb-all-epic/summary.json) | Epic，12地图×2轮 | 24 / 2400 | true | 143 | 24回合/2400帧通过；本轮生命周期passed=true |
| [rgb-all-conservative-v2](rgb-all-conservative-v2/summary.json) | Epic + 隔离组合配置 v2 + -norhithread | 24 / 2400 | true | 143 | 组合配置实际生效；RGB与生命周期通过，原生路线稳定性尚未证明 |

Low 有效基线在 Town13、Town12 首轮各完成 100 tick/100 RGB 后，于 Town03 `post_load_settings` 阶段 SIGSEGV。独立新服务直接加载 Town03 的 Low 测试也在同阶段崩溃，因此这次 Town03 失败不需要以前一张大地图的累积负载为前提。Epic 新服务 Town03 两次通过，随后 Epic 全地图两轮通过：证据支持当前机器上用 Epic 绕过这一 Low 路径问题，尚不能仅凭黑盒对照确定具体引擎/驱动代码根因。

`defrag-off` 和 `defrag-exec` 的命令行确实包含关闭请求，但运行期 `r.Vulkan.EnableDefrag` 始终为 1，属于未激活修改，不能用于评价“关闭 defrag 能否修复”。隔离 UserDir 的 defrag 测试首个启动样本为 1，之后变成 0；同步 PSO 测试 `r.AsyncPipelineCompile` 初始化后为 `[0,0]`。这两个实际生效的实验仍在 Town03 同阶段崩溃。只读 CVar 采样保存在各运行 `samples.jsonl`；读取器只适用于已校验的本机二进制，不是通用 CARLA 接口。

`-norhithread` 的 54 次线程采样都没有 RHIThread 名称，`-onethread` 同样没有该名称；但 `r.Vulkan.RHIThread` CVar 仍为 `[1,1]`，因此不能只看这个 CVar 判断命令行是否实际创建线程，也不能据 `-onethread` 名称声称所有异步任务都已消失。两次仍发生原生崩溃。端口占用导致未启动的 `rgb-all-no-rhi-thread` 是 harness_error，不纳入该效果比较，表中使用真正执行的 `-2610` 运行。

## 程序版本与历史状态解释

这些记录的 probe/summary schema 均为 1，schema 号不等于程序提交版本。测试期间探针和 supervisor 有修订，每次原始报告没有保存 Python 文件内容哈希，不能追补当前代码哈希冒充当时版本。此汇总保留完整命令、各报告摘要及已知语义变更；与旧 `rgb-all-default` 的 `sensor_tick=0.05` 首轮失败分开。所有纳入且已完成的摄像头回合都明确记录 `sensor_tick=0.0`。

Fresh Town03 Epic 的客户端退出 0，200 帧通过；服务仍存活时 supervisor 明确发出 SIGTERM，服务返回 `143=128+15`，没有 SIGKILL，进程组已清理、日志完整。旧 classifier 将这个预期的显式停止返回码当作 nonzero failure，因此原 `summary.passed=false` 保留不改。按修正后的退出判定，这是预期 SIGTERM 退出；汇总用单独说明表达这一解释，不篡改原始 passed 字段。最新全地图 Epic 使用修正后的判定，原始 `passed=true`、`shutdown_outcome=exited_with_sigterm_status`。

## Epic 每地图、每轮耗时

每格按原始回合原样列出 p50/p95；tick 和 camera wait 单位为毫秒，load 为秒。轮次 1/2 对应 repeat 0/1。这些是每回合 100 次采样的分位数，**没有把 24 个回合的 p95 求平均后当总体 p95**。原报告未保存逐次耗时数组，不能严谨重建总体或两轮合并分布；其他尝试的逐回合原值也保存在 JSON。

| 地图 | 加载第1/2轮 s | tick 第1轮 p50/p95 ms | tick 第2轮 p50/p95 ms | camera wait 第1轮 p50/p95 ms | camera wait 第2轮 p50/p95 ms |
|---|---:|---:|---:|---:|---:|
| Town13 | 22.076 / 24.059 | 1.812 / 2.768 | 1.797 / 2.732 | 7.430 / 10.169 | 2.596 / 3.022 |
| Town12 | 14.994 / 15.396 | 1.541 / 2.299 | 1.685 / 2.442 | 20.048 / 21.395 | 12.393 / 14.318 |
| Town03 | 1.771 / 1.775 | 0.614 / 0.785 | 0.688 / 0.884 | 7.636 / 9.754 | 2.603 / 3.951 |
| Town01 | 1.473 / 1.468 | 0.593 / 1.478 | 0.633 / 1.548 | 4.309 / 5.753 | 2.582 / 2.648 |
| Town02 | 1.352 / 1.341 | 0.529 / 1.725 | 0.583 / 1.817 | 4.051 / 5.677 | 2.575 / 2.604 |
| Town04 | 1.530 / 1.504 | 0.544 / 0.967 | 0.673 / 1.114 | 2.617 / 4.404 | 2.568 / 2.679 |
| Town05 | 1.528 / 1.522 | 0.532 / 0.712 | 0.676 / 1.135 | 2.686 / 4.630 | 2.548 / 2.617 |
| Town06 | 1.582 / 1.564 | 0.537 / 0.716 | 0.694 / 1.063 | 5.850 / 7.386 | 2.551 / 2.607 |
| Town07 | 1.606 / 1.601 | 0.610 / 0.886 | 0.811 / 1.454 | 2.592 / 2.898 | 2.557 / 2.633 |
| Town10HD | 2.275 / 2.278 | 0.674 / 0.779 | 0.716 / 0.886 | 7.462 / 10.765 | 2.602 / 2.695 |
| Town11 | 4.079 / 4.022 | 0.982 / 1.171 | 1.210 / 1.715 | 2.570 / 2.685 | 2.578 / 2.688 |
| Town15 | 2.334 / 2.320 | 0.687 / 1.173 | 0.848 / 1.433 | 2.804 / 4.273 | 2.609 / 2.731 |

数据来源：[Epic probe](rgb-all-epic/probe.json)、[Epic lifecycle summary](rgb-all-epic/summary.json)、[Epic resource samples](rgb-all-epic/samples.jsonl)。GPU 峰值是整卡采样总占用，不是单进程独占显存，也不是所有瞬时峰值的保证。其他程序、缓存及地图加载顺序会影响墙钟耗时；两次加载不能视为充分统计重复性或完整性能基准。

原始地图资产路径（同时保存在每个回合的 asset 字段）：

| 地图 | 精确资产路径 |
|---|---|
| Town13 | `/Game/Carla/Maps/Town13/Town13` |
| Town12 | `/Game/Carla/Maps/Town12/Town12` |
| Town03 | `/Game/Carla/Maps/Town03` |
| Town01 | `/Game/Carla/Maps/Town01` |
| Town02 | `/Game/Carla/Maps/Town02` |
| Town04 | `/Game/Carla/Maps/Town04` |
| Town05 | `/Game/Carla/Maps/Town05` |
| Town06 | `/Game/Carla/Maps/Town06` |
| Town07 | `/Game/Carla/Maps/Town07` |
| Town10HD | `/Game/Carla/Maps/Town10HD` |
| Town11 | `/Game/Carla/Maps/Town11/Town11` |
| Town15 | `/Game/Carla/Maps/Town15/Town15` |

## 2026-09-28 追加：候选组合配置 v2 的 RGB 检查

`rgb-all-conservative-v2` 独立一轮完成相同 12 图×2×100 tick/RGB，共 24 回合和 2,400 帧；客户端退出 0，设置恢复与进程回收通过。服务按显式 SIGTERM 返回 143，无 SIGKILL，原 summary 的 `passed=true`。耗时 **193.901 秒**，采样 RSS 峰值 **7,864,120 KiB（7.500 GiB）**，整卡显存峰值 **8,849 MiB**。不将这轮与前一轮的分位数混合。

候选组合为 Epic + `-norhithread` + 隔离 `-UserDir`。采样确认初始化后 `r.Vulkan.EnableDefrag=0`、`r.Vulkan.WaitForIdleOnSubmit=0`、`r.AsyncPipelineCompile=[0,0]`；首个启动样本的 defrag 仍为 1，后续为 0，未把启动瞬态隐去。全部 189 次线程名采样没有 RHIThread，虽然 `r.Vulkan.RHIThread` CVar 为 `[1,1]`；CVar 值与实际线程是否创建不能混为一谈。配置文件摘要与 profile 中的 SHA256 一致。

配置依据：[v2 profile](conservative-v2-profile.json)、[隔离 Engine.ini 副本](conservative-v2-profile-Engine.ini)、[逐次只读采样](rgb-all-conservative-v2/samples.jsonl)。这些结果只是候选渲染配置证据，**不是原生 Bench2Drive 稳定配置验收**。同一候选在加入地图就绪屏障之前的原生四路线尝试中，第二条初始化 `get_map()` 出现客户端 SIGSEGV；服务当时仍存活，关闭另需强杀。该失败与本次 RGB 通过分别保留，后续屏障修改也需独立验证。[此前原生失败解释](native-rendered-conservative-v2/outcome.json)

下表同样保留每个回合自己的 p50/p95，不计算合并分位数：

| 地图 | 加载第1/2轮 s | tick 第1轮 p50/p95 ms | tick 第2轮 p50/p95 ms | camera wait 第1轮 p50/p95 ms | camera wait 第2轮 p50/p95 ms |
|---|---:|---:|---:|---:|---:|
| Town13 | 22.436 / 23.934 | 1.678 / 2.650 | 1.837 / 2.589 | 7.343 / 9.908 | 2.614 / 3.746 |
| Town12 | 15.123 / 15.470 | 1.629 / 2.391 | 1.604 / 2.341 | 19.891 / 21.927 | 13.313 / 14.587 |
| Town03 | 1.803 / 1.796 | 0.647 / 0.906 | 0.666 / 0.983 | 9.623 / 11.517 | 3.630 / 4.556 |
| Town01 | 1.474 / 1.469 | 0.539 / 1.608 | 0.658 / 1.557 | 4.705 / 5.997 | 2.615 / 2.649 |
| Town02 | 1.370 / 1.350 | 0.516 / 1.763 | 0.566 / 1.894 | 4.924 / 6.152 | 2.621 / 2.729 |
| Town04 | 1.513 / 1.489 | 0.571 / 0.957 | 0.632 / 1.188 | 2.849 / 3.526 | 2.619 / 2.722 |
| Town05 | 1.521 / 1.510 | 0.545 / 0.690 | 0.651 / 1.141 | 2.781 / 3.561 | 2.609 / 2.714 |
| Town06 | 1.585 / 1.570 | 0.635 / 0.873 | 0.676 / 1.058 | 7.323 / 9.366 | 2.640 / 3.120 |
| Town07 | 1.602 / 1.605 | 0.672 / 0.825 | 0.841 / 1.374 | 2.569 / 3.262 | 2.613 / 2.725 |
| Town10HD | 2.280 / 2.271 | 0.648 / 0.869 | 0.723 / 0.905 | 8.660 / 10.358 | 2.693 / 3.268 |
| Town11 | 4.006 / 4.036 | 1.014 / 1.318 | 1.132 / 1.715 | 2.596 / 2.715 | 2.603 / 2.719 |
| Town15 | 2.338 / 2.322 | 0.685 / 1.272 | 0.780 / 1.463 | 4.157 / 5.684 | 2.609 / 2.754 |

原始结果：[probe](rgb-all-conservative-v2/probe.json)、[生命周期](rgb-all-conservative-v2/summary.json)。所有已完成回合均核对为 100 张连续物理帧 RGB；安装资产、旧失败记录及前十组汇总数据未改写。
