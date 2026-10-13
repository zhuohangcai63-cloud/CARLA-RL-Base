# 已结束真实测试的独立证据核对

核对日期：2026-09-28。只读取以下五组已结束的原始记录，不包含后续正在执行的测试，不启动模拟器。本附录区分客户端有限执行、驾驶任务、原生崩溃和进程关闭；`probe=passed` 或 `client_exit=0` 不代表整次运行通过。

## 执行与关闭分别统计

| 已结束运行 | 确认的执行结果 | 客户端退出 | 服务退出与整次结论 |
|---|---|---:|---|
| `baseline-fresh-readiness` | Town13 3 次加载，各 100 tick；无渲染、500/500 米，`spectator_as_ego=true` | 0 | 客户端结束时服务存活；SIGTERM 后需 SIGKILL，`-9`；关闭未通过 |
| `no-spectator` | Town13 3 次加载，各 100 tick；无渲染、500/500 米，`spectator_as_ego=false` | 0 | 客户端结束时服务存活；SIGTERM 后需 SIGKILL，`-9`；关闭未通过 |
| `native-town13-default-rhi` | 41 条清单中前 38 条返回：16 `completed`、22 `tick_budget_reached`；30,192 个 step tick，另记录 292 个 initialization tick | -15，父进程在服务退出后终止客户端 | 第 39 条 `31736` 初始化期间服务 SIGSEGV，`-11`；整批失败，后两条未尝试 |
| `native-town13-tail-default` | 新服务单独执行 `31736/31654/31124`：402/400/1,000 个 step tick，共 1,802；另 18 个 initialization tick。前两条 `completed`，第三条达预算 | 0 | `status=client_passed`，但关闭需 SIGKILL，`-9`；摘要明确 `passed=false` |
| `rgb-all-default` | 只尝试 Town13 首回合；2 个显式 tick，收到 frame 28 图像，等待 frame 29 超时；其他地图和重复回合未执行 | 1 | 超时时服务仍存活；随后关闭等待约 90 秒仍未完成，SIGKILL、`-9`；整次失败 |

原始依据：[baseline probe](baseline-fresh-readiness/probe.json)、[baseline lifecycle](baseline-fresh-readiness/lifecycle.json)；[no-spectator probe](no-spectator/probe.json)、[no-spectator lifecycle](no-spectator/lifecycle.json)；[41 条 probe](native-town13-default-rhi/probe.json)、[41 条 lifecycle](native-town13-default-rhi/lifecycle.json)、[41 条 outcome](native-town13-default-rhi/outcome.json)；[尾部三条 probe](native-town13-tail-default/probe.json)、[尾部三条 summary](native-town13-tail-default/summary.json)；[RGB probe](rgb-all-default/probe.json)、[RGB summary](rgb-all-default/summary.json)。

`completed` 表示原生路线执行结束，不能等同于所有评价项无违规；`tick_budget_reached` 表示有限执行返回，不能等同于完成路线。30,192 和 1,802 都是已返回路线的 step tick 合计，不含未返回初始化、CARLA 内部加载步进，也不能称为服务生命周期内所有物理帧总数。

## 原生崩溃与重测不能合并为“41 条通过”

长批次第 39 条 `31736` 为 `VehicleOpensDoorTwoWays`。客户端最后有 `LOADED 31736`，没有 `PREPARED 31736`；服务日志明确 `Signal 11`。因此能定位到原生场景准备阶段，尚不能仅凭日志确定是哪一个具体 API 或资产导致异常。[客户端末尾](native-town13-default-rhi/client.txt)、[服务日志](native-town13-default-rhi/server.txt)

该批 `probe.json` 顶层和第 39 条仍为 `running`，是原生崩溃和父进程终止打断写入留下的中间状态，**不表示任务仍在执行**。必须连同 `outcome.json` 解读为“第 39 条结果未知/未完整返回，后两条未尝试”。记录的 `ticks=0` 不能证明准备过程中没有任何内部物理帧。

尾部三条使用新的服务进程，而且 `reload_on_reset=true`；长批次是 `false`。它补充了这三条在另一轮有限执行中的证据，同时原批次第 39 条崩溃仍然成立。不能把前 38 条与重测三条相加后称为一轮 41 条稳定通过，也不能声称问题已经修复。

## RGB 首轮失败的边界

首轮探针使用非零 `sensor_tick=0.05`；它在 0.05 秒物理步长下等待每帧，但第二张预期图像未送达。此处的直接观测是 `TimeoutError: Camera did not deliver physical frame 29`，不是服务 SIGSEGV。frame 28 为 320×180 图像，空间标准差约 61.54，确认至少获得过非恒定画面；不能据此宣称完整传感器回合通过。

证据限制：旧 `probe.json` 没有单独保存 `sensor_tick` 属性，0.05 来自该次探针执行及后续修订记录；原始 JSON 单独能证明的是缺帧与超时。现有探针已改为显式逐帧的 `sensor_tick=0.0`，属于需要独立重新验证的修改，不能回填首轮结果。[探针修订说明](diagnostic-runner.md)

RGB 原始顶层 `stage=restore_settings` 是 finally 清理后的最后阶段；真正失败阶段保存在首回合 `stage=camera_wait` 和 traceback 中。恢复设置 RPC 已返回，但这不证明随后引擎退出完成。客户端结束到 supervisor 结束约 91 秒，与本次配置的 90 秒关闭宽限相符；summary 没有独立保存宽限参数，明确保存了 SIGTERM→SIGKILL、`shutdown_forced_kill=true` 和 `passed=false`。

## 资源记录核对

下列峰值从保存的 samples 重新计算，与相应 outcome/summary 一致。RSS 是服务进程采样值；GPU 是整张卡当时的总占用，不是 CARLA 独占占用。轮询峰值不是连续监测的绝对峰值，关闭等待阶段也不一定持续采样。

| 运行 | 服务 RSS 采样峰值 GiB | GPU 采样峰值 MiB | 可用系统 RAM 最低采样值 GiB |
|---|---:|---:|---:|
| baseline | 11.638 | 6,878 | 14.116 |
| no-spectator | 8.504 | 6,482 | 17.494 |
| Town13 长批次 | 8.527 | 6,362 | 17.066 |
| 尾部三条 | 7.493 | 7,715 | 17.076 |
| RGB 首轮 | 7.107 | 6,296 | 19.240 |

前两组每回合 tick 平均墙钟耗时分别约 0.407–0.430 秒和 0.026–0.028 秒，最大 tick 从约 37.1–39.8 秒降到约 1.64–1.65 秒；这是关闭 spectator 流送后的改善证据，但测试顺序、缓存和短样本会影响数值，不能外推为完整基准性能或崩溃根因证明。

本次长批次 SIGSEGV 前的采样没有显示普通“RAM/显存容量耗尽”，但采样不能排除瞬时峰值、碎片、驱动或硬件错误。记录中的 PCIe 可纠正错误也出现在正常返回的运行中，单靠计数不能建立它与 SIGSEGV 的因果关系。SIGKILL 关闭记录不能改写成“又发生了一次 Vulkan 崩溃”。
