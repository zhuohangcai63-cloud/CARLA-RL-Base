# Town13 长批次 Vulkan 崩溃：只读分析

日期：2026-09-28。本文只分析现有崩溃材料、本机文件和官方资料；没有启动 CARLA、修改系统、更新驱动/BIOS、附加调试器或改写进程内存。真实对照运行由独立监督程序执行，结果以相应运行目录为准。

## 已确定的范围

`native-town13-default-rhi/crash/Diagnostics.txt` 记录了 SIGSEGV，非法写地址为 `0x00000000e57d9000`。栈从 libc 返回到 `FVulkanResourceMultiBuffer`，再经过静态网格切线顶点缓冲初始化和 RenderThread。`CrashContext.runtime-xml` 的引擎版本是 UE 4.26.2。

对该 Shipping 二进制只读反汇编，在 `0x469e906` 找到 `memcpy@plt` 调用，返回地址 `0x469e90b` 与崩溃记录对应；其目的地址来自前一次缓冲锁定调用，源地址来自资源数据，长度由寄存器传入。可以定位到 **CPU 在 Vulkan 网格上传路径中复制内存时发生非法写**，尚不能区分无效目的缓冲、长度损坏、生命周期/重用错误、驱动映射或硬件错误。没有寄存器和完整 core，不能仅凭小于 4 GiB 的故障地址断言发生了指针截断。

批次在前 38 条路线返回后，准备第 39 条 `31736 / VehicleOpensDoorTwoWays` 时失败；同路线新进程运行能够返回，支持继续检查累积资源状态和时序，而非把这条路线直接判为无法加载。批次结果不是整组通过。崩溃材料中 `bIsOOM=0`，当时可用物理内存约 17.5 GiB；本轮现有采样没有支持“系统 RAM 耗尽”的证据。这不排除特定内存池或映射的错误。

## 本机二进制中的 CVar 默认值与地址

完整证据见 [binary-cvar-readonly.json](binary-cvar-readonly.json)，包含 ELF 段、字节、注册和使用指令以及内嵌帮助文字。被检查文件：

```text
/home/czh/software/carla/CarlaUE4/Binaries/Linux/CarlaUE4-Linux-Shipping
sha256 1da989e4ff136e0658fdea421bc1da2b0e5cb018c6e767efc4f6129c21ab1a81
ELF64 little endian x86_64, ET_EXEC（非 PIE）
entry 0x26fa000
```

| 项目 | 二进制默认值 | 已核对的存储位置 | 含义和限制 |
| --- | --- | --- | --- |
| `r.Vulkan.EnableDefrag` | **1** | `int32 @ 0x6c24924`，文件偏移 `0x6a21924` 为 `01 00 00 00` | 禁用它是实际改变。注册指令和分支读都指向该地址；帮助文字包含移动和驱逐 |
| `r.Vulkan.WaitForIdleOnSubmit` | **0** | `int32 @ 0x6e55620`，零初始化 `.bss` | 帮助文字标明 0 为默认；1 每次提交等待 GPU idle；2 只等 upload；3 只等含图形命令的 active buffer |
| `r.Vulkan.RHIThread` | **1** | 数据指针槽 `uint64 @ 0x6e55ae8`；console 对象指针槽 `0x6e55ae0` | 先读数据指针 `p`，再读 `int32 @ p` 和 `p+4` 两个线程副本；不可把指针槽直接当整数值 |

`-norhithread` 是引擎全局线程启动参数，可能不修改 `r.Vulkan.RHIThread` 这个 Vulkan CVar。因此该值仍为 1 不能独立证明参数失效；需要同时检查 `/proc/PID/task/*/comm` 是否实际存在 RHIThread。RenderThread 与 RHIThread 不同，本次崩溃发生于 RenderThread，禁用 RHIThread 只是时序对照，不能预先保证奏效。

ELF 可装载段如下；地址均为十六进制。固定主映像地址不代表堆、共享库或 CVar 数据对象没有 ASLR。

| 文件偏移 | 虚拟地址 | 文件长度 | 内存长度 | 权限 |
| --- | --- | --- | --- | --- |
| `0` | `0x200000` | `0x24f8fdc` | `0x24f8fdc` | R |
| `0x24f9000` | `0x26fa000` | `0x4494730` | `0x4494730` | RX |
| `0x698d730` | `0x6b8f730` | `0x8d668` | `0x8dbf1` | RW |
| `0x6a1b340` | `0x6c1e340` | `0x16dd0` | `0x339a48` | RW |

复核过程为：`readelf -h/-lW` 检查 ELF → 找 UTF-16LE CVar 名 → 按所在 PT_LOAD 将文件偏移映射到虚拟地址 → 对名称地址查找注册代码 → 确认初始化参数和存储地址 → 查找实际分支读取 → 读取文件中的初值或检查 `.bss` 与帮助默认值。`objdump` 对 stripped Shipping 程序显示的邻近导出函数名加巨大偏移不是可靠的局部函数名；证据使用指令地址、操作数、字符串和崩溃符号，不据这些邻近名称推断函数职责。

当前安装的 Engine/Carla 配置中未找到 `EnableDefrag` 或 `WaitForIdleOnSubmit` 覆盖。**静态默认值仍不能代替运行时读回**：配置、启动参数和 console 命令可能改变值。

监督程序若读取自己的 CARLA 子进程，可先核对 `/proc/PID/exe` 摘要、启动标识和 `/proc/PID/maps`，再以 `os.open(..., os.O_RDONLY)` 和 `os.pread` 读取上述整型；指针解引用前检查非零、长度和可读映射。地址只适用于上述摘要的程序。读取失败应记录不可用，不能写内存、暂停、附加 debugger 或自行提升权限。不要把不同采样时刻的多个值视为原子快照。首次静态初始化完成前，RHIThread 数据指针可能为零。

## 已有上游讨论

CARLA 官方仓库 [issue #6312](https://github.com/carla-simulator/carla/issues/6312) 于 2023 年报告 CARLA 0.9.14 切换 Town01/05/04 时在 Vulkan staging acquire/lock、资源缓冲构造或图像绑定中崩溃。它与本次同属 Vulkan 资源初始化路径，但故障位置和访问类型不同，没有确认的修复方案。这说明相邻类别的故障早于 RTX 50 系显卡和 580 驱动，也不只发生在 Town12/13；不能据此认定是相同 bug。

本轮查阅 NVIDIA 580.178.04 的官方 README、Vulkan 驱动说明和相关一手问题，**未找到与此精确 UE 4.26 `memcpy` 栈对应且确认适用于本机的修复版本**。官方 [supportedchips](https://download.nvidia.com/XFree86/Linux-x86_64/580.178.04/README/supportedchips.html) 包含 RTX 5060 Ti。[内核模块说明](https://download.nvidia.com/XFree86/Linux-x86_64/580.178.04/README/kernel_open.html) 明确 Blackwell 需要 open kernel modules；不能套用旧显卡帖子，改成旧版闭源模块或 530/535 驱动。Vulkan beta 580.94 系与生产 580.178.04 也不能只按版本号或不同问题的修复说明直接等同。未找到匹配修复不等于证明不存在驱动缺陷。

## Intel 14 代 CPU 假设核对

只读采样见 [hardware-readonly.json](hardware-readonly.json)：i5-14600KF，当前 microcode `0x133`；本次启动日志明确记录从 `0x12f` early update。BIOS `H7.7G`，日期 `05/14/2025`，主板 DMI 为 Maxsun `MS-Terminator B760M D4`。当前可读启动内核日志没有 MCE、Machine Check、Hardware Error、NVRM Xid 或 OOM 的匹配行；这只是有范围的负面证据，不能排除硬件问题。

Intel 的 [Vmin 指导](https://www.intel.com/content/www/us/en/support/articles/000102331/processors.html) 要求相关桌面平台安装包含 0x12F 或更高微码的 BIOS，并使用 Intel Default Settings。当前微码和 BIOS 启动微码已经达到这个门槛，**没有证据把本次崩溃归因于未安装 Vmin 缓解**。未读取 BIOS 功耗/电压配置，也不能从微码版本排除过去损伤。主板官网 [相关 B760M D4 系列页面](https://www.maxsun.com.cn/2022/1226/5864.html) 的 H7.7G 发布说明提到 0x12F，但型号后缀有变体，不能凭 DMI 泛称指定刷写文件。

若后续有跨应用内存错误、MCE 或同工作负载跨主机差异，再提高 CPU/内存调查优先级。当前不应为单一引擎栈直接更换 CPU、降电压或更新 BIOS，也不在仿真对照运行期间另开压力测试。

## 单变量诊断的优先顺序

1. 保留相同路线顺序、时钟、天气、流送距离和任务配置，比较完整长批次。新进程单独通过失败路线只能作为定位证据，不能替代原复现条件验收。
2. 独立对照 `-norhithread` 与 `r.Vulkan.WaitForIdleOnSubmit=1`，验证参数实际生效。前者改变线程执行，后者显著序列化 GPU 提交，均可能降低吞吐；一次成功仅提高时序/生命周期假设的可信度。
3. 独立对照 `r.Vulkan.EnableDefrag=0`。本机默认确为 1，故该对照有意义；同时比较内存占用和完整路线结果，不能把它当作已证实的内存修复。不要同时叠加三个选项后归因某一个。
4. 若仍复现，保留完整 core、寄存器、映射、二进制摘要和命令行；优先确定 `memcpy` 的源/目的/长度及对应分配是否有效，再考虑 UE Vulkan 源码级修复。当前 stack 文本不足以做该归因。
5. 只有在以上证据基础上，再安排支持 Blackwell 的独立驱动/内核对照或另一台机器复现；系统改动应与场景、引擎选项对照分开，不能随意指定“升级/降级必好”的版本。

这里的流送中心和距离优化已经有性能意义，但仍不能将其等同于已根治 C++ 崩溃；减少场景覆盖或定期重启也不构成相同条件下长时稳定性的证明。

## 补充：连续 RGB 测试中的不同崩溃

`rgb-all-every-frame-default/crashes/crashinfo-CarlaUE4-pid-96396-6FB392132819479CB929B036282E9E3D/` 记录另一次故障：Town13、Town12 各 100 个有效 RGB 帧完成后，第 3 张 Town03 的 `post_load_settings` 阶段失败，运行时间约 52 秒。这次是 `FVulkanVertexInputStateInfo::Generate` → `CreateGfxEntry` → `FCompilePipelineStateTask`，发生于 TaskGraph worker，不能与先前的网格上传 `memcpy` 故障合并为一个已确认的 bug。

故障 PC `0x46f6ca6` 对应 `movslq 0x94(%rsi),%rax`，报告读地址恰为 `0x94`。结合函数签名和 Linux x86_64 调用约定，证据支持进入 `Generate` 时 `FVulkanVertexDeclaration*` 参数为 null。调用点 `0x46c47b5` 从上层结构中取得该指针。**null 是如何产生的仍未知**：未初始化、被清空、生命周期竞态或此前内存损坏都需要进一步证据；后台编译栈本身不证明 use-after-free。

本机确实存在 `r.AsyncPipelineCompile`，默认 1，注册地址 `0x47b1e52`，数据指针槽 `0x6e5e400`，对象指针槽 `0x6e5e3f8`。同样先读 `uint64 p` 再读 `p`/`p+4` 的 int32，证据已加入 JSON。其内嵌帮助明确 0 为请求时同步创建 PSO、1 为异步创建；注册 flags 为 `0x24`。Epic [CVar flags 文档](https://dev.epicgames.com/documentation/unreal-engine/API/Runtime/Core/EConsoleVariableFlags) 将 `0x4` 定义为 console 只读、`0x20` 为渲染线程副本，因此应通过初始化配置进行对照并实际读回，不能假设 console 命令能够更改它。

公开资料有相邻问题，但没有已验证的同栈修复：

- Epic [UE-86145](https://issues.unrealengine.com/issue/UE-86145) 是 UE 4.23–4.25 Vulkan+Forward Shading+MSAA 下的异步 PSO 崩溃，目标修复为 4.26；触发条件和栈顶不同，不能据此给 CARLA 4.26.2 套旧补丁。
- Epic [UE-201261](https://issues.unrealengine.com/issue/UE-201261) 记录 `r.AsyncPipelineCompile=0` 可避开一个 Shipping PSO hang，但影响的是 UE 5.3、目标修复为 5.5，不能认作本次根因。
- [UE 5.7 Linux Vulkan 讨论](https://forums.unrealengine.com/t/ue-5-7-x-linux-vk-error-device-lost-in-vulkan-rhi/2699412) 中，一手复现者给出 validation layer 关于已销毁 pipeline 仍被提交的消息，并报告 LRU/WaitIdle 配置改善；其他参与者报告无效。该帖涉及 UE 5.7、`VK_ERROR_DEVICE_LOST` 和不同故障链，其源码分析还注明借助 AI、补丁未亲测。它可提供生命周期诊断思路，不能视为 Epic 对本机 UE 4.26 空顶点声明的确认。

对新栈，单独禁用异步 PSO 比直接断言 defrag 原因更有针对性；仍需相同 24 回合输入、参数读回、RGB 每帧检查以及初始化/退出分别统计。成功一次只能形成候选缓解方案。

## 补充：Shipping 参数必须实测生效

监督进程已观察到：命令行虽然包含 `-ini:Engine:[SystemSettings]:r.Vulkan.EnableDefrag=0`，但进入 Town13 后只读内存仍为 1。这样的运行应标为**覆盖未生效**，不得命名为已执行 defrag 关闭对照或据它排除假设。

对二进制做大小写原样的 UTF-8/UTF-16LE 搜索，没有 `ExecCmds`/`ExecCmds=` 或 `XDG_CONFIG_HOME` 字面量；存在 console 注册并不等价于 Shipping 保留启动 exec 入口。缺少字面量不能单独证明所有可变拼接路径不存在，但不支持花费完整批次去假定这些入口有效。随后监督进程实际检查 `-ExecCmds` 的运行值，`EnableDefrag` 仍为 1，也应记录覆盖未生效。

本机 `UserDir=` 字面量位于 `0xbb4300`，在 `0x33530e5` 传入命令行解析函数，后续代码检查和规范化绝对路径；`SaveToUserDir` 也有实际解析调用。更合理的隔离尝试是 `-UserDir=/绝对/诊断目录`，从独立的 `Saved/Config/LinuxNoEditor/Engine.ini` 提供 `[SystemSettings]` 或 `[ConsoleVariables]` 单变量，并先验证生成文件位置与内存实际值。Epic 的 [UserDir 支持讨论](https://forums.unrealengine.com/t/how-to-point-saved-directory-on-pc-to-localappdata/2689924) 和 [UE 4.27 CVar 配置说明](https://dev.epicgames.com/documentation/en-us/unreal-engine/console-variables-in-cplusplus?application_version=4.27) 提供此方向的依据；本报告没有启动程序验证路径。因此这仍是待运行的配置入口验证，不是已经成功的启动命令，不修改安装目录或用户现有永久设置。

进一步静态检查：`0x3352960` 的非空自定义目录分支直接复制返回路径，没有追加 Epic/CarlaUE4 名称；`[ConsoleVariables]` 在 `0x332dd4a` 至 `0x332dd56` 有实际加载调用，因此优先用该 section 做短启动读回即可，不需要先执行长批次。`-norhithread` 的另一次监督样本确实不再有 RHIThread，而 Vulkan RHIThread CVar 仍为 `[1,1]`，与上文区分全局线程开关和 CVar 的限制相符；运行稳定性以完整对照结果为准。

## 补充：销毁相机、初始化 tick 与 no-rendering 假设

已逐项检查 `rgb-all-every-frame-default/probe.json`，并阅读 CARLA `0.9.15` 官方源码；源文件 URL 和摘要见 [cleanup-quality-sources.json](cleanup-quality-sources.json)。这部分没有改变探测程序、追加 tick 或运行仿真。

“相机销毁之后没有任何物理 tick 就切图”不符合这次记录。Town13 最后图像为 frame 127，接着旧世界的 `pre_load_settings` 从 127 到 128；Town12 最后图像为 232，下一次 `pre_load_settings` 从 232 到 233。原因是 [World::ApplySettings](https://github.com/carla-simulator/carla/blob/0.9.15/LibCarla/source/carla/client/World.cpp#L56) 在固定步长配置下自己调用 `Tick`，直到观察到足够的正确 delta。Town03 的加载返回后，`post_load_settings` 还成功记录了 235→236；报告停留在这个阶段不等于这次 apply RPC 本身没有返回。新地图尚未记录 hero 或相机创建。

[SceneCaptureSensor::EndPlay](https://github.com/carla-simulator/carla/blob/0.9.15/Unreal/CarlaUE4/Plugins/Carla/Source/Carla/Sensor/SceneCaptureSensor.cpp#L607) 调用父类清理后执行 `FlushRenderingCommands()`；父类注销传感器并关闭流。服务端 actor 销毁路径会调用 Unreal actor 的 `Destroy`，随后注销 registry。以上不支持直接认定“Python 少 tick 一次”就是这次空顶点声明的原因。

边界仍需保留：render-command flush 不证明全部 GPU 工作和后台任务都结束。[PixelReader](https://github.com/carla-simulator/carla/blob/0.9.15/Unreal/CarlaUE4/Plugins/Carla/Source/Carla/Sensor/PixelReader.cpp) 存在 RHI readback 与高优先级 TaskGraph 回调，收到最后一帧不构成全局资源屏障；[WaitForRenderThreadToFinish](https://github.com/carla-simulator/carla/blob/0.9.15/Unreal/CarlaUE4/Plugins/Carla/Source/Carla/Sensor/SceneCaptureSensor.h#L399) 中的 flush 实际被注释，不能仅按函数名宣称它已同步。生命周期问题尚不能完全排除，但需要匹配该 `Generate` 栈的进一步证据，不能把任意等待或多推进几帧写成已验证修复。

本次请求和加载后读回的 `no_rendering_mode` 都为 false，没有在每张地图间来回切换。即使切换，[CarlaEngine::OnEpisodeSettingsChanged](https://github.com/carla-simulator/carla/blob/0.9.15/Unreal/CarlaUE4/Plugins/Carla/Source/Carla/Game/CarlaEngine.cpp#L380) 主要设置 GameViewport 的 `bDisableWorldRendering`；这不是关闭 Vulkan 设备、停止 RHI 或阻止地图网格资源初始化的开关。不能依据 no-rendering 下仍发生 Vulkan 崩溃就推断该配置被忽略。

## 补充：Low 与 Epic 的可检验差异

CARLA 官方仓库 [#4966](https://github.com/carla-simulator/carla/issues/4966) 是一个有针对性的原始线索：作者报告 Windows CARLA 0.9.13 在 Low 下 reload Town01 失败，移除 Low 后正常。后续有 Ubuntu 20 / 0.9.13 的相同现象留言；[2026 年 Windows 留言](https://github.com/carla-simulator/carla/issues/4966#issuecomment-3873594129) 报告 0.9.15/0.9.16 Town01 的 D3D12 资源销毁崩溃；[Ubuntu 22 / 0.9.16 留言](https://github.com/carla-simulator/carla/issues/4966#issuecomment-3968327056) 报告 Low 下 load_world SIG11。这些版本、平台和栈并不相同，没有给出本机 `FVulkanVertexInputStateInfo::Generate`/`0x94` 的确认修复。它们支持做质量预设对照，不足以直接归因。

另一个经常被二手材料引用的 [#6399](https://github.com/carla-simulator/carla/issues/6399) 不能当作“Epic 修复”的证据：原作者最初怀疑 Low，随后在评论中撤回，取消 Low 后仍再次出现错误。检索没有找到完全匹配本机 Town03 + Vulkan 空顶点声明的一手已解决问题。

0.9.15 的 [CarlaSettingsDelegate.cpp](https://github.com/carla-simulator/carla/blob/0.9.15/Unreal/CarlaUE4/Plugins/Carla/Source/Carla/Settings/CarlaSettingsDelegate.cpp) 包含以下质量函数请求，涉及一组渲染路径，而不仅是分辨率。**表中是源码请求值或对应操作，不是本机 Shipping 的运行时读回值，也不证明函数在该次加载中实际执行。**

| 源码中的请求项 | Low 函数请求 | Epic 函数请求 |
| --- | --- | --- |
| `r.RHICmdBypass` | 0 | 1 |
| `r.EarlyZPass` | 2 | 3 |
| `r.SceneColorFormat` | 2 | 4 |
| `sg.EffectsQuality` / `sg.TextureQuality` / `sg.ShadowQuality` | 0 | 3 |
| `r.SeparateTranslucency` | 0 | 1 |
| 道路和 actor | Low 材质数组、剔除距离、禁用部分阴影和后处理 | Epic 材质数组、恢复对应距离/阴影/后处理 |

`SetAllRoads`、`SetAllActorsDrawDistance`、`SetAllLights` 把操作投递到 GameThread；道路材质数组非空且槽匹配时才执行 `SetMaterial`。这给出了加载阶段材质/渲染状态变化的实际源码依据，但不能证明该任务导致了 null 指针，也不能假定每张地图都有匹配替换材质。

已有实际反例：[native-town13-epic/samples.jsonl](native-town13-epic/samples.jsonl) 的 93.410 和 94.435 秒采样，在地图已加载后读到 `r.RHICmdBypass=[0,0]`，同时存在 RHIThread；`EnableDefrag=1`、`WaitForIdleOnSubmit=0`、`AsyncPipelineCompile=[1,1]`。因此不能把 Epic 运行记为“RHI bypass 已启用”，更不能把其运行结果归因于 bypass。该批次在本次记录时仍在执行，这些样本只证明采样时的配置，不代表完整批次通过。

源码本身还有执行条件：`AppliedLowPostResetQualityLevel` 静态初值和 `Reset()` 都设为 Epic，`ApplyQualityLevelPostRestart()` 在所记档位与请求档位相等时直接返回。因此新进程 Epic 可能没有执行表中的 Epic 命令函数；命令未执行、Shipping exec 行为或后续覆盖需要区分，当前不根据值为 0 就选定其中一种原因。

因此，若同输入 Epic 通过、Low 失败，准确结论应为**质量预设组合影响复现条件**；不能直接归因于 `r.RHICmdBypass`、低质量材质、某条 shader 或线程竞态。新进程 Town03-only 的 Low/Epic 对照用于区分地图自身条件与前两张图积累条件；之后仍需同地图顺序、同 RGB 帧数的完整重复验证。实际运行结果由各目录中的 probe/outcome 记录，不在本只读报告中把进行中的批次算作通过。

本机同一 SHA 二进制还核实了 `r.RHICmdBypass`：UTF-16 名称 `0xb21074`，注册代码 `0x47b399f`，默认参数 0、flags 0；对象指针槽 `0x6e5fe10`，**数据指针槽 `uint64 @0x6e5fe18`**。读取后者得到 `p`，再只读 `int32 @p` 和 `p+4`。内嵌帮助把 0 描述为使用 RHI command list、1 描述为绕过并直接提交。该数值不能用 RHIThread 是否存在代替；注册初值也不能代替加载质量档位后的实际值。证据已补入 [binary-cvar-readonly.json](binary-cvar-readonly.json)。

质量函数源码使用 `GEngine->Exec` 请求命令。Epic [CVar 优先级说明](https://dev.epicgames.com/documentation/en-us/unreal-engine/console-variables-in-cplusplus?application_version=4.27) 将 Console 的优先级放在 SystemSettingsIni、ConsoleVariablesIni、Scalability 之上；如果相关 exec 路径确实执行并被接受，启动 ini 不一定保持最终值，需要加载地图后读回。这里的优先级是根据官方机制与调用方式的条件性推断，尚未读取该 CVar 的运行时 LastSetBy；上面的 Epic 实测尤其说明不能把源码请求直接当成生效值。不要叠加配置后仅从命令行声称已完成单变量实验。
