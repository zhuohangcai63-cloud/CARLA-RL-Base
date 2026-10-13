# Bench2Drive 平台加固验收

本轮使用 CARLA 0.9.15、固定 Bench2Drive 提交 `acbeb0dd8aa99dd0e71b9b75bb542f6fe7c7f3d8`，服务环境为 `/home/czh/software/miniconda3/envs/CARLA-RL-Base`。当前默认适配依赖副本是 `-p4`；较早长扫进程实际使用 `p3`，每条路线的 `metadata.patch_version` 保留真实版本。不得把 p3 的结果改称 p4 全量通过。

**2026-09-28 后续更新：** [地图专项调查](town13-investigation/README.md)新增 Epic、完整图层、650/650 米配置的全部 12 张地图两轮 RGB 检查，24 次加载与 2,400 张逐帧图像及本次进程生命周期均通过。本文 Town03/Town13 的崩溃、Town13 的 41 条 blocked 和旧 220 条状态表仍保留为历史证据，不代表当前所有配置下地图不可用。后续 Epic 启动、无渲染的 Town13 原生批次仍在第 39 条 `31736` 初始化 SIGSEGV：前 38 条返回（16 条原生结束、22 条达预算），`31736` 结果未知，后两条未运行。地图/主车/摄像头通过不能改写原生路线失败，更不能声称全部场景已稳定。

同日追加：修复客户端地图就绪竞态后，Town12/13 的四条原生短序列及两项 Scenic 运行回归返回，但原生服务停止仍超时；Epic Vulkan 候选配置的 [41 条逐路线重载批次](town13-investigation/native-town13-reload-barrier-v2/outcome.json) 在第 11 条 `3816` 初始化 SIGSEGV，前 10 条返回、30 条未运行。该候选仍未通过稳定性验收，旧清单结果不追改。

## 验证配置与边界

主清单为 `configs/manifests/v004_eval220.json`。每张地图使用独立 Python/TM 生命周期，CARLA 服务实例保持独立；每条路线最多 1500 个物理 tick，固定步长 0.05 秒。参考驾驶器为 CARLA 内置 `BehaviorAgent(normal)`，仅用于执行适配验收，不是学习算法或官方模型基线。路线出生点、场景、背景车和原生评价由 Bench2Drive 提供。

本轮矩阵显式采用 `no_rendering_mode=true`、`map_layers=none`、`tile_stream_distance=500`、`actor_active_distance=500`，使用清单指定的精确地图名，没有隐式 Opt 替换。无渲染、NONE 图层和缩小流送范围会改变运行条件；这些证据不代表全图层、图像传感器或默认远场配置通过。正常会话退出恢复其取得世界时的配置；为避免这台机器恢复渲染后复现 UE4 崩溃，主扫专用 CARLA 在任何环境会话之前显式设置了 `no_rendering_mode=true`，没有修改生产的配置恢复合同。

启动命令：

```bash
/home/czh/software/carla/CarlaUE4.sh -RenderOffScreen -quality-level=Low -nosound \
  -carla-rpc-port=2100 -carla-streaming-port=2101 -carla-secondary-port=2102 \
  -ResX=400 -ResY=300
```

服务就绪后，用原生客户端取得世界设置并将 `no_rendering_mode` 设为 true；这一步只在验收专用实例启动时进行。主扫命令：

```bash
python scripts/validate_native_matrix.py \
  --manifest configs/manifests/v004_eval220.json --ticks 1500 --timeout 45 \
  --no-rendering --map-layers none \
  --tile-stream-distance 500 --actor-active-distance 500 \
  --exclude-town Town13 \
  --exclude-reason 'Repeated native CARLA Town13 initialization crash, including reduced stream radius; isolated HighwayCutIn run succeeded but repeated initialization is unverified' \
  --output docs/testing/platform-bench2drive-matrix220-headless.json
```

Town13 的 41 条路线保留为明确 blocked。另一次独立 HighwayCutIn 3816 在该地图完成过 1500 ticks 的有限执行，但重复初始化仍发生原生崩溃，因此这条孤立记录不能把整个地图升级为支持。

## 结果字段的含义

- `execution=completed` 表示原生路线回合已经结束，包括成功或失败结束；并不表示无碰撞地完成驾驶任务。
- `native_success=true` 只表示 `RouteCompletionTest` 成功。违规、碰撞、RC、IP 和 DS 另见原生 criteria/statistics。
- `tick_budget_reached` 表示程序在预设有限窗口内仍能执行、但没有观测到原生回合结束，属于部分覆盖。
- `failed`、`unknown_outcome`、`blocked`、`not_run` 分别保留，不把环境故障转写成普通驾驶失败。
- `activated` 和 `completed` 两个子场景字段仅在 `scenario_progress_schema=1` 的新版报告中具有实际行为含义。旧版报告误用了外层包装树状态，等待触发就可能计为激活，收尾的 `WaitForever` 又使正常完成无法被计入；旧报告原样保留，旧字段不得用于场景触发或完成统计。新版只核验固定包装结构内实际行为，并保存状态转变的物理帧。

## 专项证据

[场景接管范围](platform-takeover-scope.json)限定到 ParkingCutIn 24416 和 StaticCutIn 22535 的已放置 adversary、`waiting_for_trigger` 阶段、12 次决策采样。L 切换 G 后车辆有真实水平位移和油门控制，控制源保持为替代控制器。原始报告没有记录交接前帧，故明确区分 44 ticks 的首末观测跨度与配置推算的 48 ticks 全窗口；不能由这些短窗口推断之后的原生 actor 生命周期或所有同类路线。

[HighwayCutIn 23842 接管尝试](platform-takeover-23842.json)遇到原生 Signal11，客户端确认服务器退出后终止，没有已完成的 Python 结果可判定所处阶段，结果为未知。该路线在主扫中的原生执行结果与这次实验接管结果分别记录，二者不相互替代。

[p4 原生执行器专项](platform-p4-control-operations.json)在真实车辆上验证：既有 TM 车辆的普通速度直写被拒绝；命名的交叉口速度脉冲在一 tick 后恢复 TM；同 tick 显式关闭 TM 会取消恢复；接管关闭 constant velocity 后制动实际降低车速；旧来源命令被抑制。这是受控执行器操作验证。另一次 [ParkingCrossingPedestrian 24519](platform-p4-crossing.json) 原生执行完成，但速度脉冲计数为零，没有宣称该次自然场景触发了该特定分支。

[Town11 26872 专项复测](platform-town11-26872-retest.json)在最终地图就绪检测代码和 p4 下通过：精确 Town11，256 ticks 原生结束。主扫旧代码在同一地图首次切换时读到旧地图元数据而失败，该历史结果未覆写。

[路线规划等价检查](platform-planner-equivalence.json)在真实 Town05 的三对路线比较缓存与独立构造的路径签名一致，调用方转弯状态独立；这只是静态路线检查。

[三类 cut-in 进度专项](platform-cut-in-progress-v2.json)使用最终 p4 和新版实际行为schema：Parking24416 在 tick62 激活、tick263 行为完成、tick417 路线完成；Static22535 在 tick61 激活，tick392 路线完成时实际行为仍 RUNNING；Highway23842 在 tick38 激活，tick1463 原生失败结束时行为仍 RUNNING。这分别提供等待、执行、收尾及未完成的真实证据，不将某一种结束推广到其他子场景。

[CrossingBicycleFlow 35418 诊断](platform-crossing-bicycle-35418-diagnosis.md)确认了明确构建失败：未改动的固定上游源码和 XML 在实际服务器同摘要的 OpenDrive 中，沿 road264 的自行车 lane−3 前进到路口前即没有后继连接。独立离线解析复现第六次 `next(2)` 为空；适配器没有改选车道或补造路线，而是将上游的打印跳过升级为结构化失败。该配置在当前地图上不能算已支持，不能以“仅路线仍运行”掩盖丢失子场景。

最终接管守卫另修正了高海拔隐藏车辆的误判：不再以世界z大于−20判断放置，而要求已识别树内该actor的原生放置节点实际SUCCESS，且存在预期运动节点。低/高海拔、未放置/成功放置和未知结构拒绝由纯测试覆盖。[Highway23842守卫真实诊断](platform-highway-placement-guard.json)中，隐藏actor的z=261.79895仍高于海平面，正确拒绝接管；56个物理ticks后原生放置完成，z=361.79813，才开放synchronizing_arrival阶段。该新回合没有接管、机制切换或前缀重演，不升级之前Highway实验接管的未知结果。

## 尚未通过的能力

Bench2Drive 原生 TM、行为树和后台交通内部状态不能完整导出，本轮不支持原生公共前缀重演或可比较起点保证。三类 cut-in 的实验接管仍要求显式 `allow_unvalidated_takeover`，每次能力查询按具体角色及阶段给出限制；本轮有限证据不会自动关闭该保护。原生 actor 销毁仍有最高生命周期权限，不为干预延长寿命。

历史失败日志和初次运行报告保留在本目录，包括 Town03 默认渲染加载、Town13 渲染线程/重复初始化、主扫恢复渲染后的崩溃、早期启动未就绪及 Highway 接管结果未知。后续通过只增加新的证据，不抹掉旧错误。

## 全清单状态

初始[220条矩阵](platform-bench2drive-matrix220-headless.json)已在 Town12 的 StaticCutIn22331 初始化期间停止：服务原生 Signal11，客户端随后 `TimeoutException` 引发 abort，子进程返回−6。原报告保留77条原生回合结束、34条预算截止、2条已知失败、22331末次持久化为running、65条not_run及41条Town13 blocked。服务和客户端已经退出，22331的running在派生覆盖表中必须解释为unknown_outcome，不能解释为仍在执行或执行了零tick。

[UE4诊断栈](platform-matrix-town12-native-diagnostics.txt)和[CrashContext](platform-matrix-town12-crash-context.xml)定位到渲染线程 `VulkanRHI::FMemoryManager::AllocUniformBuffer → RHICreateUniformBuffer → FPrimitiveSceneInfo::AddToScene`；错误为SIGSEGV，`bIsOOM=0`。这不是已证实的内存耗尽，也不足以仅凭栈判定驱动或引擎的根本缺陷。显式no_rendering_mode仍未绕开该路径。

随后启动了**唯一一个新CARLA服务**，按[独立65条计划](platform-followup65-plan.json)仅初始化旧表中not_run的路线，先Town15十条、再Town12剩55条；不重跑22331，不恢复任何旧动作。新批次采用p4和实际行为进度schema1，任何再次出现的未知结果都会停止该批次，不循环重启。原始stopped报告不覆盖，新批次不能被表述为一次连续全量稳定运行。后续批次已经完成：Town15为6条原生结束、2条预算截止、2条出生失败；Town12剩余55条为36条原生结束、17条预算截止、2条自行车路径构建失败。两次Python会话均正常清理，CLI的exit1只反映已记录的路线失败。


## 最终覆盖汇总

[220条派生覆盖总表](platform-bench2drive-coverage220.json)按路线记录原报告、实际依赖版本证据、进度schema及专项复测。原始矩阵和子报告均保留原样；总表将已退出子进程留下的22331 running转为unknown_outcome，不假设它没有推进任何初始化物理帧。

| 两批清单原始结果 | 路线数 | 解释 |
|---|---:|---|
| 原生回合结束 | 119 | 包含成功和失败结束；其中116条RouteCompletionTest成功，仍可能有违规 |
| 达到1500 ticks预算 | 53 | 有限运行窗口内无环境故障，但原生回合未结束 |
| 明确失败 | 6 | Town11旧地图元数据错误1条、Biking拓扑断连3条、Town15出生失败2条 |
| 结果未知 | 1 | StaticCutIn22331初始化中外部CARLA崩溃 |
| 明确阻塞 | 41 | Town13反复初始化崩溃；未以其他地图替代 |
| 未运行且未说明原因 | 0 | 所有220条都有上述状态 |

Town11 26872已在修复后的独立复测通过；若采用这条最新证据，原生结束为120条、尚未解决的明确失败为5条，其余不变。它不会把原批次改写为通过。172条两批返回的有限执行共记录145079个step物理ticks；这个数不包含初始化、专项测试或未知请求可能已经执行的ticks，不能当作所有实际物理推进总量。

40/42种场景有真实构建和有限执行证据；HazardAtSideLane、NonSignalizedJunctionLeftTurnEnterFlow没有这样的覆盖。采用新schema的后续批次和三类cut-in专项，共64条有可靠实际行为进度：27种场景实际激活，14种场景的31条路线观察到实际行为完成。旧v1推导字段不计入这些数量，未完成也不等于实现失败。

明确失败清单：35418、35907、35623共享已核验的Biking lane断连；26537 ParkingExit无法在前方停车点生成车辆；24693 ParkingCrossingPedestrian无法生成adversary。后二者记录为原生生成失败，没有足够证据进一步归因于哪个碰撞物或蓝图，不猜测，也未通过换seed或改出生点消除记录。

主批次与后续批次实际使用不同依赖补丁；原始覆盖中113条有p3执行进程或调用栈证据、65条有p4证据，Town11旧load失败发生在依赖准备前，41条blocked没有依赖执行证据。专项复测版本另列。不能称最终p4完整220条通过，更不能称一次连续运行覆盖220条。

[模拟器生命周期记录](platform-simulator-lifecycle.json)把会话清理与外部进程停止分开：后续65条及最终守卫诊断的Python会话正常关闭后，向本次自启的CARLA PID321652发SIGTERM，外部引擎在Vulkan资源堆析构路径返回139。日志和原生诊断均保留，属于模拟器退出故障；没有把它改写成前述路线运行中的失败。本轮自启CARLA与测试客户端均已退出，没有留下推进世界的进程。
