# 最新 Town13 服务端崩溃：Vulkan 内存绑定审查

日期：2026-09-28。只读本机二进制、安装信息和已有诊断结果，并检索上游一手资料；本次审查未启动 CARLA，未修改驱动、内核或系统 Vulkan 文件。

## 本次栈实际指向什么

[41 条路线批次](native-town13-reload-barrier-v2/summary.json) 在第 11 条 Town13 / 3816 期间服务端 SIGSEGV。它与此前 Python 客户端 `get_map()` 崩溃不同。该进程已使用 Epic、逐回合重载、新 episode 就绪屏障和 v2 配置，因此这些改动并不足以保证长批次稳定。

本次 [原始诊断](native-town13-reload-barrier-v2/crashes/crashinfo-CarlaUE4-pid-208457-201C00FCD7F94C9FB4779667FE224DEF/Diagnostics.txt) 最上层在 `libnvidia-glcore.so.580.178.04`，向下经 `libGLX_nvidia`、`FStagingManager::AcquireBuffer`、`FVulkanResourceMultiBuffer::Lock`，为静态网格 index buffer 建立 staging buffer。进一步用随发行版提供的 `.debug` 符号和可执行文件反汇编确认：

| 位置 | 只读结果 |
| --- | --- |
| UE 栈返回地址 | `0x200000 + 0x44b6fdf = 0x46b6fdf` |
| 前一条调用指令 | `0x46b6fd9: call *[0x6e54540]` |
| `.debug` 对应符号 | `VulkanDynamicAPI::vkBindBufferMemory` 位于 `0x6e54540` |
| DWARF inline 定位 | `FVulkanAllocation::BindBuffer`，`VulkanMemory.cpp:3805`；外层栈显示 `AcquireBuffer:4418` |
| NVIDIA 首帧 `+0xe52033` | `mov 0xa0(%r14), %r14`，普通 64 位内存读取 |

因此下一轮应检查 **VkBuffer 与 VkDeviceMemory 绑定的有效性和生命周期**。这不是已证明的 shader 编译错误；驱动顶层的故障指令也不是 SIMD 对齐加载。UE 的“unaligned memory access (SIMD vectors?)”说明不足以证明 NumPy、CPU SIMD 或 VkDeviceSize 的某一种对齐错误。没有寄存器/完整原生 core 时，不能确定 `%r14` 来自失效句柄、引擎内存破坏还是驱动内部错误。

以上检查可以用 `objdump -d` 指定上述地址，以及 `nm -C CarlaUE4-Linux-Shipping.debug`、`addr2line -f -C -e CarlaUE4-Linux-Shipping.debug 0x46b6fdf` 重复。地址仅适用于已记录 SHA-256 的本机发行二进制及同版本驱动；不适用于不同版本。

## 版本与公开证据

只读核查时，本机 RTX 5060 Ti 的显存为 16,311 MiB；`nvidia-smi`、`modinfo` 和 NVIDIA GL 用户态包均为 580.178.04，内核模块许可为 Dual MIT/GPL，即 open 版本。内核为 `7.0.0-34-generic`，系统 Vulkan loader 包为 `1.3.275.0-1build1`。NVIDIA ICD JSON 指向 `libGLX_nvidia.so.0`；所以 Vulkan 栈经过该名字的库是本机预期加载路径，不能仅凭名称误判为意外使用 OpenGL。

| 一手资料 | 可以得出的结论 | 不能得出的结论 |
| --- | --- | --- |
| [580.178.04 支持 GPU 清单](https://download.nvidia.com/XFree86/Linux-x86_64/580.178.04/README/supportedchips.html)、[open 内核模块说明](https://download.nvidia.com/XFree86/Linux-x86_64/580.178.04/README/kernel_open.html) | 明确包含 RTX 5060 Ti；Blackwell 仅支持 open 内核模块，本机模块类型符合要求 | 不证明 UE4.26 每种旧渲染路径都已验证；不能照搬旧帖切 proprietary 或回退到不支持该卡的 530/535 |
| [CARLA #6312](https://github.com/carla-simulator/carla/issues/6312) | 2023 年 CARLA 0.9.14 / UE4.26 的 Town01→Town05 转换已出现 `AcquireBuffer:4418 → Lock:272 → index buffer` 同类链 | 故障地址与本次驱动顶层不同，帖子没有已确认修复；它反而说明这一引擎路径的崩溃并非 Blackwell 发布后才首次出现 |
| [NVIDIA 显示驱动官方版本记录](https://www.nvidia.com/Download/processFind.aspx?ctk=0&lang=en-us&lid=1&osid=12&pfid=933&psid=120&qnfslb=01) | 580.173.02 曾修复多个线程等待同一个 Vulkan semaphore 的延迟问题；580.178.04 的显示驱动摘要只列常规改进 | 前者针对卡顿/性能，不等于修复本次 `vkBindBufferMemory` SIGSEGV；当前版本已高于该修复版本 |
| [NVIDIA Vulkan 开发版发布记录](https://developer.nvidia.com/vulkan-driver) | 后续驱动仍有具体 Vulkan 修复；开发版可能缺少通用发行版的其他修复 | 当前检索没有发现与本次调用链匹配、已确认修复的 580→595/610 条目；不能保证升级解决问题 |

其他找到的 580 用户报告主要涉及 GTK/EGL teardown、视频/SPIR-V 编译、swapchain 或满显存故障，与本次内存绑定调用不同，未作为修复依据。本次批次采样 GPU 峰值为 8,367 MiB，RSS 约 9.13 GiB；这些数据不支持直接认定 16 GiB 显存耗尽，但采样也不是每个 Vulkan heap、每次瞬时分配都成功的证明。

## 下一项最有价值的诊断

优先使用 **Khronos CPU API validation layer**，保持当前驱动、场景序列及其他参数不变。Vulkan 驱动不承担完整参数校验；非法用法可能在任意驱动内部位置崩溃。[Khronos 验证说明](https://docs.vulkan.org/guide/latest/validation_overview.html) [vkBindBufferMemory 约束](https://docs.vulkan.org/refpages/latest/refpages/source/vkBindBufferMemory.html)明确要求有效对象、匹配设备、合法内存类型、偏移/容量/对齐和外部同步。层日志能提供具体 VUID 与对象信息，比继续猜测 CVar 更直接。

初始只读检查时系统没有 `vulkan-tools` 或 `VK_LAYER_KHRONOS_validation`。主调查随后已将 Ubuntu noble `vulkan-validationlayers 1.3.275.0-1` 解包到临时用户目录，通过进程级 `VK_LAYER_PATH` / `VK_INSTANCE_LAYERS` 启用，见 [隔离配置](vulkan-validation-profile.json)。没有系统安装、没有驱动变更。先只启用 CPU API 校验，不同时加入 GPU-assisted validation 或新的引擎参数；记录加载成功证据、完整 VUID 和故障前对象顺序。该工具会改变性能和线程时序，不能将它的耗时当正常仿真性能。

单独 3816 的一轮若通过，并不能排除前十条回合造成的状态积累。之后最有价值的是保留原顺序的前 11 条，在相同 seed、重载策略和 tick 预算下采集 validation 日志，而不是只重复新进程单路线。若首先出现对象失效、重复绑定、越界或未同步访问，应先定位 UE 对应资源生命周期；若相同崩溃持续但没有校验错误，也不能据此证明驱动必错，需要故障现场原生线程/寄存器和最小复现。

更换驱动只作为之后的独立 A/B：选择明确支持 RTX 5060 Ti 的完整 open 驱动与匹配用户态组件，保留旧环境、固定同一复现输入，再测运行和正常退出。不能只用 `LD_LIBRARY_PATH` 混入另一版本的 NVIDIA GL/Vulkan 用户态库，也不能同时换驱动、内核和场景设置后把差异归因一个因素。当前没有执行这类系统修改，也没有获得一个已验证可修复本次问题的驱动版本。
