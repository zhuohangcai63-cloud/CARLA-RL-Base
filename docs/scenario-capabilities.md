# 场景能力与实际验收

“已实现接口”“在某条真实路线运行过”“具有已验证的可比较公共前缀”是不同结论。当前能力不会因为相同seed或成功导入原生类而自动升级。

## Bench2Drive

| 能力 | 实现 | 真实验证范围 | 限制 |
|---|---|---|---|
| 可执行路线、原生行为树、背景车、评价 | 已实现，统一控制网关和单一步进端 | 新版220清单逐条有界验收，见下方历史矩阵及后续补充 | 历史矩阵为无渲染、NONE图层、500m流送配置；不是全图层官方性能评估 |
| 原生子场景实际激活 | 已实现精确行为节点观测 | 新版进度schema下Parking24416、Static22535、Highway23842均实际激活 | 旧v1外层树统计不可靠，不能据此扩大实际触发覆盖 |
| 原生子场景实际完成 | 已实现，回合内锁存 | Parking24416的实际行为tick263成功，路线tick417结束 | Static/Highway专项结束时行为仍RUNNING，不冒充完成 |
| 原生路线完成与失败 | 已实现独立返回 | 多地图真实有限执行，路线终点与失败分别记录 | `execution=completed`是回合结束；`native_success`仅指RouteCompletionTest成功 |
| 官方RC、IP、DS及交通事件 | 调用固定提交StatisticsManager | 跨Conda及原生矩阵均有真实记录 | 例如Parking24416路线完成但发生碰撞；不能把完成解释为无违规 |
| 效率检查点与有界物理遥测 | 已实现 | 真实服务已传回 | 舒适度最终分数未计算；诊断原始角速度为degrees/s |
| 固定清单和配置重建 | 已实现 | 多地图、多回合初始化和显式失败记录 | 重建不等于完整物理状态恢复 |
| ParkingCutIn接管 | 阶段受限实验钩子 | p4：24416已放置adversary，waiting_for_trigger，12决策采样中L→G，有真实位移和替代控制 | 只验证该角色及短窗口；需显式allow_unvalidated_takeover |
| StaticCutIn接管 | 阶段受限实验钩子 | p4：22535已放置adversary，同样完成短窗口L→G验证 | 地下等待放置或运动阶段不可接管；后续原生寿命仍受限 |
| HighwayCutIn接管 | 阶段受限实验钩子 | 23842实验尝试遇到CARLA原生Signal11，结果未知 | 不称通过；该路线的原生运行成功执行不能替代接管验收 |
| 原生公共前缀重复性 | 明确不支持 | 未通过 | 原生TM及行为树内部状态不能完整导出，不将seed当作快照 |

三类接管先验证固定路径和当前阶段，再关闭原TM/constant velocity并获得独占控制权。原生actor销毁、场景触发和结束条件继续执行，不延长actor寿命；普通场景默认保留native机制。`capabilities()`按当前具体角色给出阶段、限制和保守验证状态，有限专项不会自动推广为该类全部路线支持。

本轮[平台加固报告](testing/platform-bench2drive-report.md)与[220条派生覆盖总表](testing/platform-bench2drive-coverage220.json)取代旧“两条路线”的覆盖概述，历史报告仍保留。默认渲染Town03加载曾以纯CARLA客户端复现Signal11；显式无渲染配置下Town03已真实执行。Town13在当时配置下反复初始化出现原生崩溃，因此该历史表的41条保留blocked；独立3816短窗执行证据不等于可重复覆盖整个地图。逐条实际p3/p4依赖版本保留，不把较早p3结果归为p4全量通过。

**2026-09-28 后续更新：** [地图专项调查](testing/town13-investigation/README.md)中，Epic、完整图层、650/650米配置的12张地图各两轮、每轮100张逐帧RGB已通过，共24次加载和2,400帧，包含本次进程生命周期。旧Town03/Town13崩溃和blocked数量不能作为当前地图一概不可用的结论；这项地图/摄像头证据也不等同于原生场景路线通过。后续Epic启动、无渲染的Town13原生批次仍失败：前38条返回（16条原生结束、22条达预算），第39条31736初始化SIGSEGV、结果未知，后两条未运行。不能宣称全部地图/场景稳定，以下旧220条数据保持原样。

同日追加：修复客户端地图就绪竞态后，Town12/13 的四条原生短序列及两项 Scenic 运行回归返回，但原生服务停止仍超时；Epic Vulkan 候选配置的 [41 条逐路线重载批次](testing/town13-investigation/native-town13-reload-barrier-v2/outcome.json) 在第 11 条 `3816` 初始化 SIGSEGV，前 10 条返回、30 条未运行。该候选仍未通过稳定性验收，旧清单结果不追改。

两批原始清单汇总为119条原生结束、53条预算截止、6条明确失败、1条未知和41条Town13阻塞；Town11旧失败已有通过复测，原始结果未覆写。40/42种有真实构建和有限执行证据；可靠新schema覆盖27种实际激活、14种实际完成，不能把旧外层树状态推导为更多覆盖。

最终[高海拔放置守卫诊断](testing/platform-highway-placement-guard.json)通过：Highway23842隐藏actor在z约261.8m时拒绝接管，56ticks后原生放置到z约361.8m才开放同步到达阶段。它没有执行接管；既有Highway接管未知结果继续保留。判断依据已识别原生放置节点的SUCCESS，未知结构拒绝，不使用海拔经验阈值。

子场景外层树在等待触发和WaitForever收尾时都可能是RUNNING。仅进度schema1的[三类cut-in专项](testing/platform-cut-in-progress-v2.json)及新版矩阵记录可作为真实行为触发/完成证据；旧v1的activated/completed字段明确为不可靠。接管的精确角色、阶段、观测跨度和声明配置见[短窗口范围记录](testing/platform-takeover-scope.json)。

## Scenic与通用自有控制器

Scenic五类事件均有真实生成、触发及至少一次完成路径证据；超时、碰撞、未触发和显式跳过仍保留，见[事件覆盖报告](testing/hardening/scenic-notes.md)。统一控制提交与帧捕获后的两NPC跟车公共前缀，在本机固定seed7的5轮重建及无干预续行中均通过原容差，见[重复性报告](testing/hardening/prefix-repeatability.json)。这是有限配置证据；capability的verified保持false，不推广为所有配置或Bench2Drive原生机制的保证。

研究几何配置支持 `reactive_npcs[].route_kind: follow | merge | crossing`，仅决定固定公共路径。follow保留原同向跟车；merge要求显式left/right相邻同向Driving lane，以smoothstep在配置距离内驶入ego所在车道；crossing从ego前方junction的Driving lane入口/出口经GlobalRoutePlanner构造路径，再验证实际横向交点、道路高度与指定接近方向。任何族找不到合适地图几何均明确失败，不改成其他族。

`configs/interaction_merge.yaml` 和 `configs/interaction_crossing.yaml` 各配置两个独立可切机制NPC、没有背景交通。Town10HD_Opt只读地图检查中，merge fixture使用ego出生点6、目标点30，两个右侧汇入路径均成功生成；crossing fixture使用出生点0、目标点30，左右来车分别具有真实横向交点。这些fixture通过纯几何假地图测试、真实地图只读检查和有限动态执行测试（见下方追加记录），公共前缀重演尚未验收。follow fixture已有上述5轮通过证据；merge/crossing的公共前缀重复性仍未验收，不能跨fixture推广。

## 完整清单验收入口

先在服务端显式配置待评估清单，再在**仅安装客户端的环境**中运行：

```bash
python scripts/validate_routes.py \
  --instance carla_01 \
  --manifest configs/manifests/v004_eval220.json \
  --output route-results.json
```

使用 `--route 28147 --route 25134` 或 `--limit 2` 进行小批验证。默认每路线最多1500个决策步，`--steps`可调整；每条路线独立reset，场景选择必须仍属于服务的同一清单。

脚本只依赖客户端、NumPy和Python标准库。参考控制器只跟随路线，没有避障、信号规则或学习算法，所得分数不能用来宣称驾驶模型性能。逐条原子保存执行状态、真实激活情况、原生criteria/scores、地图变体、种子及分开计时。步数耗尽记录为step_budget_reached，不伪装正常环境终止。

收到服务失联或未知执行结果时立即停止，剩余条目标为not_run；不会重放step或隐式重新连接。默认已知远端错误也停止，可显式`--continue-on-error`允许下一条路线重新reset；此选项仍不允许未知结果重放。

2026-09-27追加真实动态验证：两个新配置已分别运行，汇入32决策步后碰撞、近处NPC发生主动礼让和间隙等待，交叉通行执行80步但没有触发礼让/间隙机制。详见[最新验收报告](testing/hardening/README.md)；此前只读地图检查不再是唯一执行证据，公共前缀重复性仍未验收。
