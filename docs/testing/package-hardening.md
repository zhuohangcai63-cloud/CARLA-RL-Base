# 架构加固：扩展、安装与协议证据

本页只描述代码契约和无 CARLA 仿真的测试。真实仿真、Bench2Drive 覆盖及重复性以本目录对应报告为准；CI 工作流已经加入仓库，尚未在远程运行。

## 边界变化

- `ExperimentSession` 显式持有记录器、语义身份、初始 manifest 和材料缓存；`ExperimentRuntimePort` 约定它能够调用的内核操作。
- 场景诊断使用版本化 `export_state`，不再枚举事件对象的 `vars()`。控制网关的模式、所有者与待提交控制也进入比较。
- 配方固定物理时钟、渲染模式、地图层、流送距离、actor 活动距离及重载策略；来源重建不改写运行时配置。
- 容差、所有命令、控制器状态和材料版本在重建前验证。故障时保留最后完整前缀并停止追加；后续导出明确标记中止状态。
- reset 保留已完成材料句柄；客户端 close、异常退出后的会话回收和服务退出释放客户端材料。新客户端不会继承旧客户端占用的缓存容量。
- CLI 配置路径相对于配置目录，支持明确 `path_base`，并递归处理 mixed/fallback/Bench2Drive；清单内部路线路径仍属于 Bench2Drive 适配器。默认 CARLA 安装目录支持 `CARLA_ROOT` 与当前用户主目录。

## IPC 审计修复

命名信号量仍是发布边界，客户端仍一次一个请求。此次额外修复了三个会影响故障语义的问题：

1. 非有限超时值可能使截止时间永不触发；现在只接受有限正超时。
2. 同时调用 close 和 step 会在请求未完成时断开映射；现在 close 与请求共用同一把本地锁，忙时明确拒绝关闭并保留正在执行的请求。
3. 信号量已经发布、返回前发生异常时可能被误判为尚未提交；现在在尝试发布前保守标记提交，任何此后的不确定异常都报告 `UnknownOutcomeError`，禁止重放动作。

JSON 元数据接收端也明确拒绝 NaN/Infinity，与发送端规则一致。请求异常不会沿用上一请求的后端耗时。

## 安装与测试证据

- [独立 wheel 安装报告](wheel-install-hardening.json)：两个全新临时 venv，服务 Python 3.10、客户端 Python 3.11，仅从 wheel 安装，工作目录位于仓库外；4 次 reset、30 次 step、close/reconnect、观测副本、pip check、POSIX 对象清理均通过。客户端未安装 CARLA、Scenic、Gymnasium、Torch 或服务包。
- [核心依赖独立环境报告](core-only-ci-hardening.json)：新建 Python 3.11 venv，不安装 CARLA/Scenic/Torch，按本轮最终复测时的当前仓库安装两包及测试依赖后执行完整测试集，468 项通过、26 项跳过；CARLA 类型、Scenic、Bench2Drive 可选依赖或真实仿真相关的跳过不作为通过。依赖齐全的服务环境测试与真实仿真另行报告。
- `tests/test_runtime_experiments.py`、`test_replay.py`：覆盖显式状态导出、配方版本/物理配置、全命令预检、机制/轨迹重演、可定位的误差报告、故障前缀与材料生命周期。
- `tests/test_ipc.py`：实际独立进程共享内存测试，新增客户端异常退出后材料预算回收、并发 close 和发布后异常测试。
- `tests/test_cli_config.py`：从其他工作目录解析全部示例配置，涵盖递归路径、直接字典的 `~`、环境变量默认及循环 YAML 拒绝。

这些测试不把 fake backend 当作 CARLA，也不把两个 venv 的验证描述成新建 Conda 环境的验收。已有真实跨 Conda 联调应阅读其单独报告。
