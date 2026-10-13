# 动作、观测、奖励与回合结果（0.2）

本页描述当前服务实际返回的字段。`obs` 是结构化字典；数组经共享内存协议传输，默认返回独立副本。Python `int/float/bool/str` 标为“标量”，数组列出 NumPy dtype。没有归一化、模型专用 embedding 或真实 NPC 机制标签。

结构化状态来自 CARLA 真值，`info.observation_profile="privileged_structured_truth"`。它不是由 RGB/LiDAR 感知模型估计的目标，也不等同于 Bench2Drive 传感器赛道参赛输入。

## 时间、坐标与动作

默认物理间隔 `physics_dt=0.05 s`，动作窗口 `decision_dt=0.2 s`，对象历史间隔 `history_dt=0.1 s`。后两者必须是物理间隔的整数倍。动作通常推进 4 个物理 tick，遇到终止立即停止；`info.executed_seconds` 和 `info.physical_ticks` 给出实际执行量。计算动作、查询地图和读取能力不推进仿真。

世界坐标 `world_rh` 由 CARLA 坐标变换为 `[x, -y, z]`，航向为 `-CARLA_yaw × π/180`。长度用米，速度 m/s，加速度 m/s²，角度 rad，角速度 rad/s，时间 s。

结构化几何字段 `ego_flu` 的原点为**当前自车 actor transform 原点**，不是后轴中心，也不保证是包围盒几何中心。当前实现按自车 yaw 进行水平旋转，x 前、y 左、z 世界向上；不补偿车体 roll/pitch。因此它是水平化的自车航向系，不是完整三维车体系。对象位置同样是各 actor 的 transform 原点，`size` 不包含包围盒中心相对该原点的偏移。

`step(action)` 接受三个有限数值，转换为 `float32[3]`：

| 索引 | 含义 | 范围与方向 |
|---|---|---|
| 0 | steer | `[-1,1]`，**正值向右**，沿用 CARLA 控制约定 |
| 1 | throttle | `[0,1]` |
| 2 | brake | `[0,1]` |

越界、NaN/Inf、形状错误、同时正油门和正刹车在推进前拒绝，不静默裁剪。Bench2Drive 原生扰动可以对合法请求作显式控制变换；原生结果可能同时包含油门和刹车，`info.actual_controls` 同时记录请求值和实际值。`ego.control` 是最后物理帧的实际控制，不应代替整个窗口的控制历史。

`step_trajectory(trajectory, execute_seconds=None)` 接收 `reference_frame`、从 0 开始严格递增的 `times[N]`，以及 `points[N,4]=[x,y,yaw,speed]`，`N>=2`。数值必须有限、速度非负，轨迹覆盖执行窗口。位置和航向固定在提交时的自车参考系，单位 m/rad/m/s；参考 frame 必须等于最近返回观测帧。默认执行一个决策窗口，指定时长必须为正整数个物理 tick。环境只跟踪外部轨迹。

## 动态观测字段

默认容量：`H=21` 历史帧、`N=64` 个对象、`R=128` 路线点、`S=32` 条信号车道记录、`O=128` 个静态障碍；分别由 `history_frames/max_objects/route_points/max_signals/max_obstacles` 配置。半径默认 `observations.range=120 m`。容量截断显式返回数量，不能把零填充解释为真实状态。

| 顶层字段 | 类型 | 含义 |
|---|---|---|
| `frame` | int | 当前 CARLA 物理帧 ID；不是回合步数，reset 后不承诺归零 |
| `timestamp` | float | 此帧的 CARLA `elapsed_seconds`，不是墙钟；世界重载后不要假定跨回合连续 |
| `coordinate_frame` | str | `ego_flu`，含义见上文 |
| `map_id` | str | 地图 schema 前缀与 OpenDRIVE 内容的 SHA-256；用于静态地图缓存 |
| `ego/objects/route/signals/obstacles/sensors` | dict | 下列表格定义 |

### 自车 `ego`

| 字段 | dtype / shape | 说明 |
|---|---|---|
| `position_world` | float64 `[3]` | 当前 actor 原点，world_rh，m |
| `yaw_world` | float 标量 | world_rh 航向，rad |
| `velocity` | float32 `[3]` | 当前 ego_flu 速度，m/s；绝对速度经坐标旋转，不是相对速度 |
| `acceleration` | float32 `[3]` | 当前 ego_flu 加速度，m/s² |
| `yaw_rate` | float 标量 | CARLA 世界 z 角速度取负并从 degree/s 转为 rad/s |
| `speed` | float 标量 | 世界三维速度的模，m/s |
| `control` | float32 `[3]` | `[steer,throttle,brake]`，实际最后控制 |
| `size` | float32 `[3]` | 包围盒全尺寸 `[length,width,height]`，m |
| `speed_limit` | float 标量 | 自车当前查询限速，从 km/h 转为 m/s |

### 对象历史 `objects`

只包括存活车辆和行人，不含自车。按当前距离与稳定 ID 排序，选择半径内最近 N 个；数组槽位可以随距离变化，跨步关联必须使用 `ids`。

| 字段 | dtype / shape | 说明 |
|---|---|---|
| `position` | float32 `[H,N,3]` | 历史 actor 位置，全部转换到**当前**自车参考系，m |
| `velocity/acceleration` | float32 `[H,N,3]` | 历史绝对速度/加速度旋转到当前参考系；不减自车速度/加速度 |
| `size` | float32 `[H,N,3]` | 各历史帧全尺寸，沿目标自身局部轴，m |
| `yaw` | float32 `[H,N]` | 历史目标 yaw 减当前自车 yaw，环绕到 `[-π,π)` |
| `yaw_rate` | float32 `[H,N]` | 历史绝对 z 角速度，rad/s；不减自车角速度 |
| `valid` | bool `[H,N]` | 该对象在该历史样本是否存在 |
| `type` | uint8 `[H,N]` | 0 填充、1 车辆、2 行人；仍需看 valid |
| `ids` | int64 `[N]` | 对象语义跟踪 ID；未占用槽为 0 |
| `timestamps` | float64 `[H]` | 各历史样本的仿真时间；前部不足历史填 0 |
| `count` | int | 当前选中对象数量 |
| `truncated` | int | 半径内超过容量而未返回的对象数 |

历史按旧到新排列，向尾部对齐，默认覆盖最多 2 秒。未达到完整历史、新出现对象、消失对象的缺测均用 valid 表示，不复制状态补齐。因为采样间隔为 0.1 s，结束在中间物理帧时最后一个对象历史样本可以早于顶层 timestamp；应读取真实时间戳。当前自车状态及传感器仍属于结束物理帧。

已登记角色的 ID 来自角色字符串摘要，未登记角色使用当前回合分配 ID。它们不是 CARLA actor.id；普通未登记对象的 ID 不保证跨 reset 相同。重新构造场景的实体对应由 recipe/能力约束和一致性验证负责。

### 路线 `route`

| 字段 | dtype / shape | 说明 |
|---|---|---|
| `points` | float32 `[R,3]` | 从当前路线索引起的前方采样点，当前 ego_flu，m |
| `valid` | bool `[R]` | 路线尾部的有效点掩码 |
| `progress` | float | 当前选中路线点的累计弧长，m |
| `length` | float | 全路线采样折线长度，m |
| `index` | int | 全路线当前采样点索引 |

索引在上次位置附近搜索，避免在交叉线路上任意跳支路。进度按离散采样点计算，可能小幅回退；不是沿连续车道精确投影。此字典没有单独的导航指令 one-hot。

### 交通信号 `signals`

一盏灯可关联多个停车车道，输出每条关联一行，按距离与 actor ID 排序。交通灯使用 CARLA stop waypoints；停车标志使用 trigger volume 投影到车道的近似停止位置。

| 字段 | dtype / shape | 说明 |
|---|---|---|
| `position` | float32 `[S,3]` | 停止位置，当前 ego_flu，m |
| `direction` | float32 `[S,3]` | 该车道行驶方向单位向量，当前参考系 |
| `stop_line` | float32 `[S,2,3]` | 按车道宽度构造的左右停止线端点，m |
| `lane_ids` | int32 `[S,3]` | `[road_id,section_id,lane_id]`，可关联静态地图 |
| `ids` | int64 `[S]` | CARLA 信号 actor ID；不同于 objects.ids，不保证跨 reset 稳定 |
| `state` | int32 `[S]` | 0 红、1 黄、2 绿、3 关闭、4 未知、5 停车标志 |
| `valid` | bool `[S]` | 有效关联记录 |
| `truncated` | int | 超过容量的关联记录数 |
| `position_kind` | str | `stop_waypoint` |

### 静态障碍 `obstacles`

| 字段 | dtype / shape | 说明 |
|---|---|---|
| `position` | float32 `[O,3]` | 当前参考系 actor 原点，m |
| `size` | float32 `[O,3]` | actor 包围盒全尺寸，m |
| `valid` | bool `[O]` | 有效障碍记录 |
| `truncated` | int | 半径内超出容量的障碍数 |

当前只收集该环境登记拥有的 `static.*` 参与者，例如施工道具；不等于地图上所有建筑、路缘和静态几何的完整列表，也没有障碍 yaw 或稳定 ID 字段。

## 可选传感器 `sensors`

未开启的键不存在；全部关闭时是 `{}`。需要的传感器缺帧会报错，不用旧图像或零数组代替。

| 键及字段 | dtype / shape | 定义 |
|---|---|---|
| `rgb.data` | uint8 `[image_height,image_width,3]` | RGB 通道顺序，默认 `[180,320,3]`；范围 0–255 |
| `bev.data` | uint8 `[bev_size,bev_size]` | CARLA 语义类别原始标签，默认 `[256,256]`，不是调色板 RGB |
| `rgb/bev.frame` | int | 对齐的物理帧 |
| `rgb/bev.timestamp` | float | 传感器仿真时间，s |
| `lidar.points` | float32 `[M,4]` | `[x,y,z,intensity]`，前三项 m，强度为原始无量纲值 |
| `lidar.count` | int | 返回点数 M，最大默认 65536 |
| `lidar.truncated` | int | 因容量未返回的点数 |
| `lidar.frame/timestamp` | int / float | 对齐物理帧及传感器时间 |
| 每个传感器的 `coordinate_frame` | str | RGB/BEV 为 `image_uv`（u 向右、v 向下）；LiDAR 为 `ego_flu` |
| 每个传感器的 `sensor_frame` | str | `sensor_flu`，外参矩阵的源坐标系 |
| 每个传感器的 `sensor_to_world` | float64 `[4,4]` | 本帧 sensor_flu → world_rh 的齐次外参 |
| 每个传感器的 `sensor_to_ego` | float64 `[4,4]` | 本帧 sensor_flu → 当前水平化 ego_flu 的齐次外参 |

RGB 固定挂载在 CARLA 自车局部 `(1.5,0,2.0) m`，没有额外旋转，FOV 使用传感器蓝图默认值；当前载荷不单独返回相机内参矩阵。BEV 是挂载在 `(0,0,40) m`、pitch=-90°、FOV=90° 的**俯视透视语义相机**，随自车运动，不是矢量 BEV、正射地图或完整占据栅格；遮挡、道路高程及相机投影都影响像素含义。

LiDAR 固定挂载 `(0,0,2.4) m`，32 通道、100000 points/s、旋转频率 `1/physics_dt`。输出使用本帧 packet.transform 的完整旋转和平移，把原始传感器左手坐标转换到 world_rh，再转换到水平化 ego_flu；包括道路坡度、自车 roll/pitch 和挂载平移的影响，与结构化几何使用同一个当前参考系。裁剪保留回调点序列前 M 个点，不做随机采样或体素化。点数通过 count/shape 指明，不额外填充或返回 point mask。

外参的输入是三维 `sensor_flu` 点 `[x,y,z,1]`，不是像素 `[u,v]`；像素投影还需要相机内参。矩阵来自各自 packet 的传感器姿态，不能复用上一帧。

内部 IMU 和碰撞传感器不出现在 `sensors`。其同步、有限碰撞等待、迟到故障及对既有转移的影响见 [传感器同步说明](sensor-synchronization.md)。

## 静态地图 `get_static(obs["map_id"])`

地图按 ID 在客户端缓存。坐标为 world_rh，全部几何单位 m。设 `P` 为采样点数，`L` 为 road/section/lane 分组数，`E` 为连接记录数：

| 字段 | dtype / shape | 说明 |
|---|---|---|
| `map_id/name/coordinate_frame` | str | 内容 ID、地图名、`world_rh` |
| `center` | float32 `[P,3]` | 约每 2 m 采样的车道中心 |
| `left_boundary/right_boundary` | float32 `[P,3]` | 按中心、局部左右向量和宽度推导的边界 |
| `offsets` | int32 `[L+1]` | 第 i 组点区间为 `offsets[i]:offsets[i+1]` |
| `lane_ids` | int32 `[L,3]` | `[road_id,section_id,lane_id]` |
| `lane_width` | float32 `[P]` | 点对应车道宽度 |
| `lane_type` | int32 `[P]` | CARLA LaneType 数值标志 |
| `lane_change` | int32 `[P]` | CARLA LaneChange：0 无、1 右、2 左、3 双侧 |
| `speed_limit` | float32 `[P]` | OpenDRIVE road 级默认限速，m/s |
| `speed_limit_valid` | bool `[P]` | 该点是否存在有限、正值、可解析的默认限速 |
| `connections` | int32 `[E,2]` | topology 与末端后继映射的去重 lane 分组索引对 |
| `sampling_distance_m` | float | 2.0 |
| `schema_version` | int | 1；同时进入 map_id 的摘要前缀 |
| `speed_limit_source` | str | `opendrive_road_default; ego.speed_limit includes CARLA signs` |

负 lane ID 按 OpenDRIVE s 增加排序，正 lane ID 按 s 减少排序。connections 结合原始 topology 段和末端 next(2 m) 后继构造并去重，可以含同组边；它是采样车道分组图。边界是几何估计，不是道路网格或实际标线提取。静态限速来自 OpenDRIVE road/type/speed，按 s 区间赋给该 road 的车道采样点，支持 m/s、km/h、mph 转换；缺省、不限速、未知单位用 valid=false 表示。它不覆盖所有 lane 级限速或动态标志，当前自车限速以 ego.speed_limit 为准。

## 奖励、cost 和终止

每个物理 tick 计算奖励，决策窗口返回求和。定义 Δt 为物理步长、s 为离散路线弧长、a 为恢复到 world_rh 的当前加速度、j=(a-a_previous)/Δt，限速 v_limit 至少按 0.1 m/s 计算：

| 分项 | 每 tick 原始量 | 默认权重 |
|---|---|---:|
| progress | `s-s_previous` | 0.1 |
| time | `Δt` | -0.1 |
| acceleration | `min(4, sum((a_xy/3)^2)) × Δt` | -0.1 |
| jerk | `min(4, sum((j_xy/5)^2)) × Δt` | -0.05 |
| overspeed | `max(0,(speed-v_limit)/v_limit)^2 × Δt` | -1 |
| collision/offroad/signal/goal | 新事件指示量 | -50 / -10 / -10 / +10 |

加速度先按各帧 yaw_world 转回同一个惯性 world_rh，再计算 jerk；单纯改变自车航向而惯性加速度不变不会产生伪 jerk。当前奖励使用世界 x/y 平面分量，不计 z 分量。事件按身份去重；当前碰撞、驶离道路和目标事件默认每回合各计一次，信号事件按检测器给出的标识去重。信号检测基于跨停止线，正常等待红灯不计违规。

`info.reward_components` 是**已乘权重**并在窗口累加的各分项；返回的 reward 是其和。`info.cost_components` 是未乘奖励权重的 collision、offroad、signal、overspeed 原始量之和，`info.cost` 为这些量求和。cost 是明确的混合代理指标，不是概率，也未承诺不同单位具有相同风险尺度。

| 任务 profile | terminated | truncated |
|---|---|---|
| `driving` | 碰撞、驶离可接受道路、到达目标 | 达到 `max_seconds`，默认 300 s，且同帧没有自然终止 |
| `bench2drive_native` | 原生场景结束且非原生 timeout | 原生 timeout |

驶离道路指自车原点不能映射到 Driving/Parking/Shoulder 车道，不是简单超出当前车道中心线阈值，也不是完整车体侵占判定。目标判断同时要求距最终路线点不超过 `goal_radius`（默认 5 m）且索引进入末尾 5 个采样点；目标奖励使用同一事件。

`driving` 任务支持 `task.termination` 的 `collision/offroad/goal/timeout` 布尔开关，
默认全部为 `true`。可视化观察时可使用以下配置，仅在碰撞时终止：

```yaml
task:
  profile: driving
  termination:
    collision: true
    offroad: false
    goal: false
    timeout: false
```

关闭终止项不关闭对应事实、奖励和 cost 记录。未实际终止时
`info.termination_reason` 为空。`info.goal_reached` 表示本回合任一物理帧曾到达终点，
在下一次 reset 前保持为 `true`，独立于终点奖励权重和终点终止开关；外部观察客户端
可以据此主动 reset 到新的路线。关闭 timeout 时 `max_seconds` 不限制环境回合，
外部客户端自己的运行预算需另行配置。`bench2drive_native` 保留原生规则，拒绝关闭
这些通用终止项。观测模式下的结果与常规评估分别记录。

Bench2Drive 原生 profile 不以通用 collision/offroad 标志直接结束回合；原生 criteria 与结果在 `info.events` 内另行报告，RL reward 仍独立存在。请结合 terminated/truncated 解读 termination_reason，不以某个通用事件名称推断原生回合已经结束。

终止返回结束帧的最终 obs，不自动 reset。非法请求产生 `RemoteError(code="invalid_request")` 且不推进；仿真、参与者、必需传感器或迟到事件故障产生错误响应，不返回正常五元组。重新开始需要显式 reset。

`info` 同时提供 scenario 来源/标识/种子、episode_id、episode_seconds、decision_steps、事件统计、tick_counts、实际控制序列、start_frame/final_frame 和 timing。时间指标分别记录 simulation_seconds、sensor_wait_seconds、environment_seconds；reset 另含 initialization_seconds。真实机制与控制器内部状态须启用实验扩展后通过 `env.experiment.*` 查询，不能从普通 obs 中获得。
