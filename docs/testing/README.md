# 首轮重构验收快照（2026-09-27，本机 Linux）

**后续架构加固和最新测试见 [平台加固报告](hardening/README.md)。下面保留首轮历史结果，不代表后续修复后的最新状态。**

本次交付已经实现独立环境服务、独立客户端、双场景源和可选实验接口。**全部计划尚未通过最终验收**：完整220路线、代表cut-in真实接管，以及稳定通过公共前缀重复性验证仍有缺口。下面将实现、执行成功、驾驶成功、重复性通过分开记录。

## 测试环境与可复现入口

- 服务：`/home/czh/software/miniconda3/envs/CARLA-RL-Base`，Python 3.10.20、CARLA 0.9.15、Scenic 3.1.1、Gymnasium 1.3.0、NumPy 2.2.6、posix_ipc 1.3.2。
- 独立客户端：`/home/czh/software/miniconda3/envs/carla-client-test`，Python 3.11.16、NumPy 2.4.6、posix_ipc；从仓库外确认没有安装 CARLA、Scenic、Torch 或 easycarla 服务包。
- CARLA RPC2100/TM8100 和 RPC2200/TM8200 为本次独立启动的专用实例；不是已有用户服务器。测试使用 RTX 5060 Ti，低画质离屏渲染。
- 依赖安装后 `python -m pip check` 通过。未验证全新服务 Conda 从零重建；约束文件不等于跨平台哈希锁。

```bash
EASYCARLA_CLIENT_PYTHON=/home/czh/software/miniconda3/envs/carla-client-test/bin/python \
  python -m pytest -q

# 仅在没有服务占用的专用CARLA端口运行
EASYCARLA_REAL_PORT=2200 EASYCARLA_REAL_REPORT_DIR=docs/testing \
  python -m pytest tests/test_carla_integration.py -q
```

默认测试不会启动或伪造 CARLA。真实测试入口默认跳过。协议跨进程测试实际创建 POSIX 对象并启动独立进程，覆盖副本/借用视图、重复请求、序号冲突、旧会话、容量溢出、超时、客户端/服务强杀、残留回收、实例冲突、重连、fork拒绝及信号退出。额外修复了存活探测短占文件锁被误报ClientBusy的竞态，并有确定性回归。

最终不连接CARLA服务器的测试：**346 passed，12 skipped**，见 [pytest输出](pytest-final.txt)。12个跳过项是显式选择的真实仿真入口；已运行的真实测试证据单独列在下方。其中跨Conda假后端只能证明通信，不证明仿真。

## 真实运行结果

| 验收项 | 结论 | 证据/范围 |
|---|---|---|
| 两独立Conda，共享内存reset/step/close | 通过 | [Bench2Drive服务](bench2drive-service-real.json)、[mixed服务](mixed-service-real.json)、[多模态服务](sensors-service-real.json) |
| 同一服务双来源连续4回合 | 通过 | seed 0/1/2/3依次Bench2Drive/Scenic/Bench2Drive/Scenic；每回合20决策步 |
| RGB/语义BEV/LiDAR目标帧一致 | 通过已测试窗口 | [帧同步测试](carla-sensors.json)，新完整姿态LiDAR另有倾斜坐标单元测试及60步真实多模态通信 |
| 客户端等待不推进、动作窗口无额外tick、终态不自动reset | 通过已测试窗口 | sensor测试及原生B2D/服务测试；物理帧和管理器计数分别核对 |
| Scenic五类事件连续各2回合 | 已执行，事件结果见下表 | 总10回合；执行成功不代表每类事件都完成 |
| Bench2Drive两原生场景激活 | 通过 | 28147与25134；Town10HD→Town10HD_Opt为显式验证配置 |
| Bench2Drive原生评分 | 已传回 | 28147共享内存运行 RC100/IP0.6/DS60；参考控制器有碰撞 |
| 两NPC控制权与机制切换 | 通过有限范围 | [真实报告](carla-mechanism-switch.json)：near→L、far→G，25决策步；near跟车47tick，far没有触发gap规则 |
| 公共前缀重建/重演实际执行 | 已实现且实际运行 | 记录命令、控制器状态、初始物理配置并重建；不是快照恢复 |
| 可比较起点重复性 | **未通过稳定性验收** | 常规3组1通过2失败；固定20初始化tick对照2通过1失败；未放宽原阈值，见[诊断报告](prefix-repeatability-report.md) |
| 全220评估路线、三个cut-in接管 | **未运行完整验收** | [220条逐项覆盖](bench2drive-v004-coverage.json)：2条有真实证据、218条未运行；接管实现未获真实验证 |

Bench2Drive完整独立报告与复现命令见 [bench2drive-report.md](bench2drive-report.md)。其早期真实跨Conda报告保留SignalError导致退出1的原始记录；该服务问题已修复，后续mixed服务正常SIGTERM退出0。不能把旧记录改写成修复后的测试结果。

### Scenic事件结果

这里使用无学习的路线跟随控制器；它没有针对障碍物的避让策略。碰撞是实际驾驶结果，不是环境故障，也不能据此证明事件完成。

| 事件 | seed 42 | seed 43 | 证据 |
|---|---|---|---|
| 行人横穿 | 2次触发，1完成、1超时 | 2次触发、2超时 | [原始记录](carla-pedestrian_crossing.json) |
| 前车急刹 | 触发1次，随后碰撞 | 测试窗口内未触发 | [原始记录](carla-hard_break_route.json) |
| 停车切入 | 触发并完成1次 | 触发2次，窗口内未完成 | [原始记录](carla-parking_cut_in.json) |
| 车辆开门 | 触发，随后碰撞 | 显式跳过，no_side_lane | [原始记录](carla-vehicle_opens_door.json) |
| 施工障碍 | 触发，随后碰撞 | 触发，随后碰撞 | [原始记录](carla-construction_obstacle.json) |

部分事件的“完成”路径仍需更合适的受控驾驶验收，不能只凭程序未崩溃标为通过。第一次批量Scenic测试在下一次 `client.get_world()` 处原生Python客户端abort，服务端当时仍存活；已保留[日志](scenic-native-client-abort.txt)，重启专用实例后分批运行剩余场景成功，根因未确定。

## 性能（单位：毫秒/决策请求）

均使用默认返回副本。p50/p95独立计算，不能把分位数逐列相加。通信余量包含调度、编码、共享内存、摘要检查和复制，不是纯内存带宽；本机并发测试负载也会影响结果。

| 指标 | 结构化mixed，80步 p50/p95 | RGB+BEV+LiDAR，60步 p50/p95 |
|---|---:|---:|
| 客户端总往返 | 41.29 / 68.17 | 79.83 / 110.05 |
| 通信与复制余量 | 0.91 / 1.16 | 1.51 / 2.34 |
| 4个物理tick累计 | 8.36 / 14.11 | 11.07 / 28.87 |
| 传感器等待累计 | 8.37 / 8.42 | 45.15 / 55.12 |
| reset总往返 | 262.13 / 3785.11 | 2493.66 / 4639.07 |

默认每物理帧2ms的稀疏碰撞等待包含在传感器等待内。CARLA稀疏碰撞流没有零消息完成标记，有限等待不能数学保证绝无迟到；迟到消息使回合故障并返回受影响帧，外部工程需要剔除对应转移，详见[同步限制](../sensor-synchronization.md)。

无CARLA假后端的 [跨Conda测量](ipc-cross-conda.json) 单独报告。复现性能测试使用 `scripts/measure_service.py`；验证配置在 `configs/validation/`，其Town10子集是显式验收用评估材料，不是训练清单。

## 下一轮验收入口

- 在能稳定加载目标地图的独占CARLA上运行 `scripts/validate_routes.py`，保留逐路线失败和未运行条目。
- 对 ParkingCutIn、StaticCutIn、HighwayCutIn 的可交接阶段验证原生控制停用及生命周期限制。
- 调查CARLA重建后的微小动力学差异；由研究验证配置明确给出各分区容差。当前不得把普通Scenic公共前缀标为稳定可比较，也不支持Bench2Drive原生前缀恢复。
- 汇入、交叉通行还需要更多机制激活与重复性验收；未完成的Scenic事件路径需要完成验收。

## 固定真实观测的本地编码与复制微基准

采集reset观测并关闭客户端会话后各测200次；下列不是IPC往返或仿真时间。编码不含发布摘要，校验/解码/复制包含header、SHA256与独立数组副本。

| 指标（ms，p50/p95） | 结构化 | RGB+BEV+LiDAR |
|---|---:|---:|
| 编码 | 0.056/0.109 | 0.101/0.220 |
| 校验/解码/复制 | 0.107/0.146 | 0.316/0.480 |

原始结果：[结构化](codec-structured.json)、[多模态](codec-sensors.json)。脚本：`scripts/measure_codec.py`。

## 新通用几何的动态执行补测

`interaction_merge` 与 `interaction_crossing` 均使用seed7：汇入在32决策步因真实碰撞终止，交叉通行执行80步。两个NPC控制器均按物理tick闭环执行，真实执行测试2项通过；汇入近车触发主动礼让44tick、间隙等待28tick，其他角色在窗口内未触发这些机制。这只升级“动态执行”证据，未验证它们的公共前缀重复性；见[汇入](carla-reactive-merge.json)、[交叉通行](carla-reactive-crossing.json)。

## 构建与资源收尾

两个独立wheel均构建成功，`easycarla --help`、源码编译及`git diff --check`通过。实例 b2d_live/mixed_live/sensors_live/structured_live 的客户端和服务锁均已释放；专用2100/2200世界的车辆、行人、控制器和传感器均为0，见[清理记录](cleanup.json)。本次启动的测试CARLA进程已退出，未关闭其他外部实例。
