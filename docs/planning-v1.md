# planning_v1 观测契约与 Flow 输入映射

`observations.profile: planning_v1` 是可选的通用真值观测扩展。环境不包含模型、归一化、推理或强化学习算法。`legacy` 默认配置和已有字段保持 actor 原点语义。IPC 仍为协议 v1；规划观测 schema 为 1，静态车道地图 schema 为 2，路线 schema 为 1。服务 `capabilities()` 在 reset 前发布数值字段规格、容量、轨迹规格和三个时钟。

## 坐标、时间与来源

CARLA 左手世界 `(x,y,z)` 转为右手世界 `(x,-y,z)`。航向为 `-radians(CARLA yaw)`。旧 `ego.position_world` 是 actor 原点。旧 objects 的 position 也仍为 actor 原点，不要作为包围盒中心使用。

新增 `planning.reference_position_world` 为当前自车后轴中心，`reference_yaw_world` 为当前自车 actor 航向。所有 `planning` 的位置、速度、加速度均投影至这个**当前**参考系，x 前、y 左、z 上。历史朝向也相对当前自车航向。绝对速度仅旋转，不减去自车速度。向量变换 `R(-yaw) v_world`；点变换 `R(-yaw) (p_world - reference_position_world)`。

- 位置、尺寸、偏移、轴距、地图弧长：m。
- 速度：m/s；加速度：m/s²；角度：rad；yaw rate：rad/s。
- `size` 顺序始终是 **长、宽、高**；Flow 需要宽、长，适配时交换索引。
- CARLA `control[0]` 为归一化 steer，正值向右。`planning.steering_angle` 为两个前轮实际转角的算术平均，弧度，正值向左；对应 `steering_valid`。它与模型转向代理量不同。
- wheelbase 和 rear_axle_offset 来自 CARLA 实际四轮物理控制。0.9.15 的 WheelPhysicsControl.position RPC 源码传输 UE 局部世界厘米；先除以 100、加上大地图原点偏移，再通过车辆逆变换获得局部米。只接受合理范围的四轮测量；不支持的轮数或不可用测量返回零和 `rear_axle_valid=false`，不猜测通用轴距。
- bbox_offset 来自 CARLA BoundingBox.location，经完整车辆姿态旋转得到世界中心。包围盒 yaw 由 actor yaw 与局部 bbox yaw 合成，是平面朝向表达。斜坡/翻滚情况下这个 yaw 和点速度不能替代完整三维刚体姿态。
- 点速度、点加速度使用 CARLA ActorSnapshot 运动量及**平面刚体偏移修正**：`v_p = v + ω × r`，`a_p = a + α × r + ω × (ω × r)`；车辆的 r 是目标点相对物理质心的偏移，质心由 `PhysicsControl.center_of_mass` 的米制局部偏移提供。α 由相邻实际物理帧 yaw rate 差分。质心未知时 `velocity_valid=false`；第一帧或修正不可用时 `acceleration_valid=false`。没有提供俯仰/滚转角速度修正；需要完整三维运动的调用方应使用另一个观测契约。
- `get_static()` 返回 boundary/source、moving_geometry_source、point_kinematics_source 等来源。lane 边界是 waypoint 的半车道宽度估计，`boundary_is_road_mesh=false`，不能称为真实道路网格。

大地图会移动 UE 原点。[官方传感器头](https://github.com/carla-simulator/carla/blob/0.9.15/Unreal/CarlaUE4/Plugins/Carla/Source/Carla/Sensor/AsyncDataStreamImpl.h)使用 UE 局部变换，ActorSnapshot 使用全局变换；服务利用已有身份挂载 IMU 心跳的同帧位姿差测量厘米整数平移，并验证方向和帧号，不猜测地图瓦片原点。相机/LiDAR 外参也加上这个平移。[官方环境对象 RPC](https://github.com/carla-simulator/carla/blob/0.9.15/Unreal/CarlaUE4/Plugins/Carla/Source/Carla/Server/CarlaServer.cpp)只转换对象 transform，不转换其 bounding_box；地图物体 bbox 因此单独转换，在大地图中与全局 `get_level_bbs` 校验，匹配失败时返回 `geometry_valid=false`。Town12 的真实回合已覆盖这一分支。

Bench2Drive 的 planning_v1 需要新的 p6 隔离副本，记录直接及批量关闭物理的原生调用。[官方车辆实现](https://github.com/carla-simulator/carla/blob/0.9.15/Unreal/CarlaUE4/Plugins/Carla/Source/Carla/Vehicle/CarlaWheeledVehicle.cpp)关闭物理时销毁 PhysX 车辆，GetVehiclePhysicsControl 却继续访问其 wheel data；真实 route 33 的隐藏救护车曾触发段错误，失败证据保留。关闭物理、休眠或未登记来源的车辆不执行这个 RPC；未测得质心时保留 bbox 位置及真实存在掩码，速度/加速度有效性为 false。恢复物理且 active 后再采集一次几何。已测得的实际局部几何可继续缓存，历史中的旧无效掩码不会被追溯改写。

历史 deque 每 `history_dt` 写入一帧，容量为 H；数值主为 float32，时间戳/世界参考点为 float64，掩码为 bool，ID 为 int64。右对齐、由旧到新；没有数据的位置为零且 valid=false。reset 只返回正常初始化帧，不隐式开车。调用方使用普通 step/step_trajectory 累积历史；这些动作的奖励、事件和终止仍然有效。`history_ready` 表示 H 帧已满、覆盖 `(H-1)*history_dt`，且最后历史帧恰好是当前物理帧。提前终止在非采样 tick 时可返回 false；当前状态仍独立存在。21 帧 / 0.1 s 时需要正常采集两秒。

## 原始规划字段

| 字段 | 形状 | 语义 / 掩码 |
|---|---|---|
| `planning.ego_history.{position,velocity,acceleration,size}` | `[H,3]` | 真实自车后轴位置/运动；size 为实际车辆尺寸 |
| `planning.ego_history.{yaw,yaw_rate}` | `[H]` | 当前参考系航向、绝对 yaw rate |
| `planning.ego_history.{valid,geometry_valid,velocity_valid,acceleration_valid}` | `[H]` | 观测存在、后轴几何可用、速度修正可用、加速度修正可用 |
| `planning.current.*` | `[3]` / 标量 | 同样字段的当前物理帧版本，独立于历史采样时钟 |
| `planning.objects.{position,velocity,acceleration,size}` | `[N,H,3]` | 当前存在邻居的 bbox 中心、运动及尺寸 |
| `planning.objects.{yaw,yaw_rate,valid,geometry_valid,velocity_valid,acceleration_valid}` | `[N,H]` | bbox 朝向及真实缺失/修正有效掩码 |
| `planning.objects.{ids,type}` | `[N]` | 稳定回合 ID；type 0 未知、1 车辆、2 行人、3 自行车、4 摩托车 |
| `planning.objects.{count,truncated}` | 标量 | 保留数量及容量截断数量 |
| `planning.static_objects.{center,size}` | `[K,3]` | 静态 actor/可识别地图物体的 bbox 中心和尺寸 |
| `planning.static_objects.{yaw,type,ids,valid,geometry_valid,source}` | `[K]` | type 1 施工标志、2 障碍栏、3 交通锥、4 其他；source 1 actor bbox、2 地图 environment object bbox |
| `planning.static_objects.{count,truncated}` | 标量 | 保留及容量截断数量 |
| `planning.geometry.*` | 标量 / `[3]` | bbox/rear/center_of_mass 偏移、轴距、最大转角、尺寸和各自有效性 |
| `planning.timestamps` | `[H]` | 实际仿真秒，不是墙钟 |
| `planning.{history_ready,history_coverage_seconds}` | 标量 | 真实覆盖情况 |
| `planning.route_segment_index` | 标量 | 当前路线描述 `ordered_segments` 中的索引 |
| `signals.{segment_index,association_valid}` | `[S]` | 受控地图 lane segment 索引；不可靠时 -1 / false |

邻居与静态物体按当前中心距后轴的三维距离、ID 排序，范围由 observations.range 控制。邻居只选当前存在对象；历史缺失不补齐，已消失对象不占当前容量。未知两轮车保持 type=0，不自动归为自行车。地图 Other/TrafficSigns 类别及 `static.*` actor 可识别物体纳入；建筑/植被不作为障碍物全集回传。不可识别的道路杂物无法承诺完整覆盖。地图对象 ID 为负的内容派生 ID；静态 actor ID 为 CARLA ID，在回合内稳定。ID 跨服务 epoch 的一致性不作承诺。

## 地图与回合路线

首次 reset 返回 `map_id`、`route_id`。客户端分别调用 `get_static(map_id)`、`get_route(route_id)`，通过现有静态共享槽获取并本地缓存；step 不重传静态几何。地图 ID 包括 OpenDRIVE、分段构建源码版本、采样配置和环境静态物体内容；源码或物体内容改变会产生新 ID。

地图扁平 `center/left_boundary/right_boundary/direction[P,3]` + `offsets[L+1]` 表示 L 个沿行驶方向排列的连续车道段。`lane_ids[L,3]` 是 road/section/lane，允许同一 key 在不同连续段重复。沿 s 有超过 6 m 间隙或 junction 属性变化时另建段；稀疏采样不是几何网格无缝拼接证明。`connections[E,2]` 表示有向前继→后继，反向查询即前驱；`adjacency[L,2]` 为中点左/右邻段索引，-1 未知。`is_junction[L]` 标识连接段。点级 speed_limit / speed_limit_valid 来自 OpenDRIVE road default；缺失为 0 / false，不推断默认限速。动态 CARLA 限速仍在 ego.speed_limit 中。

路线 `ordered_segments[R]` 是原始导航路线投影得到的有序车道段索引，重复访问同段保留；不可投影为 -1 / segment_valid=false。`point_lane_index` 和 `point_segment_index` 与 `points_world` 一一对应。`corridor[L]` 是这些路线车道段的归属集合，不扩展为任意平行车道，也不将路线前方点分块伪装成地图车道。这是 **CARLA directed lane segment mapping**，与 nuPlan roadblock/corridor 不等价。

交通灯关联来自 CARLA stop waypoint 的 road/section/lane，并要求 stop waypoint 到候选地图段中心线距离 ≤4 m。同一信号可有多条 stop waypoint 记录。关联未知时外部使用 unknown。`signals.state` 保持 CARLA 枚举：0 red、1 yellow、2 green、3 off、4 unknown；5 是项目的 stop sign，不应当作交通灯。无关联不把 ego 所遇红灯广播到所有车道。交通灯更新每物理 tick；静态 stop-waypoint 几何可缓存。

## 通用字段 → Flow 默认原始输入

对照 [Flow-Planner 366df76 agent_process.py](https://github.com/DiffusionAD/Flow-Planner/blob/366df7654d7b7fa77f9d2577279da63127c6d2f8/flow_planner/data/data_process/agent_process.py)、[map_process.py](https://github.com/DiffusionAD/Flow-Planner/blob/366df7654d7b7fa77f9d2577279da63127c6d2f8/flow_planner/data/data_process/map_process.py)、[data_processor.py](https://github.com/DiffusionAD/Flow-Planner/blob/366df7654d7b7fa77f9d2577279da63127c6d2f8/flow_planner/data/data_process/data_processor.py) 和 [默认配置](https://github.com/DiffusionAD/Flow-Planner/blob/366df7654d7b7fa77f9d2577279da63127c6d2f8/flow_planner/script/model/flow_planner.yaml)。下述索引从 0 开始，均为未加 batch、未归一化数据。这里描述外部适配责任，不提供模型专用运行入口。

| Flow 输入 | 默认形状 | 完整索引映射 |
|---|---|---|
| ego_agent_past | `[21,14]` | 0:2 ego_history.position.xy；2 cos(yaw)、3 sin(yaw)；4:6 velocity.xy；6:8 acceleration.xy；8 size.y 宽、9 size.x 长；10:14 ego 类型 `[1,0,0,0]` |
| ego_current_state | `[16]` | 0:8 对应 current 的位置、cos/sin、速度、加速度；8 模型转向代理；9 模型 yaw rate；10 宽、11 长；12:16 ego 类型 `[1,0,0,0]` |
| neighbor_agents_past | `[32,21,11]` | 0:2 objects.position.xy；2 cos(yaw)、3 sin(yaw)；4:6 velocity.xy；6 宽、7 长；8:11 one-hot 车辆、行人、自行车 |
| static_objects | `[5,10]` | 0:2 static.center.xy；2 cos(yaw)、3 sin(yaw)；4 宽、5 长；6:10 one-hot 施工标志、障碍栏、交通锥、其他 |
| lanes | `[70,20,12]` | 0:2 中心点 xy；2:4 下一点-当前点（末点零）；4:6 左边界-中心；6:8 右边界-中心；8:12 green,yellow,red,unknown |
| route_lanes | `[25,20,12]` | 从地图车道集合按 route.corridor 与导航顺序选择，与 lanes 相同特征；不可用 route.points 分块代替 |
| lanes_speed_limit / lanes_has_speed_limit | 各 `[70,1]` | 对应被选地图段的 road default m/s 与 bool 有效性；同段多个限速记录不能无声压平，适配方需按限速边界分段或明确选择规则 |
| route_lanes_speed_limit / route_lanes_has_speed_limit | 各 `[25,1]` | 对应路线车道集合限速与有效性 |

地图点为世界右手坐标，先按各自弧长独立重采样中心线和左右边界至目标容量，然后转换到当前后轴参考系并求偏移。delta 为点间**位移米**，不是单位切向量。边界始终相对行驶方向，不能按道路 s 正方向交换左/右。多个关联灯状态冲突时应保守返回 unknown；没有可靠关联或 Off/Unknown/Stop 类型也返回 unknown。

外部适配默认选择 100 m 内邻居和车道；Flow 默认只保留 32 个邻居，过量时对行人/自行车合计有 10 个配额，按距离选取。环境默认容量 N=64 并独立提供截断计数；需要更多时配置容量。静态物体选最近 5 个，空槽为零。摩托车/未知类型不属于三类 one-hot，适配方必须明确排除或另行训练扩展类型，不能当作自行车。

适配方需核对 timestamps 形成 21 个真实 0.1 s 历史样本、检查 reference_valid 与相关几何掩码；不足两秒时继续正常驾驶采集，提前结束则不能接管。Flow 上游只处理当前出现对象，并反向用最近的未来可见状态填过去缺失；这项补齐若要复现，应在外部逐 ID 执行，**不得改写环境真实 valid 掩码**。对象全无时输出全零；所有角度在构造 cos/sin 前是相对当前参考系的 rad。

Flow 官方预处理硬编码 Pacifica 宽 2.297 m、长 5.176 m、轴距 3.089 m。这里返回实际 CARLA 车辆尺寸及轴距；由外部工程决定是否使用模型名义参数。其 current 第 8 项不是实际轮转角，而是基于 `atan(yaw_rate*3.089/abs(vx))` 的代理，并带低速阈值和裁剪；第 9 项也有上游裁剪。外部复现应逐行依照对应版本 data_processor 的公式处理，不能直接以 normalized steer 替换。环境保持实际 wheel angle / raw yaw rate，避免替模型决定这些处理。

环境不执行 Flow 归一化，不提供未来标签，也不输出默认模型未消费的交叉口标签来冒充必要输入。使用这个字段映射只能证明可构造基本向量输入；CARLA 与 nuPlan 地图、车辆参数和状态来源有分布差异，不能据此宣称模型驾驶效果通过验证。
