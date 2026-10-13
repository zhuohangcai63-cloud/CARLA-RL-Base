# Vulkan validation：实际错误及诊断边界

日期：2026-09-28。本文只读取已有运行材料、本机 Shipping 二进制及 Khronos/Epic 一手资料，没有启动仿真、修改驱动或改写进程内存。初步审阅后已补入 11 条顺序实验的失败结果。

## 验证层确实已启用，但短测试不等于修复

[单条 3816 的 server.txt](native-town13-vulkan-validation/server.txt) 第 367、387 行分别记录 Khronos instance/device validation layer 被装入。使用 Ubuntu noble 的 `vulkan-validationlayers 1.3.275.0-1`，从 `/tmp` 解包并仅向诊断进程传入配置；包、动态库摘要及完整 layer manifest 见 [vulkan-validation-profile.json](vulkan-validation-profile.json)。没有系统安装或驱动替换，没有启用 GPU-assisted validation。

[单条运行结果](native-town13-vulkan-validation/summary.json) 中客户端正常返回，路线 3816 执行 1000 tick 后达到预算；这不是驾驶任务完成。服务退出仍需 SIGKILL，`shutdown_failed=true`、`passed=false`。开启验证层后的短运行没有复现之前的 native 崩溃，不能据此宣布运行稳定。

验证层增加 CPU 检查和对象追踪，会改变调用开销、内存布局与线程时序。Khronos 明确把它作为开发诊断机制，并说明违反 Vulkan valid usage 可产生未定义行为；验证层没有报某类错误也不能证明引擎/驱动内部完全正确。[官方验证概述](https://docs.vulkan.org/guide/latest/validation_overview.html)

## 单条 3816：启动阶段已出现的错误

完整单条日志中有 **13 条 ERROR、6 条 WARN，分属 5 种 VUID 和 2 种 warning ID**。计数按消息首行统计，不重复计算同一行中的 VUID 和规范链接。配置把同类重复消息限制为 3 条；表中 3 条多数已经封顶，不能当作真实发生次数或路线间频率。

| 消息及日志行 | 记录数 | 实际诊断 | 与 buffer memory binding 的关系 |
| --- | ---: | --- | --- |
| `VUID-vkCreateDevice-ppEnabledExtensionNames-01387`，402 | 1 | 开启 `VK_KHR_swapchain` 时缺少其 `VK_KHR_surface` 依赖 | 设备/显示扩展声明问题，没有指出 buffer 或 memory 句柄损坏 |
| `VUID-vkCmdWriteTimestamp-None-00830`，406、416、444 | 3 | query pool 中的 timestamp query 使用前未 reset | GPU 时间戳查询生命周期问题，不是内存绑定 API |
| `VUID-VkImageCreateInfo-flags-04738`，410–414 | 3 | image format list 包含两个格式，但 image flags 未包含 mutable format | 图像创建参数问题，不能直接等同于 VkBuffer 子分配故障 |
| `VUID-VkShaderModuleCreateInfo-pCode-08742`，420–424 | 3 | compute SPIR-V 声明 `SPV_GOOGLE_hlsl_functionality1` / `SPV_GOOGLE_user_type`，但对应 Vulkan 要求未满足 | shader 扩展声明与设备能力启用不一致 |
| `Undefined-Value-ShaderOutputNotConsumed`，426–432 | 3 | fragment shader 的输出 location 0 没有匹配 attachment | 管线接口警告；不是 buffer 生命周期证明 |
| `Undefined-Value-ShaderInputNotProduced`，435–441 | 3 | attachment 1/2/3 没有被 fragment shader 写入 | 管线输出可能有未定义值；不是 binding 错误 |
| `VUID-RuntimeSpirv-OpImageWrite-07112`，448–460 | 3 | shader 向四分量 image format 写入三分量 texel | shader/image write 有效性问题 |

这些消息集中在 UE 启动输出之后。现场观察表明多数在默认 Town10HD 启动阶段即已出现；Shipping server 日志没有逐条时间戳或地图阶段标记，因此仅凭这个文件不能给每条消息精确关联到 Town13 的加载帧。不能把它们全部称为“Town13 特有错误”，也不能以早已出现为由忽略有效性问题。

Khronos 规范分别确认 [swapchain 的 surface 依赖](https://docs.vulkan.org/refpages/latest/refpages/source/VK_KHR_swapchain.html)、[timestamp query 的使用要求](https://docs.vulkan.org/refpages/latest/refpages/source/vkCmdWriteTimestamp.html)、[image mutable-format 限制](https://docs.vulkan.org/refpages/latest/refpages/source/VkImageCreateInfo.html)、[SPIR-V 扩展要求](https://docs.vulkan.org/refpages/latest/refpages/source/VkShaderModuleCreateInfo.html) 和 [OpImageWrite 分量规则](https://docs.vulkan.org/spec/latest/appendices/spirvenv.html)。其中 `VK_KHR_surface` 是 instance extension，不应按日志的泛化措辞把它错误地塞入 device extension 列表。要修复这些调用需定位引擎渲染后端/着色器生成路径，环境 Python 层不能靠吞掉日志修复。

## 顺序运行新增：buffer 尾部越界

2026-09-28 05:22:45 UTC 首次读取 [11 条顺序运行日志](native-town13-validation-sequence11/server.txt) 时，除上述同类启动消息外，已经出现两个新 VUID。随后该实验也发生了 native 崩溃，具体结果如下节。

| 消息 | 记录数 | 精确边界计算 |
| --- | ---: | --- |
| `VUID-vkCmdCopyBuffer-size-00116`，468 | 1 | `2192704 + 252 = 2192956`，超过 `dstBuffer.size=2192952` **4 字节** |
| 同上，472 | 1 | `1634288 + 576 = 1634864`，超过 `1634856` **8 字节** |
| 同上，476 | 1 | `1081216 + 3864 = 1085080`，超过 `1085076` **4 字节** |
| `VUID-vkCmdDrawIndexed-robustBufferAccess2-07825`，481、486、491 | 3 | `1331840 + 2 × (0 + 24) = 1331888`，超过 index buffer size `1331880` **8 字节**；三条指向同一 buffer/pipeline |

这比启动 shader 警告更接近网格上传/绘制资源路径，是需要保留的具体证据。[vkCmdCopyBuffer 规范](https://docs.vulkan.org/refpages/latest/refpages/source/vkCmdCopyBuffer.html) 要求目标偏移加复制长度不超过目标 buffer 的声明长度。以上是 **VkBuffer 逻辑边界越界**，即使底层 VkDeviceMemory 分配留有额外空间也不能把它标为合法。当前日志不包含每次 `vkCreateBuffer`、绑定和子分配历史，尚不能确定是资源长度计算、重用、句柄生命周期还是其它内存损坏。

三次 copy 的目标偏移以及 index binding offset 都是 16 字节整数倍，相关 buffer 总长却不是 16 字节整数倍；超出量仅 4/8 字节。它支持检查“对齐之后的 offset 与剩余容量计算/子分配末尾”这一具体假设。它**不证明** Vulkan 必须要求这些 buffer 总长 16 字节对齐，也不能仅凭该模式认定是哪行引擎代码有错。DrawIndexed 的错误还说明问题不限于一次 copy 的长度取整。

进一步只读反汇编发现了与该模式直接相关的边界缺口：

1. `AllocateBufferPooled` 在 `0x46b48e8` 把池大小/请求长度的较大值写入 `VkBufferCreateInfo.size`，在 `0x46b4913` 调用 `vkCreateBuffer`。
2. `0x46b4947` 调用 `vkGetBufferMemoryRequirements`；`0x46b49dc` 用返回的 `requirements.size` 创建物理 device memory。物理分配大小可以包含不属于逻辑 buffer 的尾部空间。
3. 随后的 `FVulkanSubresourceAllocator` 构造函数在 `0x46b43b9` 读取 `FDeviceMemoryAllocation.Size`，在 `0x46b43bc` 写入 `MaxSize`，并在 `0x46b4403/4407` 初始化 `FreeList={offset:0,size:MemoryAllocation.Size}`，没有限制为刚才的 `VkBufferCreateInfo.size`。
4. `TryAllocate2` 的 `0x46b23e0–0x46b23f8` **已经**把 offset 对齐产生的前置 padding 计入所需容量。因此现有证据更指向自由区容量的定义，而非这个函数简单漏算了前置 padding。

离线 `gdb -nx -nh -batch -iex 'set auto-load off'` 仅载入 `.debug` 后执行 `ptype /o`，确认 `FDeviceMemoryAllocation` 的 offset 0 是 `VkDeviceSize Size`，`FVulkanSubresourceAllocator` 的 offset 72 是 `MaxSize`，offset 144 是 `FreeList`；没有连接或启动进程。DWARF 分别定位到 `VulkanMemory.cpp:2788/2801/2832`（create/allocate/构造调用）、构造函数 `2631`（读取 GetSize）、`2644`（追加 FreeList）及 `TryAllocate2:3887`。

由此可以提出一个**有具体二进制证据的源码修复目标**：buffer 子分配器的可用逻辑范围必须限制到 `VkBufferCreateInfo.size`，不能把物理分配的尾部 padding 当作可读写 buffer 范围。需要在对应 UE 源码中复核所有 buffer 构造/重用路径，补“memory requirements 大于逻辑 buffer 长度”的回归，再重新构建验证。现有调用没有保留每个违规 buffer 的创建 trace，故仍不能声称已证明这正是所有日志越界或驱动 SIGSEGV 的唯一原因；也不能通过 Python 配置直接修正 C++ 边界。

## 11 条顺序实验最终结果：仍然失败

[监督结果](native-town13-validation-sequence11/summary.json) 于 `2026-09-28T05:30:21.088923+00:00` 结束：`status=server_exited_during_run`、`server_exit=-11`、`passed=false`。客户端因服务已崩溃由监督程序发 SIGTERM 收尾（`client_exit=-15`），不能误记为客户端自己又发生了 SIGSEGV，也不能记作正常主动停止服务失败。

[原始 probe](native-town13-validation-sequence11/probe.json) 保留了前 **10 条正常返回，共 7847 个决策推进 tick**：4 条 `completed`、6 条 `tick_budget_reached`。这描述验证脚本的执行结果，不代表全部驾驶评价通过。第 11 条 `3816 / HighwayCutIn` 的持久记录仍是 `running, ticks=0`；客户端日志已有 `LOADED`，没有 `PREPARED` 或返回结果，因此应标为**初始化期间结果不确定/失败**，不能用这个未完成记录推断崩溃前没有初始化物理 tick。进程中断使 probe 顶层仍保留 `status=running`，判断实验结束应结合已完成的监督结果。

[崩溃栈](native-town13-validation-sequence11/crashes/crashinfo-CarlaUE4-pid-234132-DC7A3521B2484DB588431A6B7EDD5D25/Diagnostics.txt) 报告写地址 `0x0` 的 SIGSEGV，路径为 libc → `FVulkanResourceMultiBuffer` 构造函数（`VulkanIndexBuffer.cpp:136`，顶点上传 memcpy 路径）→ `RHICreateVertexBuffer` → `FPositionVertexBuffer::InitRHI` → RenderThread。它与上一次 NVIDIA 用户态驱动中的 `vkBindBufferMemory` 栈**不是同一个故障点**。

因此，验证层没有消除长序列崩溃；buffer 越界和容量缺口是值得修复的独立证据，但当前没有寄存器、分配追踪或源码修复前后对照把它连接成“此前越界导致此次空地址 memcpy”的完整因果链。不可将 buffer VUID 直接认定为全部 native 崩溃的唯一根因。后续 Binning=1 单变量实验应分别报告 VUID、运行崩溃和退出状态，不混合为一个通过指标。

## 与此前 vkBindBufferMemory 崩溃的证据关系

此前 [driver-bind-buffer-audit.md](driver-bind-buffer-audit.md) 已将实际崩溃调用点定位为 `FVulkanAllocation::BindBuffer` → `vkBindBufferMemory` → NVIDIA 用户态驱动。当前已结束的单条 3816 日志没有 `vkBindBufferMemory`、`vkFreeMemory`、`vkDestroyBuffer`、无效 buffer/memory handle 或 threading 诊断；顺序运行的上述新增消息也不是 binding VUID。

[vkBindBufferMemory 的规范](https://docs.vulkan.org/refpages/latest/refpages/source/vkBindBufferMemory.html) 涉及有效句柄、设备归属、memory type、offset/容量以及 host 同步。未看到这类消息只说明现有日志没有捕获相应违反，**不排除**未覆盖路径、时序改变后未复现的错误、驱动内部 bug 或此前越界产生的间接影响。反过来，copy/index 越界也是独立错误证据，但尚未证明它导致了之前驱动栈中的 SIGSEGV。

## 二进制支持的一次单变量诊断

对已记录 SHA256 `1da989e4ff136e0658fdea421bc1da2b0e5cb018c6e767efc4f6129c21ab1a81` 的 `CarlaUE4-Linux-Shipping` 做只读核对；该 ELF 为 `ET_EXEC`，以下地址只适用于这个二进制。使用 `.debug` 符号确定名称、Shipping 中的真实注册/读取指令确认，不依据 stripped 反汇编显示的邻近大偏移符号名推断功能。

| CVar | 静态默认 / 读取方式 | 注册与作用证据 |
| --- | --- | --- |
| `r.Vulkan.UseBufferBinning` | **0**；直接 `int32 @ 0x6e552a8`，不是指针 | `GVulkanUseBufferBinning` 位于零初始化 `.bss`；注册 `0x46db253`，flags `0x4`；`AllocateBufferPooled` 在 `0x46b4666` 读取。非零时把不超过 8192 字节的请求分到 128、256、512、1024、2048、8192 字节的档位，并使用不同池类别。 |
| `r.Vulkan.SingleAllocationPerResource` | **0**；直接 `int32 @ 0x6e55328`，不是指针 | `GVulkanSingleAllocationPerResource` 位于 `.bss`；注册 `0x46db469`，flags `0x20`；`FVulkanResourceHeap::AllocateResource` 在 `0x46b2610` 读取，`0x46b26f7` 跳过现有共享分配的复用分支，进入新 device memory 分配。没有证据它同时禁用了 `AllocateBufferPooled` 的全部逻辑子分配。 |

内嵌帮助明确 binning 会用更高内存高水位换取较少碎片，并标为 read-only；必须通过启动时隔离 `Engine.ini` 设置并读回，而不是运行期 console。bin size 表在 `0x11ff0f0`（文件偏移 `0xfff0f0`），前六个 little-endian uint32 为上述档位。二进制静态默认仍需用监督父进程只读实际值验证，不能把配置文件中的请求值当作生效证据。

**当前较有针对性的下一次对照是只增加 `r.Vulkan.UseBufferBinning=1`**，其余 v2 配置、验证层、路线顺序、seed、reload、tick 预算保持一致。主要观察两种新增 buffer 越界 VUID 是否仍出现、是否仍有 native 崩溃，以及内存高水位。它改变布局和复用时序，可能只是掩盖错误；即使短测试无 VUID，也不能直接发布为根因修复。不应同时启用 SingleAllocationPerResource 后归因其中某一项。

若之后需要区分 device-memory 共享池与逻辑 buffer 子分配，SingleAllocationPerResource=1 可以作为另一独立诊断，但可能显著增加分配数和开销。Epic 的 [一手支持答复](https://forums.unrealengine.com/t/ue-5-5-uses-excessive-memory-with-vulkan-backend-with-adreno-663-automotive-gpu/2737228/4) 确认此开关用于绕开 driver allocation 的内部 bucket；该答复针对 UE 5.5/Android 内存使用，不能当作本机 UE 4.26/Linux 越界的已知修复。

复核命令示例（均只读）：

```bash
nm -C /home/czh/software/carla/CarlaUE4/Binaries/Linux/CarlaUE4-Linux-Shipping.debug | rg 'GVulkan(SingleAllocationPerResource|UseBufferBinning)'
strings -t x -e l /home/czh/software/carla/CarlaUE4/Binaries/Linux/CarlaUE4-Linux-Shipping | rg -A 1 'r.Vulkan.UseBufferBinning|r.Vulkan.SingleAllocationPerResource'
objdump -d --start-address=0x46b45b0 --stop-address=0x46b47a0 /home/czh/software/carla/CarlaUE4/Binaries/Linux/CarlaUE4-Linux-Shipping
objdump -d --start-address=0x46b25d0 --stop-address=0x46b28a0 /home/czh/software/carla/CarlaUE4/Binaries/Linux/CarlaUE4-Linux-Shipping
```

下一轮应继续保留所有原始 VUID，不能把启动错误过滤后把报告写成“验证层通过”。同类消息封顶也意味着不能根据末尾没有重复输出推断错误已经停止。


## Binning 单变量对照最终结果

[native-town13-binning-validation11](native-town13-binning-validation11/outcome.json) 已结束并失败：前 5 条返回（3 条 completed、2 条 tick_budget_reached），共 3,366 个 step tick；第 6 条 29893 在 LOADED 后、PREPARED 前 SIGSEGV，后 5 条未运行。只读采样确认 UseBufferBinning=1、SingleAllocationPerResource=0，其余 v2 参数不变。

日志再次出现 copy/draw 边界错误；复制请求变为 9,840 / 9,216 字节，超出逻辑 buffer 4 / 8 字节。它们已超过 8,192 字节分档范围，因此小 buffer 分档不能完整避开此类边界问题。崩溃栈为 libc memcpy → FVulkanResourceMultiBuffer ctor:136 → FStaticMeshVertexBuffer TexCoord/InitRHI，写地址 0。越界和 native 崩溃仍不能据此归为唯一因果链。此开关未通过验证，不推荐为稳定性修复。

本次原生进程在运行期间先崩溃，退出超时抓栈钩子没有触发；没有取得原生退出挂起栈。新目标进程仅授权其监督父进程调试，没有改全局 Yama 或既有进程；运行期没有调试器附加，详见本批 ptrace-launch.json 与 shutdown-debug-setup.json。
