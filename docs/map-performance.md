# 按地图配置流送负载

`simulation.map_overrides` 只在地图名精确匹配时覆盖流送距离和参与者激活距离。它不替换地图，不修改路线、出生点、行为树或奖励；没有自动降级或失败回退。

本机推荐先以 `-quality-level=Epic` 启动 CARLA。2026-09-28 的对照中，Low 在 Town03 复现原生 Vulkan 崩溃，Epic 完成了全部 12 张 Bench2Drive 地图的两轮逐帧 RGB 检查。Low/Epic 是包含多个渲染设置和材质处理的预设，不能把结果归因于某一个线程参数；实测与限制见[地图排查报告](testing/town13-investigation/README.md)。

Epic 单独使用仍复现过 Town13 原生长批次崩溃。[隔离的 Vulkan 诊断配置](carla-vulkan.md)也未通过长批次验收，仅用于后续复现；缩小地图流送距离和渲染兼容设置解决的是不同问题。

[bench2drive_eval.yaml](../configs/bench2drive_eval.yaml) 显式使用全局 650/650 米、全部图层、启用渲染、关闭 spectator 流送驱动，匹配上述全图诊断的世界设置。650 米也见于锁定的[官方 leaderboard evaluator](https://github.com/carla-simulator/leaderboard/blob/b00c8bd7fe9ea7a2783cbbdbc53e419c65d6a0ad/leaderboard/leaderboard_evaluator.py#L166-L213)。原来的未指定距离会继承服务端状态；现在不再依赖继承。地图加载/RGB 检查通过不等于全部路线、全部感知配置或正式基准评价通过。

如果其他课题需要保留基础加载范围、仅针对指定地图调整，可以使用下面的覆盖接口：

```yaml
simulation:
  tile_stream_distance: 3000.0
  actor_active_distance: 2000.0
  map_overrides:
    Town13:
      tile_stream_distance: 650.0
      actor_active_distance: 650.0
```

默认值为 `{}`。键必须是精确地图 basename，例如 `Town13`；不接受路径、通配符或前缀匹配。服务加载完整资产路径时仍只用末级地图名查找。每项至少指定一个距离，两者都必须是正的有限数值；不接受 `None`、布尔值、其他参数。覆盖与基础值合并后，`actor_active_distance` 不能大于 `tile_stream_distance`。部分字段未指定时，加载后还会针对 CARLA 实际值检查这一关系。

每次加载独立地从基础配置合并，不改写基础字典。上例从 Town13 切换到其他地图时明确恢复 3000/2000 米；再回到 Town13 时重新应用 650/650 米。加载前尽可能应用目标值，加载后重新读取新世界的设置，再应用本次配置并验证实际返回值。部分覆盖如果只与上一张地图继承值冲突，预加载阶段保留上一张地图的距离组合，加载后再按新世界实际值校验。

基础距离为 `None` 的含义是使用新世界读回的实际值，**不保证恢复该地图的出厂默认值**。`load_world(reset_settings=False)` 本身可能在 CARLA 服务端保留前一个世界的设置。需要明确跨地图恢复时，应像上例一样指定基础值。3000/2000 米已通过本机 CARLA 0.9.15 的 `carla.WorldSettings()` 构造结果核对；该核对没有启动仿真器。

[configs/bench2drive_low_load.yaml](../configs/bench2drive_low_load.yaml) 只为 Town12/Town13 设置 650/650 米，其他地图使用明确的基础值。它保留渲染和全部地图图层，关闭 spectator 对大地图的额外流送驱动；没有全局降低画质。90 秒 RPC 超时为地图初始化提供时间，不能修复 SIGSEGV、渲染线程死锁或硬件故障。此配置是可测试的负载选项，不等于所有路线已经通过稳定性验收。

```bash
easycarla serve --instance carla_01 --config configs/bench2drive_low_load.yaml
```

流送和激活距离会影响远处内容加载与参与者休眠/唤醒，因此即使保留画面和图层，也不能未经验证就称为官方默认 Bench2Drive 设置等价。配置需覆盖研究中所需的感知、交互距离和场景参与者生命周期；具体路线仍需要单独验收。

返回的 `info.applied_map_override` 记录本张地图命中的覆盖项，`info.simulation_config.map_overrides` 记录完整配置，`info.effective_world_settings` 记录地图名和读回值。初始 manifest 和 recipe 固定这些字段；配置改变在重建前拒绝，实际值或实际覆盖不同在重建后、重演驾驶命令前拒绝。缺少新增字段的旧前缀需重新记录。

无 CARLA 回归测试覆盖 A→B→A 切换、基础配置不变、新世界默认值读回、精确匹配、部分覆盖校验、拒绝异常返回值及 recipe 不符拒绝；这不替代真实地图运行测试。
