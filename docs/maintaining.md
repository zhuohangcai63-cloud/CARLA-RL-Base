# 维护与扩展入口

服务端与客户端分别发布；协议定义只在 `packages/client` 中维护。模型、候选生成、训练器、经验回放和实验分支调度由外部仓库实现。修改环境时，应能说明变化属于下表哪一层。

| 变化 | 入口 | 必须保持的约束 |
|---|---|---|
| 新场景来源 | `scenarios/contracts.py`、`scenarios/selection.py` | 实现可执行 episode 契约；明确任务配置、清理与能力限制 |
| 新 Scenic 事件 | `scenarios/events` | 独立组件；登记资源；通过控制网关提交；记录跳过与失败 |
| 新执行器或控制器 | `control` | 显式控制权交接；每个 actor 每 tick 一个来源；禁止自行推进世界 |
| 新观测字段或传感器 | `observations`、`simulation/sensors.py` | 物理帧一致；声明坐标、单位、容量、有效掩码及失败语义 |
| 奖励或终止规则 | `tasks.py` | 从实际转移事实计算；区分自然终止、时间截断和环境故障 |
| 新实验诊断 | `extensions` | 版本化数据导出；预检先于修改状态；不混入默认策略观测 |
| 新客户端命令 | `packages/client`、`service` | 有界数据；会话/序号校验；超时后不自动重放动作 |

调用关系是客户端 → 服务 → 运行内核 → 场景/控制/观测/任务，物理推进由 `WorldSession` 管理。第三方兼容补丁集中在 Bench2Drive 适配器及准备脚本；不向运行内核增加来源专属判断。旧 Gym 入口仅转换接口，不能重新实现奖励、清理或步进逻辑。

## 修改后验证

在服务环境执行无仿真测试：

```bash
python -m pytest -q
python scripts/check_wheel_install.py --output /tmp/wheel-install.json
```

第一条命令包括真正跨进程的 POSIX 通信测试，真实 CARLA 入口默认跳过。第二条在仓库外用全新环境安装 wheel，验证两端包的依赖与导入边界。CI 同时检查 Python 3.10/3.11 核心、Scenic 编译与安装；远程运行状态以 CI 实际结果为准。

改变物理步进、传感器、场景适配、控制提交或结束规则时，再运行独占 CARLA 的相关真实测试。不要和使用同一世界的服务并发运行：

```bash
EASYCARLA_REAL_PORT=2000 python -m pytest tests/test_carla_integration.py -q
python scripts/validate_repeatability.py --port 2000 --trials 5 \
  --output /tmp/repeatability.json
```

对应脚本与依赖说明见[安装文档](installation.md)、[事件组件](event-components.md)和[场景能力表](scenario-capabilities.md)。headless、地图层、流式加载范围会改变测试条件，必须保存在报告中。服务关闭会恢复接管前的世界设置；专用 headless 测试应按安装文档先设置外部 CARLA。

## 兼容性与证据

- 改动动作尺度、观测字段、奖励、结束条件或故障处理时，更新[行为变化](behavior-changes.md)及对应契约测试。共享内存布局、配方、诊断状态各自有版本，不用 Python 对象内部字段作为持久协议。
- 无效请求应在修改状态前拒绝；已经提交且结果不确定的动作使会话进入故障流程。不要为了让测试通过而自动 reset、额外 tick 或补发 step。
- 测试报告记录代码/依赖版本、来源清单、种子、实际物理设置、预算和失败原因。执行无异常、场景真实触发、路线完成、驾驶成功及可比较起点分别报告。
- 能力声明应限定路线、角色、阶段与测试窗口。支持接口不等于真实验收通过；短窗口通过不推广为全路线、任意随机种子或长时间重演保证。
- 保留原失败材料，修复后单独记录复测。模拟器崩溃与测试预算耗尽不能改写为成功；未运行的真实检查不能被 fake 测试替代。

当前证据与仍受限范围集中在[本轮验收报告](testing/hardening/README.md)。

六项正确性补丁的回归结果见 [修复验证记录](testing/correctness-fixes.json)。新增真实服务检查使用独占 Scenic 和 p6 Bench2Drive 实例，运行 `EASYCARLA_FIX_INSTANCES=scenic_instance,native_instance python -m pytest tests/test_correctness_carla.py -q`；两个服务都应开启 experiments，采用 planning_v1、0.05 秒物理步长、0.1 秒决策步长，Native 使用 Town12 训练清单。该检查覆盖初始物理清单、预检拒绝和后续步进，不替代全路线或长期性能验证。
