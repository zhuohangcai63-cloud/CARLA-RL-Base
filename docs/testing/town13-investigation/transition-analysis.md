# Town13 同世界复用与逐回合重载：有界审查

日期：2026-09-28。本文件阅读现有代码、锁定的 Bench2Drive 副本、路线 XML 和崩溃记录；没有启动 CARLA，也没有修改运行中的结果文件。这里给出诊断条件与策略边界，不宣称已确定原生崩溃根因。

## 已确认的配置差异

`EnvironmentRuntime` 的 `simulation.reload_on_reset` 默认是 true；`WorldSession.load()` 在该值为 true 时，即使地图名称没有改变，也调用 `load_world(..., reset_settings=False)`。false 时，仅首次加载、地图改变或地图层请求改变才会实际加载世界；同地图后续 reset 复用现有世界。

此次调查前，`validate_native_routes.py` 的 `--reload` 是默认 false 的 `store_true`，直接覆盖运行内核的默认值；`validate_native_matrix.py` 没有显式转发此选项。因此原来的 Town13 41 路线批次使用同世界复用，与生产默认策略不同。已将两个 CLI 改为默认 true，保留 `--reload` 并新增显式 `--no-reload`，矩阵记录和转发该选择。两个评估 YAML 也显式声明 true。

这与锁定的 [Bench2Drive evaluator](https://github.com/Thinklab-SJTU/Bench2Drive/blob/acbeb0dd8aa99dd0e71b9b75bb542f6fe7c7f3d8/leaderboard/leaderboard/leaderboard_evaluator.py) 一致：每条路线的 `_load_and_wait_for_world()` 都调用 `load_world(town, reset_settings=False)`，之后重新应用 650 m 流送/激活距离。修正 CLI 默认值是验收配置一致性修复，尚不是 C++ 崩溃修复。

## 值得隔离的路线转换

位置来自锁定 XML `leaderboard/data/bench2drive_0.0.4_val.xml`，单位为 CARLA 世界坐标米；末端是路线 XML 终点，不是驱动停止位置的实测值。

| 顺序 | 路线/场景 | XML 起点 | XML 终点 |
| --- | --- | --- | --- |
| 37 | 30008 / ConstructionObstacleTwoWays | (2687.6, 2058.1, 174.5) | (2821.6, 2059.7, 176.1) |
| 38 | 37391 / MergerIntoSlowTrafficV2 | (-3777.9, -2854.7, 163.1) | (-3626.7, -2851.2, 161.1) |
| 39 | 31736 / VehicleOpensDoorTwoWays | (2826.4, 2284.7, 177.8) | 见锁定 XML |

第一条终点到第二条起点约 8.23 km，第二条终点到第三条起点约 8.25 km；第三条起点距离第一条终点约 225 m。它形成“区域 A → 远处 B → 返回 A 附近”的加载条件，因此适合先测试 `37391 → 31736`，再测试 `30008 → 37391 → 31736`。这只是优先诊断子集；不能据地理关系直接证明 tile 复用错误，也不能用新进程单独通过 31736 代替原批次条件。

已有重要反证：新进程中的三路线同世界复用子集 [native-town13-transition-reuse/probe.json](native-town13-transition-reuse/probe.json) 正常返回，分别为 1000 tick 到预算、415 tick 完成、402 tick 完成，没有复现初始化崩溃。因此 **A→B→A 三路线转换本身不是当前已证明的充分触发条件**；完整批次的更早历史或其他未隔离条件仍可能相关。该次 [summary.json](native-town13-transition-reuse/summary.json) 同时记录停止超时后 SIGKILL，`shutdown_failed=true`、`passed=false`，运行阶段正常返回不等于整个生命周期通过。

`native-town13-epic/client.txt` 已记录前 38 条返回，第 39 条仅到 `LOADED`，没有 `PREPARED`。对应崩溃为 Vulkan `FVulkanResourceMultiBuffer` / `FRawStaticIndexBuffer::InitRHI` 渲染线程路径；它不是 RGB Town03 的空顶点声明栈。`LOADED` 只说明 `WorldSession.load()` 返回，不能证明随后失败的是开门 NPC 创建；hero 创建、地图 tile 初始化和场景构造都在 `PREPARED` 之前。

## hero、地图块与资源清理

CARLA 0.9.15 的 [LargeMapManager.cpp](https://github.com/carla-simulator/carla/blob/0.9.15/Unreal/CarlaUE4/Plugins/Carla/Source/Carla/MapGen/LargeMapManager.cpp) 给出如下边界：

- hero 注册时依次请求原点重定位、更新 tile 状态，再 `FlushLevelStreaming()`，防止出生时地面未加载。
- `OnActorDestroyed()` 本身没有将 hero 从考虑列表移除。后续 tick 由 `UpdateTilesState()` 发现无效 actor 并排队，再由 `RemovePendingActorsToRemove()` 删除。
- 不存在 hero 且 `spectator_as_ego=false` 时，原 tile 会被请求卸载；卸载标志不要求阻塞。`CurrentTilesLoaded` 记录请求后的集合，不是 GPU 资源释放完成的证明。
- 同世界复用保留 LargeMapManager 和当前世界原点等状态。逐回合重载会重建这些世界级对象，但不重启 Vulkan 设备或整个 CARLA 进程。

环境适配器先终止场景树，清空可能被延迟析构使用的 actor 列表，再调用 provider 清理和 owned registry 补充清理。成功的 batch destruction 会注销归属；没有通配符清场。下一回合 WorldSession 的两次 `apply_settings()` 仍可能在 CARLA 内部推进初始化 tick，因此不能简单归因于“完全没有清理 tick”。这些边界支持进行世界复用/重载对照，但不能证明 UE 资源生命周期中的具体缺陷。

## VehicleOpensDoorTwoWays 的实际构造顺序

锁定 [RouteScenario](https://github.com/Thinklab-SJTU/Bench2Drive/blob/acbeb0dd8aa99dd0e71b9b75bb542f6fe7c7f3d8/leaderboard/leaderboard/scenarios/route_scenario.py) 先插值路线，再创建 `vehicle.lincoln.mkz_2020`、role `hero`，通过 provider 与 world bridge 执行初始化 tick，随后定位 spectator、再 tick，构建路线树后才创建附近场景。初始化期间这些 tick 由唯一 WorldSession 管理，不属于决策执行窗口。

[VehicleOpensDoorTwoWays](https://github.com/Thinklab-SJTU/Bench2Drive/blob/acbeb0dd8aa99dd0e71b9b75bb542f6fe7c7f3d8/scenario_runner/srunner/scenarios/vehicle_opens_door.py) 根据 trigger 前进配置距离，选择侧车道；31736 的距离是 53 m。随后通过 provider 从 `*vehicle.*` 中按 `has_dynamic_doors=true`、`base_type=car` 过滤后采样蓝图，创建 role `scenario` 的车辆，略抬高出生位置，tick 后按实际 bounding box 横向调整位置并拉手刹。没有证据表明这条路线的 XML 或某个指定蓝图必然损坏；失败前缺少细粒度 actor 创建日志，不能假定已执行到开门车辆。

## 对照与使用策略

先保持 Epic、seed 123、650 m 距离、无相机、相同 tick 预算和路线顺序，仅比较 `--no-reload` / `--reload`。若小子集不复现，扩大前缀，不能说复用假设已排除；若重载小子集通过，仍需完整 41 条同预算验收。对照结果应分别保存在独立目录，原故障证据保留。

逐回合重载是明确的 reset 策略，不修改地图或场景内容、不缩短路线、不重放失败动作，也没有 Town13 专用分支。代价是更多初始化时间；它是否足以稳定原条件，必须由真实测试回答。不能静默改用定期进程重启、扩大 actor 存活期、增加额外决策 tick，或将同世界复用的失败记录改写为通过。
