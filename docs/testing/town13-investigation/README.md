# Town12 / Town13 与全地图稳定性排查

日期：2026-09-28。当前 CARLA 0.9.15 / UE 4.26.2，RTX 5060 Ti 16 GB，驱动 580.178.04。
本页保留成功和失败的原始材料；有限执行通过不等于所有场景、感知负载或长时间运行都已验证。
**安装完整性与 Epic 全地图 RGB 检查已通过；Town12/Town13 能加载和运行，但原生路线长批次仍会崩溃。最新“候选配置 v2＋地图就绪屏障＋每路线重新加载”在第 11 条发生服务端 SIGSEGV，不能作为稳定方案推荐。**
地图加载、逐帧相机、原生场景执行和服务正常退出分别验收；下表保留各阶段结果，不用短检查通过覆盖长批次失败。

**最新追加：** 仅开启 `r.Vulkan.UseBufferBinning=1` 的同序列对照也失败：前 5 条返回，第 6 条 `29893` 初始化时顶点缓冲区上传向空地址写入，后 5 条未运行。较大缓冲区仍有 4/8 字节越界，因此该开关也不能作为稳定配置推荐。[本次结果](native-town13-binning-validation11/outcome.json)及[缓冲区分析](vulkan-validation-notes.md)保留证据；原生根因尚未闭合，所有地图长期稳定的目标未达成。

## 已取得的结果

| 检查 | 实际结果 | 范围 |
| --- | --- | --- |
| 官方主包、附加包与安装内容 | 通过 | 44,711 个合并文件路径全部匹配 |
| Town13 spectator 单变量对照 | 每组 3 次、每次 100 tick 均执行成功；旧工具退出需强杀 | Low、无渲染、500 m、相同 34183 起点 |
| Low 下 Town13 41 路线批次 | **失败**：第 39 条初始化 SIGSEGV | 前 38 条返回，30,192 个 step tick；后两条未运行 |
| Epic 启动、无渲染的 Town13 41 路线批次 | **失败**：仍在第 39 条 `31736` 初始化 SIGSEGV | 前 38 条返回：16 条原生结束、22 条达到预算，29,914 个 step tick；`31736` 结果未知，后两条未运行 |
| Low 下尾部 3 路线独立重建 | 客户端执行返回；进程停止未通过 | 2 条原生结束、1 条达到 1,000 tick 上限，停止需强杀 |
| Low 下有效逐帧 RGB 全图序列 | **失败** | Town13、Town12 各 100 RGB 后，Town03 原生崩溃 |
| 新进程单独 Town03：Low / Epic | Low 崩溃；Epic 两次加载、各 100 RGB 通过 | 原 Epic 退出分类器误判 143，原报告保持不改 |
| **Epic 全 12 图，两轮 RGB** | **通过，包含设置恢复及拥有进程停止** | **24 次加载、2,400 个显式物理 tick 与对应 RGB；帧号在各回合内连续，193.37 s** |
| Epic 候选组合配置 v2，全 12 图两轮 RGB | **本次 RGB 与生命周期通过；后续原生长批次失败** | 24 次加载、2,400 tick/RGB，193.901 s；实际关闭 defrag/异步 PSO，采样无 RHIThread |
| v2＋就绪屏障，Town12/13 四条原生路线 | 4 条运行返回；**服务退出失败，整次未通过** | 3 条原生结束、1 条达到预算，2,240 个 step tick；客户端退出 0，停止服务需 SIGKILL |
| v2＋就绪屏障，Scenic Town12/13 回归 | **2 项运行测试通过；服务退出失败，整次未通过** | 每图 15 次动作、60 个 step tick；hero/地面高度/时钟/清理/设置恢复通过，无 NPC、无 RGB |
| **v2＋就绪屏障＋逐路线重载，Town13 完整 41 条清单** | **失败：第 11 条 `3816` 初始化服务端 SIGSEGV** | 前 10 条返回：4 条原生结束、6 条达到预算，7,847 个 step tick；`3816` 结果未知，30 条未运行；整次耗时 648.211 s |
| 同配置启用 Vulkan 验证层，**仅前 11 条子集** | **失败：第 11 条 `3816` 在 `LOADED` 后服务端 SIGSEGV，结果未知** | 10 条返回＝4 条原生结束＋6 条预算，7,847 个 step tick，701.352 s；[本批结果](native-town13-validation-sequence11/outcome.json)、[验证层证据](vulkan-validation-notes.md)。已观察到 buffer 越界 VUID，但尚未证明它是所有 SIGSEGV 的唯一根因 |
| 无 CARLA 自动化 | **628 passed, 18 skipped，18.02 s** | [本轮结果](pytest-map-final.txt)、[JUnit](unit-results-map-final.xml)、[当前源码摘要](current-code-provenance.json)；协议、fake world、配置、回归与诊断工具，不是原生仿真证明 |

全图检查使用完整图层、650/650 m、`spectator_as_ego=False`、物理步长 0.05 s、
320×180 单 RGB、每物理帧采集。整卡显存采样峰值 **8,496 MiB**，服务 RSS 峰值
**8,090,380 KiB**。这些是本次采样峰值，不是最大资源保证；高分辨率多相机或与训练模型
共用 GPU 的负载没有在这组测试中验证。

地图为 Town01、02、03、04、05、06、07、10HD、11、12、13、15。
原始数据和逐地图两轮加载、tick、图像等待分位数见
[渲染结果汇总](render-results.md)、[结构化汇总](render-results.json)、
[Epic 全图原始结果](rgb-all-epic/probe.json)、[生命周期](rgb-all-epic/summary.json)。

新增的 [候选组合配置 v2](conservative-v2-profile.json)另一次 RGB 检查也通过，
实际读回 Defrag=0、WaitForIdleOnSubmit=0、AsyncPipelineCompile=[0,0]，
189 次线程采样均无 RHIThread；`r.Vulkan.RHIThread` 的 `[1,1]` 不表示实际创建了该线程。
v2 的采样显存峰值为 8,849 MiB、RSS 为 7,864,120 KiB，与上面普通 Epic 的峰值分别记录。
这不是原生路线稳定性通过：加入地图就绪屏障之前的同候选原生测试发生过客户端
`get_map()` SIGSEGV，旧失败保持，[结果解释](native-rendered-conservative-v2/outcome.json)。
加入屏障后四条短序列返回，但随后完整清单在第 11 条服务端崩溃；RGB 通过不替代原生路线验收。
[v2 RGB 原始记录](rgb-all-conservative-v2/probe.json)、[v2 生命周期](rgb-all-conservative-v2/summary.json)

## 安装没有发现缺损

[完整资源审计](package-audit.md)核验了官方 S3 对象身份、gzip CRC、每个文件内容及
附加包覆盖关系。主包中的 6,240 个安装差异均恰好是官方附加包的覆盖内容；
合并的 44,711 个路径中没有缺失或未解释差异。该结论限于归档中的路径，不声称
扫描了安装目录内所有归档外文件。不需要重新下载、重新导入或覆盖当前安装。

## 定位到的触发条件与工程修正

### Low 质量预设是本机一个可复现的崩溃触发条件

有效 RGB 流程在 Low 下反复于 Town03 触发 `FVulkanVertexInputStateInfo::Generate`
空指针读取（地址 0x94）；新进程单独加载 Town03 同样失败。仅切换 Epic 后，
Town03 两次和全 12 图两轮检查通过。原生文件、地图资产、驱动没有更换。

源码中 Low/Epic 同时涉及道路材质处理、剔除和多项渲染设置，因此只能先定位到
**质量预设组合的触发差异**，不能宣称已证明某一条 CVar 是 C++ 根因。
关闭 RHIThread、单线程、真正关闭 Vulkan defrag、关闭异步 PSO 编译的单独对照
都没有消除 Low 下的 Town03 崩溃，不作为稳定配置推荐。

`-ini:Engine:...` 和 `-ExecCmds=...` 两次尝试未改变进程内的 defrag 值，
不能算作关闭后的对照；后来通过隔离 `-UserDir` 的配置成功读回 0。
只读核验未改写进程内存、未暂停仿真器。
详见[原生栈与配置核查](native-stack-readonly.md)。

启动文档和诊断工具默认值已改为 Epic。画质会改变相机输入，实验应记录外部
CARLA 启动命令；环境服务不能从 CARLA API 自动证明所有引擎启动参数。

### 大地图的多余流送负担已经修正

原工程继承 `spectator_as_ego=True`。没有 hero 时，spectator 会额外驱动大地图加载。
本机 Town13 加载后距离还会被原生地图初始化重置为 2,000 m，
`reset_settings=False` 并不保证预加载设置一直生效。
现在在加载前后设置、读取并校验实际值，拒绝未生效或非有限的返回值。

相同 Town13 起点的纯 API 对照（Low、无渲染、500 m、3×100 tick）：

| spectator 流送 | 每轮最长物理 tick | 采样峰值 RSS | 整卡显存峰值 |
| --- | --- | --- | --- |
| 开启 | 37.1–39.8 s | 11.64 GiB | 6,878 MiB |
| 关闭 | 1.64–1.65 s | 8.50 GiB | 6,482 MiB |

这是**最长 tick 等待**，不是整个地图加载时间；加载 RPC 仍需约 27–31 秒。
它证明了加载负担和超时风险的改善；两组都未复现运行中 SIGSEGV，不能把这项优化
单独当作所有原生崩溃的修复。[对照原始汇总](initial-ab-summary.json)

Scenic 自车原来使用原生 `role_name=ego`，大地图管理器不将其视为 hero。
现在改为 CARLA 认可的 `hero`，内部语义身份仍为 `scenic/ego/1`，避免关闭
spectator 流送后真实自车附近的 tile 未被正确保留。

### 地图就绪屏障修正客户端初始化顺序，不覆盖服务端渲染崩溃

CARLA 0.9.15 的 `load_world()` 可以在 Traffic Manager 完成新地图重建前返回；
此时应用立即 `get_map()` 会与内部初始化重叠。本机一次客户端 SIGSEGV 的 Python
调用位置就在这里。现在先注册新 episode 回调，等待目标 episode 的初始化通知，
再读地图；旧 episode 通知不能解除等待，超时明确报错，结束或异常均清理回调。
等待本身不调用 tick、不重发 load，不额外延长动作窗口。
[上游顺序依据与客户端故障边界](client-reload-upstream.md)

修正后的 [四条原生路线](native-rendered-barrier-v2/outcome.json)覆盖 Town12 的 `22331`
与 Town13 的 `3816/31736/31654`，共返回 2,240 个 step tick；其中 `3816` 达到预算，
不能称为四个驾驶任务成功。[Scenic 回归](scenic-large-maps-barrier-v2/outcome.json)在两张图
各执行 15 次制动动作，确认自车 `hero`、地面高度、闲置不推进、每动作四个 tick、
仅清理自有资源和关闭环境后恢复原世界设置。其 scope 不含 NPC、RGB 或路线基准。
这两组客户端都正常退出，但 CARLA 收到 SIGTERM 后未及时停止，最终需要 SIGKILL；
因此它们是运行检查通过、进程生命周期失败，不能将整次测试标为通过。

随后完整 41 条清单仍发生服务端崩溃，详见下文。该结果没有否定初始化顺序修正的作用，
也没有证明客户端问题在所有场景下消失；服务端 Vulkan 路径仍需独立排查。

## 使用配置

```bash
# 独立 CARLA，保留 GUI；无窗口运行时另加 -RenderOffScreen
/home/czh/software/carla/CarlaUE4.sh -quality-level=Epic -carla-rpc-port=2000

# 另一个终端，环境仓库与服务 Conda 环境中
easycarla serve --instance carla_01 --config configs/bench2drive_eval.yaml
```

`bench2drive_eval.yaml` 使用显式 650/650 m、全部图层、启用渲染、关闭 spectator 流送
和 90 秒 RPC 超时，避免继承另一张地图的状态。流送/激活范围影响远处 NPC 休眠，
需要计入实验配置；该值也用于所引用的官方 leaderboard evaluator。

其他课题需要只调整某些地图时，使用 `simulation.map_overrides`。
`bench2drive_low_load.yaml` 是 Town12/13 选择性覆盖示例，其余地图保留明确的基础距离；
它与本次全局 650 m 的全图诊断不是同一个配置。
接口、坐标、动作尺度、奖励权重没有因这些优化改变。设置、覆盖项和实际读回值
记录在 info 和重演材料中，旧材料缺少新字段时必须重新记录。
[按地图配置及限制](../../map-performance.md)、[行为变更](../../behavior-changes.md)

本次还保留了[隔离 Vulkan 配置的复现说明](../../carla-vulkan.md)。其中候选 v2 使用
独立 UserDir，不覆盖 CARLA 安装；它通过了有限 RGB 检查，但完整 Town13 原生清单失败，
仅供有明确记录的诊断对照，不是已验证的稳定运行方案。

## 失败材料与边界

Low Town13 长批次在第 39 条 `31736 / VehicleOpensDoorTwoWays` 初始化发生
Vulkan 网格上传 `memcpy` 非法写，和 Town03 的空指针栈不同。
该时刻采样仍有约 17 GiB 可用 RAM、整卡显存约 4 GiB，没有 OOM 证据；
不能仅靠相邻 Vulkan 栈认定两个故障完全同因。
[批次结果解释](native-town13-default-rhi/outcome.json)、[崩溃栈](native-town13-default-rhi/crash/Diagnostics.txt)

后续 Epic 启动的原生批次同样在第 39 条 `31736` 的 `LOADED` 后、`PREPARED` 前
发生 SIGSEGV，服务退出 `-11`，客户端随后被父进程终止。此前返回 38 条，
其中 16 条 `completed`、22 条 `tick_budget_reached`，确认 29,914 个 step tick。
原始 `running` 是崩溃打断写入的中间状态；`31736` 必须视为结果未知，另两条
`31654/31124` 未运行。该批仍使用 `no_rendering_mode=true`、完整图层和 650/650 米，
与已通过的有 RGB 地图检查分开解读；Epic 并未消除这一原生场景长批次故障。
[Epic 原始路线记录](native-town13-epic/probe.json)、[Epic 生命周期](native-town13-epic/summary.json)

最新 v2＋就绪屏障批次开启渲染、每路线重新加载，完整 41 条清单在第 11 条
`3816 / HighwayCutIn` 的 `LOADED` 后、`PREPARED` 前发生服务端 SIGSEGV（`-11`）。
此前 10 条返回，4 条 `completed`、6 条 `tick_budget_reached`，确认 7,847 个 step tick
和这些已返回路线的 74 个初始化 tick；后 30 条未运行。`3816` 的原始 `running/ticks=0`
是写入被崩溃打断的中间状态，执行结果未知，不能当作正常零步转移。
父进程随后终止客户端（`-15`），不是另一次已观测的客户端 SIGSEGV。

该次栈从 `libnvidia-glcore.so.580.178.04`、`libGLX_nvidia.so.0` 进入
`VulkanRHI::FStagingManager::AcquireBuffer` 和 index-buffer 创建路径。
这是故障位置证据，尚不足以唯一归因于驱动、资产或硬件；候选 v2 没有消除该故障。
整次包装器墙钟耗时 648.211 s，采样 RSS 峰值 9,570,276 KiB、整卡显存峰值 8,367 MiB；
这些不是纯物理推进耗时，也不构成 OOM 证据。复制到仓库的 CrashContext 已移除机器身份字段，
原 CARLA Saved 文件未改动。
[本批结果解释与栈摘要](native-town13-reload-barrier-v2/outcome.json)、
[原始路线记录](native-town13-reload-barrier-v2/probe.json)、
[进程生命周期](native-town13-reload-barrier-v2/summary.json)

旧工具首次 readiness 连接复用问题、RGB `sensor_tick=0.05` 漏第二帧、
停止分类未接受显式 SIGTERM 的 143 返回码，以及 TIME_WAIT 预检误报分别修复并
保留原失败记录。它们不计为地图通过。正式 probe 使用 `sensor_tick=0.0`，
逐帧匹配并检查空间变化，缺帧立即使检查失败，不增加 tick 补帧。
[已结束早期证据审计](completed-evidence-audit.md)

本次结果不替代完整 220 路线、长时负载、全部传感器和 NPC 接管/重演验收。
旧版报告中的 Town12 地图拓扑/场景构建失败也没有因地图 RGB 测试通过而消失；
不能通过修改官方路线、删除失败记录或周期性重启来声称基准通过。

## 上游资料与系统检查

[CARLA #4966](https://github.com/carla-simulator/carla/issues/4966) 有 Low 下重新加载崩溃、
移除 Low 后改善的用户报告；[Bench2Drive #234](https://github.com/Thinklab-SJTU/Bench2Drive/issues/234)
有其他硬件/系统上的 Town12/13 长跑崩溃报告。它们支持这些调查方向，
不等于维护者确认本机每个崩溃都有统一修复。详细版本、源码和候选限制见
[上游核查](upstream-research.md)。

已有 correctable PCIe AER 计数增长，没有观察到相应 Xid、OOM 或 MCE 证据。
CPU 当前微码为 0x133，不能因 14 代 CPU 型号就判断缺失 Vmin 缓解。
未更换驱动、内核、BIOS 或 CARLA 安装内容；没有证据要求先升级硬件。

复现工具：[资产审计](../../../scripts/audit_carla_assets.py)、
[纯 API probe](native-probe.md)、[拥有进程包装器](diagnostic-runner.md)。
