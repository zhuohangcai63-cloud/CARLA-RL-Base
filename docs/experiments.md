# 可选干预、轨迹跟踪与公共前缀

这些能力是环境扩展。训练、候选生成、八分支调度、数据集保存和 `G/D/Δ/η` 标签计算属于外部工程。普通观测不包含 NPC 机制、控制器状态、未来路径或未来动作。

## 控制与数据约定

`easycarla.control` 不依赖 CARLA。适配器输入世界右手坐标：`position=[x,y,z]`（米）、`yaw`（弧度，逆时针为正）、`speed`（米/秒），可附加 `velocity`、`dimensions=[length,width,height]`。输出始终是 `float32[3]`，顺序为转向、油门、刹车；**正转向遵循 CARLA 约定，表示向右**。`validate_action` 拒绝非有限值、越界和同时正油门/刹车；`legacy_action` 是旧二维动作的显式转换。

`ControlArbiter` 登记每个对象的控制来源。适配器必须先停用原 autopilot/行为树控制，再释放并接管。每个 tick 每个对象只能提交一次，不能通过同 tick 释放再申请来提交第二次。这个登记器本身不能拦截绕过它的第三方 CARLA 调用；原生适配器仍须审计其行为节点。

外部轨迹格式为：

```python
trajectory = {
    "reference_frame": obs["frame"],
    "times": [0.0, 0.2, 0.4],
    "points": [
        [0.0, 0.0, 0.0, 4.0],
        [0.8, 0.0, 0.0, 4.0],
        [1.6, 0.0, 0.0, 4.0],
    ],
}
```

点字段是 `[x,y,yaw,speed]`，采用**提交瞬间**自车前、左坐标；时间从零开始严格递增。轨迹必须覆盖执行窗口。参考帧过期会在物理推进前拒绝。`TrajectoryTracker.start` 将参考系固定转换为世界坐标，`control(state, dt)` 根据实时自车状态闭环跟踪；轨迹耗尽后禁止继续输出。它不选择轨迹，也不检验外部轨迹是否具有无碰撞保证。短周期重新提交轨迹会保留纵向 PID 状态，跨回合必须新建 tracker。

## 反应式车辆控制器

`ReactiveNPCController(actor_id, route, mechanism="C", target_speed=8, seed=0)` 在每个物理 tick 读取本分支实时状态，并沿共同路径使用纯追踪转向和 PID 纵向控制。`route` 是世界右手坐标点。邻居使用相同状态格式，并包含语义 `id`；输入不能来自固定重放轨迹。

| 预设 | 相对 C 的唯一变化 | 默认值 |
|---|---|---|
| C | 基线 | 主动礼让；延迟 0.2 秒；接受间隙 4 秒 |
| A | 关闭主动礼让 | 保留间隙、信号及紧急制动约束 |
| L | 延迟高层交互确认 | 0.8 秒 |
| G | 缩小接受间隙 | 2 秒 |

这是可配置规则控制器，不是人类行为统计模型。礼让速度乘数默认 0.75、礼让减速上限对应 2 m/s²；刹车比例与加速度换算仅是通用近似，真实车辆需要仿真标定。控制器持续检测前车间隙和交叉路径预计到达时间，分别触发跟车、主动让行与冲突入口间隙检查。高层使用延迟历史，紧急制动和信号约束直接使用当前状态。信号约束由适配器提供 `signal_red` 和 `stop_distance`，缺少这些字段就不会凭空推断信号。

交叉冲突采用当前运动方向的局部预测，适合跟车、直线汇入和交叉通行；它不具备通用城市规划器能力，不能由单元测试推断任意复杂路口可用。首版不提供行人机制或承诺撤回机制。

`set_mechanism("L")` 只切换高层参数，保留路径索引、低层 PID、时间和延迟队列。`state_dict/load_state_dict` 保存控制器版本、参数、路径、PID、历史、触发计数及有界事件日志。日志记录机制切换与状态变化，也允许“配置生效但未触发”的样本。日志覆盖时增加 `dropped_events`。随机扰动使用 `indexed_uniform(seed, semantic_id, physical_tick, channel)`，不受其他分支随机调用次数影响。

## 公共前缀与重复性

独立客户端通过 `env.experiment` 调用服务扩展：`describe_episode()`、`query_state()`、`begin_prefix()`、`finish_prefix()`、`replay_prefix(prefix=..., tolerances=...)`、`set_mechanism(role=..., mechanism=...)` 和 `takeover(role=..., mechanism=..., target_speed=...)`。服务必须启用 `experiments.enabled`。`begin_prefix()` 仅允许在回合第一个决策之前调用；前缀中的机制切换也保存为命令。场景 capability 不提供前缀重演时，请求会被拒绝。

recipe 记录实际注册顺序、角色、蓝图、属性、初始变换、可读取的车辆物理参数（包括车轮及挡位）、自车/控制器路线和来源版本摘要。重建前检查 Scenic 模板摘要，重建后核对来源、角色、物理配置、出生顺序和固定路线；不匹配会明确失败。时钟、观测及任务配置必须与记录一致。真实位置偏差继续由用户指定容差检查。交通规则的上次相对停止线位置和已停车集合也纳入重复性比较，交通灯及停止标志使用固定位置标识。

公共前缀材料是 JSON 可序列化、带版本和 SHA-256 的数据，不使用 pickle。`PrefixRecorder` 默认最多 10,000 个命令、64 MiB；超限明确报错并保留之前完整的材料。每个条目记录命令和执行后的原始状态，保存时复制 NumPy 数组，不保留会被下一步覆盖的共享视图。若执行后记录超限，调用者必须报告记录失败，不能把动作当作未发生。

服务配置 `max_prefix_steps` 默认 5,000，`max_prefix_bytes` 默认 64 MiB。命令数和已耗尽的空间在动作前检查，但未来状态的编码大小无法预知；执行后仍会严格校验字节容量。发生后置溢出，回合进入故障状态，完整旧材料仍可结束导出。不要把这种请求当作未执行动作进行重试。

### 有界材料缓存与分页

`finish_prefix()` 默认返回小型句柄描述，包含 `handle`、`size_bytes`、文件 `sha256` 和 `chunk_bytes=65536`。可直接传回 `replay_prefix(prefix=description, tolerances=...)`，也可使用 `replay_prefix(handle=description["handle"], ...)`。句柄跨同一客户端会话内的 `reset()` 存活；`close()`、客户端异常退出后的会话回收或服务退出会释放材料。新客户端会话无法继承前一客户端占用的缓存预算。

默认材料缓存总容量为 64 MiB、最多 32 个句柄，分别由 `prefix_cache_bytes`、`max_prefix_handles` 配置。导入中的材料也按声明大小占用预算；没有自动驱逐。用 `release_prefix(handle=...)` 显式释放材料或放弃未完成的导入。容量不足时先释放旧句柄，再重试结束材料；不会静默丢弃已有材料。

导出每次最多 64 KiB，不增大动作/观测共享槽。外部工程自行管理文件与数据集：

```python
material = env.experiment.finish_prefix()
offset = 0
with open("prefix.json", "wb") as stream:
    while offset < material["size_bytes"]:
        chunk = env.experiment.export_prefix_chunk(handle=material["handle"], offset=offset)
        stream.write(chunk["data"].tobytes())  # uint8 数组
        offset = chunk["next_offset"]
env.experiment.release_prefix(handle=material["handle"])
```

导入先计算整个文件的 SHA-256，再调用 `begin_prefix_import(size_bytes=..., sha256=...)`。按顺序提交 `import_prefix_chunk(handle=..., offset=..., data=uint8_array)`，每块不超过 65,536 字节；最终调用 `finish_prefix_import(handle=...)` 验证总长度、文件校验和、内部材料校验和与命令容量，再使用这个句柄重演。乱序、越界和摘要不一致都会明确失败；失败的导入可显式释放后重新开始。

小材料可选 `finish_prefix(export=True)` 直接返回旧材料字典，限制为 512 KiB，适合测试与调试。这个限制保证请求槽能够承载同一材料的重演请求；长前缀必须使用句柄或分页，不能依赖把共享槽不断扩容。

`replay_prefix(record, rebuild, execute, read_state, tolerances)` 依次调用：

1. `rebuild(recipe)`：按 recipe 重新构建新回合。
2. `read_state()`：读取初始化状态，不推进。
3. 对每条命令调用 `execute(command)`，通过同一个物理步进端执行，再读取状态比较。

recipe 必须来自实际场景适配器，包含蓝图、出生顺序、信号、路线、采样结果、控制器版本和所有相关随机源。这里的 helper 不会用 seed 替代这些信息，也不会将 recorder 或 WorldSnapshot 当作状态恢复。CARLA、TM 或第三方控制器不能导出/恢复的内部状态属于能力限制，不能被某一次位置接近掩盖。

比较状态契约：

```python
state = {
    "actors": {"ego": {"position": [0, 0, 0], "velocity": [0, 0, 0], "yaw": 0}},
    "signals": {},
    "controllers": {},
    "events": [],
    "history": {},
    "task": {},
}
```

对象键为重建后稳定的语义标识，不能使用 CARLA 临时 actor ID。时间、控制器 tick、事件序号应表达为回合内相对量；绝对 CARLA 帧号应作为诊断元数据。对象位置和速度比较欧氏误差，航向采用最短圆周角差；对象集合、字段结构和离散标签严格相等，其余分区比较最大数值差。支持 `actors` 为带 `semantic_id` 的列表，但调用方仍负责 ID 稳定性。

必须显式提供 `position/velocity/yaw/signals/controllers/events/history/task` 容差。需要比较额外对象数值状态时可提供 `actor_state` 容差；精确一致的额外值无须此项。缺少分区或容差返回 `unverified`，超过阈值或结构变化返回 `failed`，只有全部检查通过才 `passed` 且 `comparable=True`。报告包含初始、每步和末端误差；单次通过不能代表已经建立统计重复性。

## 验证状态

`tests/test_control.py` 覆盖动作、单 tick 控制冲突、坐标/转向符号、轨迹闭环、两车辆闭环、C/A/L/G 分别触发、共同紧急与信号约束、序列化和机制切换连续性。`tests/test_replay.py` 覆盖材料复制与容量、校验和、重建后实际执行、位置和非位置状态漂移、异常传播及缺少容差时不宣称一致。

`tests/test_runtime_experiments.py` 进一步验证默认句柄、跨 reset 保存、64 KiB 分页导出/导入、缓存总容量、错误摘要、机制命令记录、轨迹参考帧重映射、初始物理 manifest 和交通规则隐状态比较。

这些是无 CARLA 的协议与控制测试。真实 CARLA 的跟踪误差、车辆参数标定、机制接管、原生场景可重演范围和重复性误差必须由独立集成报告说明；本文件不宣称这些集成结果已通过。

每个已记录驾驶命令额外包含 `executed_controls`：按回合相对物理tick保存请求自车控制、应用自车控制和托管NPC控制，以及 `executed_seconds`。重演重新执行原命令和闭环控制器，不把记录的NPC输出当作强制开环轨迹；实际控制材料供外部审计。原生TM内部控制无法完整导出，场景限制仍适用。

## 扩展内部边界与材料版本

运行内核显式持有一个 `ExperimentSession`。记录器、材料缓存、历史语义标识和初始 manifest 都由该对象管理，不再向内核临时附加私有字段。扩展通过 `ExperimentRuntimePort` 调用 `rebuild_episode`、`attach_controller`、`require_ready`、`episode_info` 和 `mark_faulted`。重建使用明确的来源配置参数，不临时改写服务的 `scenario` 配置，因此混合来源配置在重演前后保持一致。

场景和事件必须通过 `export_state(actor_roles)` 提供 `schema_version=1` 的数据，扩展不读取 `vars(component)`。新增内部缓存不会自动改变实验材料；有意增加诊断字段时必须更新导出契约和回归测试。已销毁对象的语义身份映射在本回合内保留，下一回合明确清除。

当前材料信封仍为 prefix v1，内部 episode recipe 已升级为 **v2**，原始诊断状态与初始 manifest 为 schema v1。旧 recipe v1 依赖隐式 Python 字段枚举，无法无损迁移成显式状态契约；重演会在重置前拒绝，并要求重新记录前缀。不能仅修改版本号后宣称旧实验仍然等价。

重建之前会一次验证完整材料校验和、所有条目结构、recipe 和状态版本、控制器可加载性、动作/轨迹/机制参数、时钟配置及容差。负数、非有限值、布尔值和拼错的容差名都会在当前回合仍有效时被拒绝。未提供容差仍允许执行诊断重演，但结果只能是 `unverified`。

结束记录时如果缓存已满或内联材料过大，记录保持有效，可释放旧句柄后再次结束；不会将当前前缀置为不可继续的半结束状态。接管进入原生控制器变更后发生任何异常则标记回合故障，避免继续使用部分完成的控制权交接。

一致性报告现在附带位置、速度、航向误差最大的语义对象，以及各分区最多五个最大数值误差的字段路径。这些信息帮助定位 PID、对象动力学、事件状态和历史缓存差异；容差和是否可比较的规则不变。

故障清理会停止当前记录器，并保留故障前已经完整记录的命令，允许使用 `finish_prefix()` 导出。材料外层 `recording_status` 明确标记 `aborted=true`、`reason=episode_faulted`、`includes_faulting_command=false` 和已记录数量；不会把结果未知或记录失败的动作伪造为成功转移。停止后的记录器不能追加命令。重新 `reset()` 或关闭客户端会清除尚未导出的记录；缓存容量不足时可以释放旧句柄再重试导出。故障前已经返回、后来被异步传感器故障追溯影响的帧仍以故障诊断为准，前缀材料不是对这些训练转移的安全背书。

recipe 的仿真配置还固定 `map_layers`、`no_rendering_mode`、`tile_stream_distance`、`actor_active_distance`、`spectator_as_ego` 和 `reload_on_reset`。完整地图与减少流送范围的回合不能仅因为 seed 和 tick 时钟相同就进入一致性比较；这些设置不同会在重建前拒绝。服务端口和 CARLA 本地安装目录不属于等价物理配置，不需要在不同实例间相同。

当流送距离等配置为 `None` 时，表示继承当前 CARLA 世界实际设置，不能把 `None` 本身当作固定的物理值。因此初始 manifest 还记录 `effective_world_settings`，包含服务读取确认的同步步长、渲染、子步和流送设置。重建后在重演任何驾驶命令前严格核对这些实际值；例如同样配置为 `None`、实际分别为 500 米和 3000 米的世界会被拒绝比较。

初始 manifest 与观测共享车辆物理查询的安全条件：关闭物理、休眠或物理状态未知的车辆不调用 `get_physics_control()`，使用原有 `physics=None` 和 `limitations` 表达缺失。p4/p5 Bench2Drive 副本无法见证原生物理开关，其车辆物理状态按未知处理；legacy 场景执行仍可用。manifest 的限制会进入诊断状态和重演比较，缺失物理信息不能因两次都缺失而被判为完整验证通过。可安全查询的车辆仍保留完整物理字段，材料和 manifest 版本不变。
