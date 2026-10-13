# 真实公共前缀重复性诊断

日期：2026-09-27。CARLA 0.9.15，独立 RPC 2200 / Traffic Manager 8200，Town10HD，`interaction_follow.scenic`，seed 7，两个自有反应式 NPC。每个公共前缀执行 5 个决策、20 个物理 tick，动作固定为 `[0, 0.4, 0]`。这是真实 CARLA 数据，不是 fake 后端结果。

**结论：重建和前缀执行已经实际运行，但当前配置没有建立可靠的起点重复性保证。** 某次重演可以完全通过，同一配置重复执行也会超过控制器/历史容差。场景 capability 继续保持 `verified: false`，没有将失败回合标为可比较起点，也没有放宽已有阈值来使测试通过。

## 保留的失败与检查

最初失败记录保存在 [carla-prefix-failure-original.json](carla-prefix-failure-original.json)：前四个决策匹配，第五个决策位置误差约 0.029 m、速度误差约 0.814 m/s，历史与奖励任务内部数值也出现偏差。

随后一轮逐物理 tick 诊断同时读取 `actor.get_transform/get_velocity/get_acceleration` 与该 tick 的 `WorldSnapshot`。这次运行没有发现二者帧内状态不一致，5 步重演通过；远处 NPC 第 17 个物理 tick 起仍出现微小动力学和转向差，末速度误差约 `4.6e-6 m/s`。记录见 [carla-prefix-diagnostic-01.json](carla-prefix-diagnostic-01.json)。额外诊断读取改变墙钟执行速度，因此这一次通过不能反证正常请求路径存在时序敏感性。

所有后续比较继续使用原位置、速度、航向、控制器、历史、事件、信号及任务阈值；只显式补充原先缺少的 `actor_state=0.2`，用于已有额外对象数值字段，避免把“未配置容差”误当作重复性原因。

| 字段 | 容差 |
|---|---:|
| position | 0.2 m |
| velocity | 0.2 m/s |
| yaw | 0.03 rad |
| actor_state | 0.2 |
| controllers | 0.00001 |
| history | 0.2 |
| events、signals | 0.00001 |
| task | 0.01 |

分区容差对数值叶子逐项比较；`history` 含位置、速度和加速度，`controllers` 含 PID 与延迟历史，因此它们的最大数值差具有混合单位，不能直接解释为单一距离或速度。

## 重复运行与固定初始化对照

正常请求路径不增加逐 tick 诊断读取，连续进行三组重新构建与重演。结果见 [carla-prefix-repeatability-01.json](carla-prefix-repeatability-01.json)：

| 组 | 判定 | 末位置最大误差 m | 末速度最大误差 m/s | 控制器最大数值差 | 历史最大数值差 |
|---|---|---:|---:|---:|---:|
| 0 | passed | 0 | 0.000000153 | 0.000000000124 | 0.0000113 |
| 1 | failed | 0.001994 | 0.026661 | 0.017832 | 0.176578 |
| 2 | failed | 0.004597 | 0.041410 | 0.034161 | 0.273390 |

本轮差异集中在远处 NPC；第 1 组超过控制器阈值，第 2 组超过控制器和历史阈值。与最初失败不同，本轮末端任务状态没有偏差。

另做三组明确的初始化实验：在 Scenic 完成创建之后，为自车和两个托管 NPC 设置 `brake=1`，固定推进 **20 个 initialization tick**，不运行事件/控制器更新、不把这些 tick 计入公共前缀。它是临时诊断补丁，未修改源码默认行为。记录见 [carla-prefix-settling-01.json](carla-prefix-settling-01.json)：

| 组 | 判定 | 末位置最大误差 m | 末速度最大误差 m/s | 控制器最大数值差 | 历史最大数值差 |
|---|---|---:|---:|---:|---:|
| 0 | passed | 0 | 0 | 0 | 0 |
| 1 | failed | 0.004647 | 0.024330 | 0.032226 | 0.388334 |
| 2 | passed | 0 | 0 | 0 | 0.00000000186 |

固定等待没有消除失败。3 组中通过次数从 1 次变为 2 次只是小样本结果，不能证明稳定性改善，更不能宣称修复。未将这个初始化选择写入默认配置。车辆落地、动力系统/自动换挡隐状态或控制命令时序仍是后续待区分的原因；当前没有足够证据指定其中一种为根因。

## 两个 NPC 的独立闭环机制切换

机制功能使用独立真实测试验证，不以重演通过为前提：

```bash
EASYCARLA_REAL_PORT=2200 EASYCARLA_REAL_REPORT_DIR=docs/testing \
  python -m pytest tests/test_carla_integration.py::test_real_two_npc_mechanism_switches -q
```

本轮结果为 **1 passed，9.68 秒**。切换前已正常执行 10 个决策；近处 NPC 切为 L，远处 NPC 切为 G，确认两者的 PID、路径索引、延迟历史及控制器时间在切换时保持连续。随后执行 25 个决策、100 个物理 tick，逐决策检查控制权和 `last_tick`，并记录实际 CARLA 控制及状态。

近处 NPC 出现 47 个 `following` 物理 tick。远处 NPC 持续闭环行驶，但这个跟车几何没有适用的交叉间隙事件，`gap_wait` 触发数为 0；这不是 G 间隙机制激活成功的证据。完整结果见 [carla-mechanism-switch.json](carla-mechanism-switch.json)。本测试不组织八分支、不计算研究标签，也不将它的通过解释为公共前缀重复性通过。
