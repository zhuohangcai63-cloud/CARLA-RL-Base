# Bench2Drive 可执行场景适配

本适配复用上游 `RouteParser`、`RouteScenario`、场景行为树、背景交通、天气、灯光和评价标准。没有调用 evaluator、ScenarioManager 主循环或其按墙钟调度的场景线程。真实 CARLA 验收与纯协议测试分开；目前不能将代码导入成功称为路线验收通过。

## 固定依赖副本

在环境服务的 Conda 环境中安装项目的 Bench2Drive 可选依赖后运行：

```bash
python scripts/prepare_bench2drive.py --source /path/to/Bench2Drive
```

脚本只读取提交 `acbeb0dd8aa99dd0e71b9b75bb542f6fe7c7f3d8` 的 tracked 文件，通过 `git archive` 导出到 `~/.cache/easycarla/bench2drive/<commit>-p4`。不会复制用户 checkout 的未提交修改或写入原目录。`easycarla-lock.json` 记录提交、补丁版本、原文件及修改后文件 SHA256。再次准备时验证已有文件，发现修改直接失败。可以用 `--destination` 指定另一个目录，并在配置中设置 `dependency_root`。

集中补丁仅负责 Python 3 的 XML API、场景构建失败传播、遍历时删除列表元素的跳过问题、停车位列表的跨回合污染，以及原生 actor 控制调用的所有权门控、原生销毁后的资源退登记。保留原源码格式和许可证。不会安装上游已过时的整份 requirements。

## 显式清单与数据隔离

配置示例中的 `scenario.bench2drive` 配置（或直接传给适配器的字典）：

```yaml
manifest: configs/manifests/town12_long_train78.json
purpose: train
dependency_root: /home/your-user/.cache/easycarla/bench2drive/acbeb0dd8aa99dd0e71b9b75bb542f6fe7c7f3d8-p4
allow_unvalidated_takeover: false
```

路径由服务配置加载器解析。`purpose` 必须与清单 split 一致，缺少 manifest 时直接报错。`reset(options={"route_id": "0"})` 或 `scenario_index` 只可选择清单内路线，不能跳到评估集。无指定路线时用回合 seed 在当前清单抽样。

如部署使用明确的地图变体，可显式配置 `town_overrides: {Town10HD: Town10HD_Opt}`。清单的逻辑town不变，metadata同时记录实际加载地图；默认没有地图替换。本机这两种地图的OpenDrive文件SHA256相同，但变体测试仍需单独报告，不自动推广为另一地图或全部路线已验证。

| 清单 | 内容 | 用途 |
|---|---|---|
| `town12_long_train78.json` | 原90条Town12长路线中26组几何的78条 | 训练；不是1100条新版短训练路线 |
| `town12_long_dev12.json` | 同文件剩余4组几何的12条 | 开发留出；不替代最终基准评估 |
| `v004_eval220.json` | 新版220条 | 保留评估 |
| `legacy_eval220.json` | 旧版220条 | 保留评估 |
| `legacy_dev10_reserved.json` | 旧版220子集10条 | 保留评估，不因名字dev而用于训练 |

每条清单记录文件摘要、路线几何指纹和场景触发点指纹。90条路线实际是30种几何各三份（ID为r、r+30、r+60）。审计发现80/10按ID划分会重复10条完整几何及485个触发点，因此改为按几何分组的 **78/12** 划分（`id % 30 < 26`训练，其余开发），精确路线和触发点均无跨split重叠。`overlap-audit.json` 保存两两内容比对。训练与上述保留评估清单没有发现精确触发点重叠；这不证明地理位置或任务分布完全独立。旧版Dev10和旧版220共享10条路线。`assert_evaluation_isolation` 遇到重复内容拒绝将其作为独立评估。

## 生命周期与物理步进

运行内核先调用 `adapter.town(seed, options)` 并加载地图，再 `prepare(session, seed, options)`。适配器自行创建自车、原生场景和背景交通，不调用旧的随机出生、随机路线或长尾事件逻辑。

初始化期间，原生隐藏的 `tick/wait_for_tick` 都转给唯一 `WorldSession.tick(phase="initialization")`；批量命令的 tick 参数不会交给 CARLA，避免重复推进。运行期这些隐藏请求被记为 deferred，不实际推进；同步RPC生成 actor 后，由当前动作的下一次正常物理 tick 激活。每个物理 tick 在控制前同步检查场景构建条件，不使用墙钟线程。

`before_tick()` 构造临近场景；`resolve_control(action)` 更新原生行为和NPC控制，同时捕获 ControlLoss 等自车扰动；运行内核只提交返回的最终自车控制一次。物理 tick 后 `after_tick(snapshot)` 更新 GameTime、DataProvider，并在结束帧运行原生 criteria 和 timeout。`info()` 返回评价值、原生场景状态、pending场景、结束/成功/超时和隐藏tick计数。`native_success` 专指 `RouteCompletionTest` 成功，不能解释成无碰撞、无违规或官方组合分数满分。原生ControlLoss可能向刹车动作叠加油门，这属于明确记录的原生变换，不重新解释成客户端动作错误。

所有通过 world/client 创建的 actor 均登记资源归属，native `actor.get_world()` 也返回登记代理。清理只处理本回合创建的资源；不删除世界全部车辆、不停止CARLA、不启动下一回合。构造场景失败直接失败，禁止上游默认的打印后跳过场景行为。

## 原生行为与研究接管

`capabilities()` 分别返回原生执行、重建、重演、角色接管及实际验证状态。`native_execution: true` 表示有实际执行实现，`native_validation: not_run` 明确没有宣称真实测试通过。

ParkingCutIn、StaticCutIn、HighwayCutIn 的已构造 adversary 提供研究接管钩子：`takeover(role)` 返回 actor 和固定路径，并关闭其 autopilot。随后原生 Python 控制代码的 `apply_control`、`set_autopilot`、target velocity 调用都被拦截；运行内核的通用控制器成为有效控制源。原生触发、评价和销毁照常执行，不延长参与者存活时间。源配置默认禁止未经真实验证的接管；必须显式 `allow_unvalidated_takeover: true`。尚在地下等待放置或已经销毁的角色不能接管。接管在同回合不可撤销。

Traffic Manager 和原生行为树内部状态无法完整导出，因此原生 Bench2Drive 的 `prefix_replay` 返回 **false** 及具体限制。固定 recipe、相同seed、重建路线均不等于完整状态恢复。通用自有场景上的前缀重演由其他环境扩展实现；不能拿它的测试结果冒充所有Bench2Drive场景支持。

## 原生统计与原始度量

`info.events.native_statistics` 直接调用固定提交的 `StatisticsManager`，返回上游RouteRecord（路线完成率、违规乘积、组合驾驶分数DS）、原始交通事件及`final`标记。运行中的分数是部分结果，上游临时record状态可能显示Failed；只有`final`才表示原生回合结束。该封装不调用上游文件写入或HTTP方法，也不输出训练标签。

`efficiency_mean_percent` 按上游工具规则取有效MIN_SPEED检查点百分比的平均值；无有效检查点时返回null，不能解释为零分。为保留本机观察到的上游异常百分比，原始events仍完整输出，包括被工具规则排除的>1000%值。

显式配置 `native_metrics: true` 时，`telemetry`返回有界最近物理tick窗口（默认覆盖一个标准decision窗口，`metric_history_ticks`可设1至4096），包含上游舒适度工具需要的 acceleration、angular_velocity、forward/right vectors、location、rotation，以及frame和timestamp。这里保留**CARLA世界左手坐标、角速度degrees/s、旋转degrees**以对应上游原始metric_info；它位于诊断info，不混入采用FLU/radian的策略观测。外部工程按frame去重保存；长轨迹执行时应增大窗口或缩短执行窗口，以免物理tick样本被覆盖。

环境不声称已经计算官方舒适度分数（`smoothness_score: null`）。上游舒适度脚本默认0.1秒采样，输入该脚本前应依据返回时间戳按适当频率采样并审计其角速度处理，不能将degrees/s未经确认当作rad/s。模型、标签与跨路线实验聚合仍在外部工程。

## 验证

```bash
python -m pytest tests/test_bench2drive.py -q
```

此测试不连接CARLA，覆盖清单隔离、内容重叠、受控选路、隐藏tick抑制、batch tick禁用、原生自车扰动捕获、NPC控制所有权、补丁可解析性和数据完整性。真实路线验收应使用独立实例按显式清单逐条运行，保留构造失败、场景触发和完成状态，区分程序执行完成与驾驶任务成功；不得用测试框架假世界代替真实集成验收。


## 平台加固后的控制与状态边界

适配器实现统一 `EpisodeProtocol`，返回 `ResolvedControl`、`ScenarioFacts`，声明支持的任务配置。内核不再按来源字符串分支。`export_state()` 输出版本化诊断状态及明确的不可恢复限制，不通过扫描 Python 实例属性生成研究快照。

所有原生车辆、行人控制写入通过同一个 `session.controls` 网关，按物理 tick 提交有回执的批量命令。批量出生中的原生 autopilot 链登记为原生委托所有权；未支持的已有actor批量控制命令在任何RPC之前拒绝。角色接管显式停止TM及constant velocity，取消旧来源待提交控制，并登记新的唯一控制源。

CARLA 内置 `ConstantVelocityAgent` 的窄适配子类保留其规划和驾驶逻辑，将嵌套的 constant-velocity 写入导向网关；其碰撞传感器登记资源归属，回调只写有界队列，在下一物理步前处理。接管后旧回调不能重新启用原生执行器。CARLA没有constant-velocity命令对象，因此该设置使用原生setter并通过后续同步RPC建立顺序屏障；不宣称单个setter自身具有回执。

**已记录的原生初始化行为变化：** Bench2Drive背景交通会对刚出生且已启用TM的车辆同时设置初始速度。适配器让该车出生的第一物理tick由原生手动来源设置速度，下一tick恢复TM，避免两个执行源竞争。只适用于尚未进入世界快照的新actor；对已存在TM车辆直接写速度仍报控制冲突。`native_spawn_velocity_handoffs` 返回这种交接次数。这是明确的一tick初始化适配，不能宣称与上游逐位或逐tick完全等价。

ParkingCutIn和StaticCutIn仅在 `waiting_for_trigger` 阶段允许实验接管；HighwayCutIn还允许 `synchronizing_arrival` 阶段。Static/Highway必须先在已识别的原生树中观察到该actor的BatchActorTransformSetter/ActorTransformSetter成功；未知包装、缺少预期运动节点或无法识别放置节点均拒绝。判断不使用绝对海拔阈值：Highway原生下移100m的隐藏车在高海拔地图仍可能高于海平面。进入原生CutIn/BasicAgent运动阶段后拒绝接管。原生销毁被接管actor时，同时退登记通用控制器并记录 `native_lifetime_ended`，不延长actor寿命、不让旧控制器继续向已销毁actor写控制。

补丁版本4还复用一个有界静态路线拓扑模板。键包含OpenDrive摘要及采样分辨率，每个调用方得到独立planner对象和转弯判断状态，不缓存world、actor或回合。旧版本副本继续保留作审计，运行默认要求重新执行prepare脚本生成 `-p4` 副本。

## 原生全清单诊断运行器

`validate_routes.py`仍是只依赖轻量客户端的服务验收入口。另提供服务端原生适配器诊断，用于隔离客户端和参考驾驶器以外的场景问题：

```bash
python scripts/validate_native_matrix.py \
  --manifest configs/manifests/v004_eval220.json \
  --output route-matrix.json --port 2100 --tm-port 8100 \
  --ticks 1500 --no-rendering --map-layers none \
  --tile-stream-distance 500 --actor-active-distance 500
```

每张地图单独Python/TM生命周期，每条路线保存构造、实际激活、子场景完成、原生路线成功、预算截止和错误；原子更新报告。CARLA崩溃、未知执行结果或子进程abort时停止，不重放step，也不自动重启模拟器。该命令明确关闭渲染、加载NONE图层并缩小远场范围，只用于该配置下的结构化环境验证，不能推广为全图层官方评估或图像传感器验证。默认不传这些参数时保留完整配置。

补丁版本4进一步明确两种已有原生后台交通行为：窄车道停车先停止TM委托、再提交零速度（仍在同一物理tick）；交叉口留空的零速度脉冲使用命名的一tick临时接管，下一tick恢复TM。若原生代码在同tick明确要求TM保持关闭，则取消排定的恢复。普通客户端或其他来源仍不能直接改写已由TM控制的车辆速度。这些兼容行为和计数位于适配模块，未放宽通用控制网关。


## 子场景进度的精确定义

原生 `BasicScenario.behavior_tree` 是外层序列，包含等待触发、实际行为及以 `WaitForever` 结尾的收尾逻辑。外层 `RUNNING` 可以表示尚未触发或已经完成，不能用它判断激活；正常结束的子场景外层也不会变成 `SUCCESS`。

`native_scenario_progress_schema: 1` 明确区分 `wrapper_status` 与 `actual_behavior_status`。适配器核验固定上游的路线包装结构后，只把实际行为进入非 `INVALID` 状态记为 `activation_observed`，实际行为达到 `SUCCESS` 记为 `completion_observed`；两个正向观测在本回合锁存。无法识别的结构返回空值及原因，不猜测。原来的 `status` 保留作外层诊断。原生总路线结束、路线完成测试成功、子场景实际完成、零违规驾驶是四种不同结论。

`validate_native_routes.py` 版本2保存该schema及物理帧上的进度状态变化。旧版本1报告的 `activated`/`completed` 是外层状态推导，不能作为实际触发/完成证据；旧报告保持原样供审计，新的针对性报告单独保存。旧表的构造、物理tick、原生criteria、预算截止与异常证据不受此进度指标修正影响。
