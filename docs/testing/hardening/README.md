# 平台加固验收（2026-09-27—28）

这是第二轮架构加固的证据目录。首轮历史报告保留在上级目录；本轮没有将失败记录改写成通过。平台核心与接口得到实质加固，但整个 Bench2Drive 基准尚未达到稳定验收标准。

**2026-09-28 后续更新：** [地图稳定性专项调查](../town13-investigation/README.md)已在 Epic 画质、完整图层、650/650 米流送/激活范围下完成全部 12 张地图各两轮、每轮 100 张逐帧 RGB：共 24 次加载、2,400 帧，包含本次进程生命周期通过。下文 Town03/Town13 崩溃及旧 220 条清单的 blocked 数量是当时配置下的历史记录，不能解释为这些地图目前一概不可用。后续 Epic 启动、无渲染的 Town13 原生批次仍失败：前 38 条返回（16 条原生结束、22 条达预算），第 39 条 `31736` 初始化 SIGSEGV、结果未知，后两条未运行。地图/摄像头通过不等于全部原生场景稳定，旧清单数据保持不变。

同日追加：修复客户端地图就绪竞态后，Town12/13 的四条原生短序列及两项 Scenic 运行回归返回，但原生服务停止仍超时；Epic Vulkan 候选配置的 [41 条逐路线重载批次](../town13-investigation/native-town13-reload-barrier-v2/outcome.json) 在第 11 条 `3816` 初始化 SIGSEGV，前 10 条返回、30 条未运行。该候选仍未通过稳定性验收，旧清单结果不追改。

## 架构与行为

- 场景实现统一 `EpisodeProtocol`：生命周期、控制变换、结束事实和版本化状态导出。普通 step 不再按来源名称分支。
- `ControlGateway` 统一控制归属，在 tick 前确认批量控制提交；处理 TM、手动控制、walker、目标速度与恒速执行器的交接。同来源重复写合并，不同来源冲突报错。
- Scenic 事件采用独立组合组件；明确配置/状态；全局生成也遵守 `max_active` 预算，部分初始化失败继续清理其它资源。
- 每个物理帧捕获一份不可变状态，供观测、轨迹、NPC 和交通规则复用。静态地图使用有界只读缓存。
- 实验扩展通过 `ExperimentSession` 与明确运行端口协作。recipe v2 校验源配置、物理加载配置及控制器状态；重建不修改服务默认场景选择。
- reset、回合故障、客户端断开、服务退出分别处理。已完成前缀可在故障后导出，客户端会话结束释放全部实验材料。
- 配置文件路径与调用目录解耦；服务和客户端均可从 wheel 独立安装；增加 Linux CI 和可复用验收脚本。

接口仍是 `CarlaEnvClient.reset/step/close`，动作尺度及奖励权重未因本轮重构改变。详细行为变化见[变更记录](../../behavior-changes.md)。

## 自动化测试

最终服务环境离线验收 **511 通过、15 跳过（15.24 秒）**，覆盖契约与故障用例，原始输出见 [pytest 输出](pytest.txt) 和 [JUnit](unit-results.xml)。这组测试使用真实跨进程 POSIX 通信，但不连接 CARLA；默认跳过的真实入口不能算通过。

[全新 wheel 安装报告](../wheel-install-hardening.json)使用两套独立 venv，而不是开发目录导入；[core-only 测试](../core-only-ci-hardening.json)在不含 CARLA/Scenic/Torch 的新 Python 3.11 环境中取得 **468 通过、26 跳过**。真实仿真服务与客户端另外使用两个独立 Conda：`CARLA-RL-Base` / `carla-client-test`。依赖一致性检查均实际运行。

重点回归涵盖请求重复/冲突、旧会话、借用视图覆盖、副本保持、容量溢出、超时未知结果、通知后异常、并发 close、强制退出、重连、资源残留、控制权冲突、传感器缺帧/乱序、清理部分失败和配方预检。CI 文件已提供，远程 CI 尚未运行。最终代码文件摘要与关键依赖记录在[版本核对](code-verification.json)。

## 真实 CARLA

- [原生传感器帧同步](carla-sensors.json)：RGB/BEV/LiDAR 对齐目标帧，客户端等待不推进、最终帧不自动 reset。本轮完整 `tests/test_carla_integration.py` 曾运行 **10 passed / 119.54 s**。
- [Scenic 场景覆盖](scenic-coverage.json)与[事件说明](scenic-notes.md)：区分生成、触发、完成、跳过及驾驶碰撞。后续预算修复又重跑 parking/construction 共四回合，并增加完成路径专项验证。每类完成证据不等于任意随机回合必定完成。
- [公共前缀重复性](prefix-repeatability.json)：固定 Town10HD、seed 7、两个自有 NPC，5 轮各执行 5 个公共前缀决策，再比较 3 个相同动作续行决策；**5/5 通过原容差**，没有增加 settling tick。每次重演含重建约 4.2 秒。[最终版本复测](prefix-repeatability-final.json)在显式 headless、MapLayer.NONE、500 m 配置下再次 5/5 通过，重演含重建约 3.3 秒；两组配置分别记录。该证据局限于本机、该配置和短时间窗；没有将所有场景的 `verified` 改成 true。
- [外部轨迹跟踪与制动](carla-trajectory.json)：真实推进、过期参考帧在推进前拒绝、速度下降到停止阈值；专项测试 **1 passed / 10.17 s**。
- [机制切换](carla-mechanism-switch.json)、[汇入](carla-reactive-merge.json)、[交叉通行](carla-reactive-crossing.json)各有真实闭环记录。模型、分支调度和研究标签仍在平台外。
- Bench2Drive [最终清单覆盖](../platform-bench2drive-coverage220.json)及[专项报告](../platform-bench2drive-report.md)保留 native run、场景激活、路线完成、驾驶成功和测试预算耗尽的区别。旧长扫的外层行为树 activation/completion 指标不能证明真实触发；修复后的版本使用实际行为节点并记录物理帧。Town13 曾单次运行 HighwayCutIn 1500 tick，但再次初始化发生原生崩溃；没有据此宣称该地图稳定。

[两批独立仿真运行按唯一 route ID 汇总](../platform-bench2drive-coverage220.json)后，220 条清单的状态如下。原始主批在 Town12 路线 22331 初始化时原生崩溃；新批只执行原先未运行的 65 条，没有重放该未知回合。两批使用的适配版本和事件遥测版本分别记录，不能称作同版本的一次连续稳定运行。

| 清单状态 | 条数 | 含义 |
|---|---:|---|
| 原生回合结束 | 119 | 包括驾驶成功或失败结束，不能直接解释为无违规完成 |
| 物理 tick 预算耗尽 | 53 | 有限窗口执行，尚未观察到原生回合结束 |
| 明确失败 | 6 | Town11 旧加载确认错误 1 条（已有通过复测）；断连自行车道 3 条；演员生成失败 2 条 |
| 执行结果未知 | 1 | Town12 22331 原生服务崩溃，无终态结果，未重放 |
| 明确受阻 | 41 | Town13 重复初始化崩溃，未纳入这两批执行 |

失败没有被默认回退或只有路线的回合掩盖。Town12 崩溃的原生堆栈位于 Vulkan 渲染线程分配路径；日志不支持系统内存耗尽的判断，根因仍未确定。

最终[高海拔放置守卫](../platform-highway-placement-guard.json)也完成真实验证：HighwayCutIn 23842 地下等待时明确拒绝，56 个物理 tick 后原生放置完成才开放阶段能力。这次仅验证权限判断，没有执行接管，因此不覆盖此前接管尝试的未知结果。

重演复现：

```bash
python scripts/validate_repeatability.py --port 2200 --trials 5 \
  --output /tmp/prefix-repeatability.json
```

Bench2Drive 使用显式 headless、MapLayer.NONE、500 m 流式加载范围的检查与渲染完整场景分开报告；这些配置会改变远场内容，不能冒充官方原样全量渲染评测。

## 两端联调与耗时

[独立服务生命周期](service-isolation.json)、[结构化观测](structured-service.json)、[多模态](sensors-service.json)、[混合场景](mixed-service.json)保留机器测量。混合场景修复前的[失败报告](mixed-service-before-cv-fix.json)与[完整异常](mixed-service-before-cv-fix.txt)也保留。

每种观测各 2 回合、60 次 step；每步执行 4 个物理 tick，物理时间 0.2 秒。下表是本机并行运行其它专用测试实例时的墙钟耗时，单位毫秒：

| 观测 | 通信/编解码/复制 p50 / p95 | 物理 tick p50 / p95 | 整体请求 p50 / p95 |
|---|---:|---:|---:|
| 结构化 | 0.96 / 1.15 | 32.50 / 39.36 | 60.95 / 67.84 |
| RGB + BEV + LiDAR | 1.48 / 1.79 | 23.69 / 31.80 | 141.05 / 164.46 |

物理 tick 不包含控制提交。多模态的传感器等待 p50 约 95 ms，结构化约 8.4 ms；观测构建约 17.6 ms；控制提交约 1 ms。通信项包含调度、编码和复制，不是纯 memcpy 基准；不能将并发测试环境中的物理耗时差异解释为传感器提升仿真性能。

## 尚未证明的范围

全 220 路线稳定执行、所有原生角色接管/机制替换，以及任意地图/种子的长前缀重复性均不能由本轮局部通过推断。原生模拟器崩溃、测试预算内未结束及未验证场景都保留明确状态。这些限制是后续研究选择场景时需要参考的能力边界。

专用 2300 仿真器在所有 Python 会话关闭后由测试进程显式停止；原生退出路径出现 Signal11，进程已回收、共享内存无残留。该问题与已完成的回合结果分开记录，见[清理证据](dedicated-server-cleanup.json)，不宣称原生模拟器正常退出。

全部验收结束后的[资源核对](final-cleanup.json)确认测试仿真进程已退出、POSIX共享对象为空。Bench2Drive专用实例的[生命周期记录](../platform-simulator-lifecycle.json)同样保留显式停止时的原生退出故障；稳定锁文件按协议保留，依赖副本和可复用Conda环境不删除。
