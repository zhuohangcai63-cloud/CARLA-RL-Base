# Bench2Drive 验证记录（2026-09-27）

运行环境为本机 CARLA 0.9.15、Python 3.10、固定Bench2Drive提交 `acbeb0dd8aa99dd0e71b9b75bb542f6fe7c7f3d8`。以下分开记录无仿真测试、原生源码导入、真实运行及失败，不推断未运行场景的支持能力。

## 已通过

- `python -m pytest tests/test_bench2drive.py -q`：12个无服务器测试通过，覆盖清单隔离、内容指纹、tick所有权、控制门控、资源退登记、唯一稳定spawn角色、原生事件序列化及批跑未知结果不重放。
- 在服务Conda环境导入全部原生场景类；90条长训练路线、新版220、旧版220和旧版Dev10均可由原生RouteParser解析，所引用的38/42/44/10种场景类型均有实现。此项只证明依赖和解析可用。
- 独立真实CARLA实例RPC2100/TM8100，显式将清单Town10HD映射到Town10HD_Opt。两个本机地图OpenDrive文件SHA256均为 `5d883b799f634030af92be1e9d79d107845540ba04338e8c60e095be1aef7be7`。

| 路线 | 原生场景 | 实际物理tick | 实际激活 | 路线完成 | 结果 |
|---|---|---:|---|---|---|
| 28147 | SignalizedJunctionLeftTurnEnterFlow | 405 | 是 | 是，100% | 执行通过；CollisionTest为0；MinSpeedTest失败 |
| 25134 | T_Junction | 1000 | 是 | 否，56.49% | 执行通过；达到测试步数上限，非驾驶任务通过 |

两个回合连续运行，等待客户端计算时帧不前进，物理执行tick计数与请求执行tick完全相同。初始化各使用4个受管tick。原生子场景在测试截止时仍为RUNNING，不将路线完成错误等同于每个子场景完成。回合清理后读取世界，车辆、行人、传感器和行人控制器均为0。

原始结果见 [bench2drive-real.json](bench2drive-real.json)。报告中的 `status: passed` 指本测试的场景执行/时钟/资源检查通过；驾驶成功独立记录为 `native_success` 和每项 criteria。

在该次直接适配器测试中，路线28147总耗时7.76秒，其中受管仿真tick累计1.37秒；25134总耗时8.83秒，其中受管仿真tick累计5.51秒。这里没有共享内存客户端，不能将余下时间称作通信耗时；通信性能由独立协议测试测量。

复现命令（需提前运行独立CARLA2100，且当前地图为Town10HD_Opt）：

```bash
EASYCARLA_B2D_INTEGRATION=1 \
EASYCARLA_B2D_RELOAD=0 \
EASYCARLA_B2D_OPT_TOWN10=1 \
EASYCARLA_B2D_ROUTES=28147,25134 \
EASYCARLA_B2D_TICKS=1000 \
python -m pytest tests/test_bench2drive_integration.py -q -s
```

## 已发现并修复

首次真实执行到404tick时，测试用BehaviorAgent在计划结束后继续调用run_step，产生空waypoint异常。改为检测agent.done并制动；这是测试驱动修复，没有更改原生场景。

首次清理暴露了CARLA的actor.is_alive缓存：上游criterion已同步销毁传感器，但未产生下一帧时is_alive仍为真，导致环境registry重复销毁。已将上游destroy和DestroyActor batch确认集中接入资源退登记；无需额外cleanup tick。修复后两个回合均清理成功。初次失败证据保留在 [bench2drive-first-execution-failed.json](bench2drive-first-execution-failed.json)。

## 两个独立Conda通过共享内存联调

服务使用 `CARLA-RL-Base` 环境，客户端使用独立 `carla-client-test` 环境（实际确认没有CARLA、Scenic、Torch）。使用新runtime和真实仿真运行两次各10步的回合，再由客户端中的纯NumPy路线跟随器完成28147，共188个决策步。对象历史有最多13个不同对象，检查了ID唯一、shape/dtype、观测副本不覆盖、静态缓存、每步4个物理tick及终止时不足4tick。客户端close后服务继续存活；服务停止后IPC和动态actor均无残留。

官方StatisticsManager返回最终RC=100、IP=0.6、DS=60；简单外部驾驶器发生车辆碰撞，完成路线不能等同完美驾驶。原始交通事件成功序列化传回独立客户端。有界物理遥测也随info传回，没有加入策略obs。

完整路线的通信+复制p50/p95约 **1.15/1.54毫秒**，仿真tick累计p50/p95约 **12.06/14.58毫秒/决策步**；包含全部后台处理的总往返约63.16/86.11毫秒。详见 [跨Conda报告](bench2drive-service-real.json)，测试入口 `tests/test_bench2drive_service.py`。

该次测试结束时SIGTERM打断命名信号量等待，引发SignalError，虽然finally已完整清理，服务仍退出1。此问题已由服务层修复；这份真实报告仍保留修复前exit_code=1，未冒充修复后真实复测。后续测试已加exit_code=0断言。常规 `pytest` 默认跳过这两个真实测试入口。

## 未通过或未运行

- 本机独立2100实例跨图加载Town03时，UE4重复Signal11崩溃；最小复现仅含原生 `client.get_world()` 和 `client.load_world('Town03')`，没有导入easycarla或设置同步模式。证据见 [bench2drive-server-crash.txt](bench2drive-server-crash.txt)。尚未确定驱动、地图或并发仿真中的具体原因，不能归因为适配器。
- ParkingCutIn、StaticCutIn、HighwayCutIn的真实运行/接管未验收，因为代表路线需要其他地图。角色接管默认仍需显式实验选项，能力不宣称已验证。
- 未跑完整220条真实路线；不能将42种场景类可导入视为220条可运行。
- 原生Traffic Manager和树内部状态不能完整导出，因此Bench2Drive原生公共前缀重演明确不支持，不将相同seed视为状态恢复。
