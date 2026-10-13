# Python 客户端切图 SIGSEGV：TM 与地图缓存只读审查

日期：2026-09-28。本文读取代码与已有诊断材料；审查者没有启动 CARLA。它不把客户端原生崩溃归入 UE/Vulkan 服务端崩溃，也不预先宣称修复已通过真实验证。

## 已观察到的失败

[native-rendered-reload-epic/summary.json](native-rendered-reload-epic/summary.json) 记录 Python 客户端退出 -11，服务端当时仍存活，没有 UE crash 文件；Town12 路线 22331 正常返回 438 tick，下一条 Town13 路线 3816 在 START 后、LOADED 前失败。之后监督器停止服务端超时并 SIGKILL，另计生命周期失败。

[native-rendered-conservative-v2/client.txt](native-rendered-conservative-v2/client.txt) 的 faulthandler 把再次出现的客户端故障定位于 `await_loaded_world()` 的 `world.get_map()`，调用位于 `client.load_world()` 返回之后。这是 Python 调用位置；它还不是 C++ 原生线程栈，不能独自证明具体内存竞态。

## CARLA 0.9.15 中的相关顺序

[Episode.cpp](https://github.com/carla-simulator/carla/blob/0.9.15/LibCarla/source/carla/client/detail/Episode.cpp) 的 streaming callback 先将新 episode state 发布到 `_state`，检测地图变化后标记 `_should_update_map`，随后调用 `OnEpisodeChanged()` → `TrafficManager::Reset()`，完成后才发布 snapshot future 并调用用户 `on_tick` callback。

[Simulator.cpp](https://github.com/carla-simulator/carla/blob/0.9.15/LibCarla/source/carla/client/detail/Simulator.cpp) 的 `LoadEpisode()` 每轮等待状态 50 ms，随后只检查 episode ID 是否改变即可返回；检查并没有显式等待 TM reset 完成。`GetCurrentMap()` 使用 `_cached_map`、`_open_drive_file` 与上述地图变化标记，方法中没有可见互斥锁。

[TrafficManagerLocal.cpp](https://github.com/carla-simulator/carla/blob/0.9.15/LibCarla/source/carla/trafficmanager/TrafficManagerLocal.cpp) 在 Reset 中停止旧 worker、换 episode/world，再调用 `SetupLocalMap()`，后者也读取 `world.GetMap()`。这使 TM 重建与应用主线程立刻 `get_map()` 并发成为有源码依据的候选。另一个待证实边界是 Stop 唤醒同步 worker 后，Run 没有立即重新检查退出标记，仍可能执行当前轮 ALSM。两者都尚缺客户端 C++ 栈，不能称为已确定根因。

锁定的 [Bench2Drive evaluator](https://github.com/Thinklab-SJTU/Bench2Drive/blob/acbeb0dd8aa99dd0e71b9b75bb542f6fe7c7f3d8/leaderboard/leaderboard/leaderboard_evaluator.py) 也保留一个 TM，逐路线 `load_world(..., reset_settings=False)`，没有在每次切图前关闭它。保留 TM 本身不是已经证实的 API 使用违规。

## 所有权与可用同步接口

[Python 绑定](https://github.com/carla-simulator/carla/blob/0.9.15/PythonAPI/carla/source/libcarla/TrafficManager.cpp) 直接暴露 `shut_down()`。它在 [TrafficManager.cpp](https://github.com/carla-simulator/carla/blob/0.9.15/LibCarla/source/carla/trafficmanager/TrafficManager.cpp) 中仅删除 wrapper 对应 `_port` 的 native 实例，持 TM 全局互斥锁，不调用全体 TM 的 Release。local 实例析构会撤销该端口注册、Stop 并 join worker；再次请求同端口会重新创建。

但是 [TrafficManagerRemote.cpp](https://github.com/carla-simulator/carla/blob/0.9.15/LibCarla/source/carla/trafficmanager/TrafficManagerRemote.cpp) 会把 ShutDown 发给连接的外部 TM。`get_trafficmanager()` 可以自动连接既有实例，Python 0.9.15 没有提供 local/remote ownership 查询。因此不能因保存了 Python wrapper 就认定有权关闭它。现有 world-port flock 不保护另一个 CARLA 实例误用同 TM 端口；若实现主动 TM 生命周期，必须增加独占端口合同、稳定锁文件，并在创建前拒绝已有监听者。端口检查与 native bind 之间仍存在不遵守锁的外部程序竞争窗口，不能宣称原子所有权证明。

`get_port()` 只返回端口，不能作 reset 栅栏。`set_synchronous_mode(True)` 会短暂经过全局锁，但主线程仍可能在 streaming callback 开始 Reset 前取得锁，因此不是严格的 ready 证明。切到 False 会唤醒 worker 进入异步模式，并不 stop/join，不应当作经过证明的安全清理修复。

本次采用范围更小的有序栅栏：在 `load_world` 前注册 `on_tick`，等待 `snapshot.id` 匹配返回的 `world.id`，再访问地图；0.9.15 的 [Snapshot 绑定](https://github.com/carla-simulator/carla/blob/0.9.15/PythonAPI/carla/source/libcarla/Snapshot.cpp) 确有 episode ID，且上述 callback 发生在 TM Reset 之后。须在 load 前注册以免错过事件、在所有退出路径移除、超时明确报错；禁止为等 callback 额外 tick 或任意 sleep。`WorldSession._load_ready_world()` 已实现此顺序，未新增 TM shutdown、端口锁或监听检查；上述所有权分析只是备选方案边界，不是已实施的行为。

还核实了 [streaming/detail/tcp/Client.cpp](https://github.com/carla-simulator/carla/blob/0.9.15/LibCarla/source/carla/streaming/detail/tcp/Client.cpp)：接收 handler 和向用户投递 `_callback` 都绑定同一 ASIO strand，因此同一 Episode stream 的后续/重复帧 callback 不能并发越过正在进行的 TM Reset。这一点是该屏障正确性的必要条件，不能只按 Python 调用先后推断。

针对 world loading、simulation 和 native validator 的 75 项无 CARLA 测试通过；新增用例验证旧/无关 episode callback 不解除等待、TM 未就绪前不得 `get_map`、RPC 返回前的通知不丢失、超时与 RPC 失败清理、仅移除本次注册的 callback，以及不重试、不额外 tick。真实对照由独占服务器的主调查流程另行记录。

## 旧对象、清理与网上修复的适用范围

项目 native adapter 终止行为树、清空可能延迟析构的 actor 列表，调用 provider 清理并由 owned registry 补充销毁；未做通配符清场。reference driver 仍可能持有旧 Map 与 planner 引用，但本机 bundled agent 没有自动 destroy 自车的析构函数，CARLA Map/Waypoint 使用 shared ownership；当前没有证据证明只是保留旧 driver 引用就形成悬空指针。`set_autopilot(False)` 能立即 unregister，普通 DestroyActor 则依赖后续 TM 生命周期扫描移除；不能在无证据时向每次销毁添加额外 tick。

上游 [PR #3288](https://github.com/carla-simulator/carla/pull/3288) 已在 2020 年修复早期版本同步 TM 切图错误，不能把旧 issue 的切 async 建议直接当成 0.9.15 的已知修复。[0.9.16 的 PR #8553](https://github.com/carla-simulator/carla/pull/8553/files) 为 LocalizationStage 的缺失 buffer 增加检查，尚未证明与本次 `get_map` SIGSEGV 同因；不能据 changelog 宣称升级必然解决。

验收需要保持同路线、顺序、画质、距离和 seed，真实重跑 Town12→Town13，分别报告客户端退出、服务端退出与关闭结果。单元测试只能验证调用顺序和失败清理，不能证明 C++ 竞态已消失。
