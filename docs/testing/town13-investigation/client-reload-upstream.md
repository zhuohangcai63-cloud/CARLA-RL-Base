# Python 客户端退出与服务关闭：上游依据

2026-09-28 查询。这里区分客户端原生崩溃、服务运行期崩溃和服务停止超时；相似现象不等于相同原因。未启动 CARLA，未修改安装或运行配置。

## 本机已有证据

[native-rendered-reload-epic/outcome.json](native-rendered-reload-epic/outcome.json) 记录了一次客户端 `SIGSEGV / -11`：Town12 路线 22331 已返回、确认 438 tick，随后 Town13 路线 3816 只出现 `START`，没有 `LOADED`。服务在显式停止前仍存活；停止超时后的 `SIGKILL` 是另外的关闭失败。该次没有客户端 native 栈，不能确认是哪段 Python 扩展或 TM 代码出错。原始 probe 保留 `running`，没有重写为正常结束。

[native-town13-transition-reuse/summary.json](native-town13-transition-reuse/summary.json) 中三条短序列全部返回，未复现长批次的运行期 SIGSEGV，但显式停止后仍需强杀。它不能作为长批次崩溃的最小复现，也没有证明正常关闭。

## 已合并的 TM 修复及适用边界

CARLA [PR #8553](https://github.com/carla-simulator/carla/pull/8553) 于 2025-01-22 合并。修复为 `LocalizationStage` 的三个查询/路径函数添加 actor 是否在 `buffer_map` 中的检查，避免访问已失效或尚未注册的车辆 ID 时抛出 `std::out_of_range`。这已列入 [0.9.16 发布说明](https://github.com/carla-simulator/carla/releases/tag/0.9.16)；精确代码见 [提交 fb5f612695db1bfe33c60ecaa658c3bc82d235f8](https://github.com/carla-simulator/carla/commit/fb5f612695db1bfe33c60ecaa658c3bc82d235f8)。

这是一项真实的上游修复，但本机这次只有 SIGSEGV，没有 `unordered_map::at` 或相关函数栈；未经捕获的 C++ 异常通常表现为 SIGABRT。不能因此宣布升级、回移补丁或重建 wheel 已解决本机问题；也不应混用不同版本的客户端与服务作为无验证的修复。

## 相近报告没有给出通用修复

- [CARLA #7597](https://github.com/carla-simulator/carla/issues/7597) 报告 0.9.15 在启用 autopilot 后反复 load/reload/关闭的异常。维护者建议销毁自有车辆、关闭 TM、分离进程，但作者随后仍报告失败，维护者未复现。`tm.shut_down()` 不是该 issue 证实有效的补丁。
- [CARLA #9830](https://github.com/carla-simulator/carla/issues/9830) 区分启动 shell 不转发 SIGTERM、直接给引擎发 SIGTERM 后崩溃，以及容器停止超时。本项目诊断器直接启动引擎，且看到 `RequestExit`，所以 shell 信号转发解释不适用。该报告未解决底层停止故障，其直接 SIGTERM 后 SIGSEGV 也不能解释本机没有崩溃栈的停止等待。

官方 [0.9.15 TM 文档](https://carla.readthedocs.io/en/0.9.15/adv_traffic_manager/) 说明 TM 位于客户端，拥有原生线程和车辆状态缓存。因此服务仍活着并不能排除客户端 LibCarla/TM 故障。下一步应以 faulthandler 的调用位置和 native 客户端栈区分问题；faulthandler 只提供 Python 调用上下文，不能代替 C++ 回溯。

## 新客户端栈与地图加载屏障

[native-rendered-conservative-v2/client.txt](native-rendered-conservative-v2/client.txt) 再次记录客户端 SIGSEGV；faulthandler 指向当时 `session.py:28` 的 `await_loaded_world → world.get_map()`，发生于 Town12 → Town13 切换。该次同样先返回 22331 的 438 tick，3816 尚未 `LOADED`；服务仍活着，随后停止超时被强杀。见 [派生 outcome](native-rendered-conservative-v2/outcome.json)。这是 Python 调用位置证据，尚无 C++ 寄存器/回溯直接证明具体失效指针。

独立复核 CARLA **0.9.15** 上游实现得到以下顺序依据：

1. [Simulator::LoadEpisode](https://github.com/carla-simulator/carla/blob/0.9.15/LibCarla/source/carla/client/detail/Simulator.cpp#L79-L103) 等待每轮 50 ms 后检查 episode ID，未强制等待 TM 初始化完成。相同文件的 `GetCurrentMap` 使用共享 `_cached_map` 和 `_open_drive_file`，这段实现没有显式互斥。
2. [Episode::Listen](https://github.com/carla-simulator/carla/blob/0.9.15/LibCarla/source/carla/client/detail/Episode.cpp#L49-L101) 先 CAS 发布新 `_state`，再处理地图标志和 `OnEpisodeChanged`，然后 `_snapshot.SetValue`，最后调用用户 `on_tick`。`OnEpisodeChanged` 同步调用 TM Reset，不清除 tick 回调；LoadEpisode 复用该 Episode。
3. [TrafficManagerLocal::Reset](https://github.com/carla-simulator/carla/blob/0.9.15/LibCarla/source/carla/trafficmanager/TrafficManagerLocal.cpp#L308-L314) 依次 Release、更新 episode/world、SetupLocalMap、Start。SetupLocalMap 在同一函数栈中调用 `world.GetMap()`，因此用户新 episode 回调到达时这次地图重建已返回。
4. [TCP Client::ReadData](https://github.com/carla-simulator/carla/blob/0.9.15/LibCarla/source/carla/streaming/detail/tcp/Client.cpp#L166-L222) 将收到的缓冲回调及后续读操作放入同一个 `io_context::strand`，同一 episode stream 不会并行执行另一个回调越过尚未返回的 Reset。这也排除了重复帧早返回分支抢先发出新 episode 通知的疑虑。
5. Python [Snapshot.cpp](https://github.com/carla-simulator/carla/blob/0.9.15/PythonAPI/carla/source/libcarla/Snapshot.cpp#L43-L45) 暴露的 `snapshot.id` 是 [WorldSnapshot::GetId](https://github.com/carla-simulator/carla/blob/0.9.15/LibCarla/source/carla/client/WorldSnapshot.h#L22-L25) 返回的 episode ID。普通 `get_snapshot()` 直接读取早已发布的 `_state`，不能替代上述完成通知。

由此采取的应用层措施是在 load 之前注册回调、等待返回 world 的新 episode ID 已经回调，再读取地图；等待本身不 tick，不重发 load，也不重启 TM。先注册可避免漏掉同步模式的唯一初始化通知。完成或异常时移除回调；[World::RemoveOnTick](https://github.com/carla-simulator/carla/blob/0.9.15/LibCarla/source/carla/client/World.cpp#L145-L151) 解析到同一个 Simulator，且 0.9.15 的 [EpisodeProxy::Lock](https://github.com/carla-simulator/carla/blob/0.9.15/LibCarla/source/carla/client/detail/EpisodeProxy.cpp#L38-L47) 只检查引用是否为空，因此旧 world 句柄也能移除这个回调。

这是针对已发现初始化顺序的有依据修补，不覆盖服务端 Vulkan 故障。测试需覆盖加载返回前/后到达的通知、旧 ID 不能解除等待、超时和异常后的回调清理，且等待期间不增加 tick。

## 屏障修补后的有限真实结果

[native-rendered-barrier-v2/outcome.json](native-rendered-barrier-v2/outcome.json) 将这次复测明确记为 `runtime_passed_lifecycle_failed`。在相同 conservative v2 参数组合、Epic、650 m 距离、开启渲染及每路线重新加载条件下，客户端正常退出，四条路线都返回，共确认 **2240 个物理 tick**：

| 路线 | 地图 | 执行结果 | tick |
| --- | --- | --- | --- |
| 22331 / StaticCutIn | Town12 | completed | 438 |
| 3816 / HighwayCutIn | Town13 | tick_budget_reached | 1000 |
| 31736 / VehicleOpensDoorTwoWays | Town13 | completed | 402 |
| 31654 / VehicleOpensDoorTwoWays | Town13 | completed | 400 |

该次没有观察到运行期客户端或服务崩溃，Town12 → Town13 的 `get_map()` 初始化也成功返回；但这是两个地图、四条选定路线的一次有限验证，**不是 Town13 全 41 条或完整 220 条验收**。3816 达到步数预算，不是驾驶任务完成。

服务收到显式 SIGTERM 后 60 秒仍未退出，监督器随后 SIGKILL；所以完整生命周期仍失败。`RequestExit` 日志只说明信号处理发生，不能说明资源已经正常释放。原始 summary 的 `passed=false` 保留。

## 停止等待的进一步上游线索

CARLA [PR #9529](https://github.com/carla-simulator/carla/pull/9529) 于 2026-01-22 合并到 UE4 分支，修复了一类 RPC 停止死锁：同步 RPC 的工作线程等待主线程处理返回值，而停止阶段又等待该工作线程结束。修复增加关闭标志及有界轮询。[0.9.15 Server.h](https://github.com/carla-simulator/carla/blob/0.9.15/LibCarla/source/carla/rpc/Server.h#L99-L114) 对应路径仍直接 `future.get()`；上游 [#9609](https://github.com/carla-simulator/carla/issues/9609) 跟踪该修复的回归测试。

这为当前停止挂起提供了具体候选，但本机还没有停止时的 native 回溯，不能确认属于该死锁，也没有实施或验证回移补丁。恢复 asynchronous 设置、确认世界实际恢复推进，是有价值的分界检查；即便检查通过，也不能排除之后退出路径中的 RPC 或其他线程等待。不把强杀结果归因于缺少对应栈证据的 Vulkan 故障。

该补丁的精确合并提交为 [0c477d65c85059288562ea2fd832dac9d73ce6ee](https://github.com/carla-simulator/carla/commit/0c477d65c85059288562ea2fd832dac9d73ce6ee)，时间晚于 0.9.16 发布，不能将升级到 0.9.16 称作已包含此修复。仅更换 Python wheel 也不会修补正在运行的服务端二进制。

[native-build-readiness.json](native-build-readiness.json) 记录了一次有界只读构建条件检查：现有安装是预编译包，缺少 CARLA/UE4 源码和构建文件；检查的常见位置及服务 conda 中也没有找到匹配 clang、cmake、ninja 或 lld 工具。随包 `.debug` 可解析栈，不等于可重建工程。该检查不是全盘扫描。按官方 [0.9.15 Linux 构建说明](https://carla.readthedocs.io/en/0.9.15/build_linux/)，后续源代码回移需单独准备 CARLA 定制 UE4.26、兼容工具链和版本资产，再保留版本/补丁记录重新验收；本次没有下载、安装、升级或修改二进制。
