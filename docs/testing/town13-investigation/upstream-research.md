# Town12 / Town13 原生崩溃：上游资料核查

检索日期：2026-09-28。本文件是文献与源码审计，不代表任何候选修复已在本机通过。未启动 CARLA、未改动驱动或引擎设置。

## 已找到的讨论与证据范围

| 来源 | 实际报告 | 能说明什么 |
| --- | --- | --- |
| [Bench2Drive #234](https://github.com/Thinklab-SJTU/Bench2Drive/issues/234) | 2026-06-24，CARLA 0.9.15、RTX 5090、原生 Windows，模型与特权专家都在部分 Town12/13 长时间测试中崩溃或挂起 | 其他安装也遇到大地图不稳定；截至检索未见维护者确认的统一修复。不能证明本机与该报告是同一根因。 |
| [CARLA discussion #4639](https://github.com/carla-simulator/carla/discussions/4639) | 0.9.12 的 RenderThread 60 秒超时；一名用户报告重装适合其 GPU 的驱动后解决 | 驱动兼容值得 A/B；没有给出适用于本机 RTX 5060 Ti 的版本或可复现修复。 |
| [CARLA #8369](https://github.com/carla-simulator/carla/issues/8369) | Ubuntu 22.04 容器中 0.9.15、`-RenderOffScreen`，相同 60 秒超时 | 仅报告相同症状，未提供已验证修复。 |
| [CARLA #9492](https://github.com/carla-simulator/carla/issues/9492) | RTX 4090 云容器；作者称从 570 换到 530.41.03 后能启动 | 是另一硬件的用户自报结果，不能照搬到 Blackwell。 |
| [CARLA #8584](https://github.com/carla-simulator/carla/issues/8584) | Intel Arc，`vkCreateDescriptorPool` 返回 `VK_ERROR_OUT_OF_DEVICE_MEMORY` | 与本机 `AllocUniformBuffer` 非对齐访问不是同一异常；不能据此把本机故障认定为显存不足。 |
| [Epic Vulkan RHI 讨论](https://forums.unrealengine.com/t/vulkan-rhi-causing-regular-engine-crash-assertion-failed-error/479354)、[第 7 条回复](https://forums.unrealengine.com/t/vulkan-rhi-causing-regular-engine-crash-assertion-failed-error/479354/7) | 主题包含 UE 4.26.1/Linux 的纹理 layout/defrag 断言。2023 年用户称关闭 defrag 仍崩，`r.Vulkan.WaitForIdleOnSubmit=1` 后改善但性能下降；2024 年另一回复支持该办法 | 用户自报的诊断候选，不是 Epic 确认的补丁；原始栈在 `VulkanBarriers / DefragTick`，不同于本机 `AllocUniformBuffer`，不能声称同因或本机已修复。 |

未找到 CARLA 0.9.15 在本机 `VulkanMemory.cpp:3586 / AllocUniformBuffer` 栈对应的官方修复提交，也未找到明确把它归因于驱动 580.178.04 或内核 7.0.0 的上游结论。RenderThread 超时表示等待渲染线程未返回，本身不能区分 GPU 卡死、引擎线程问题、负载或资源错误。

## 地图包完整性如何确认

官方要求导入与主程序版本相同的附加资源包：[0.9.15 安装说明](https://carla.readthedocs.io/en/0.9.15/start_quickstart/#import-additional-assets)、[0.9.15 发布页](https://github.com/carla-simulator/carla/releases/tag/0.9.15)。该版本发布说明明确新增 Town13 与 Town15。

本机 `/home/czh/software/carla/ImportAssets.sh` 与[上游脚本](https://github.com/carla-simulator/carla/blob/0.9.15/Util/ImportAssets.sh) 使用 `tar --keep-newer-files`。所以重复导入会保留时间较新的既有文件；“脚本跑过”不能证明文件与包完全一致。应依次核对：压缩流完整性、包内成员路径、安装文件存在性/长度、逐文件 SHA-256；若有官方可信校验值，再核对整个下载包。只有与本地原始压缩包一致时，也不能进一步声称下载包必然来自正确官方版本。

大地图按 tile 延迟载入：地图能出现在列表、能读 OpenDRIVE 或某一条路线成功，不能证明所有 tile 完整。[0.9.15 `LargeMapManager.cpp`](https://github.com/carla-simulator/carla/blob/0.9.15/Unreal/CarlaUE4/Plugins/Carla/Source/Carla/MapGen/LargeMapManager.cpp#L590) 在包缺失时有明确的 `Level does not exist in package` 日志；应在加载故障区域时检查此类资源错误。当前已知 Vulkan 栈没有直接证明资源损坏。

## 优先 A/B 次序

1. **同一二进制、同一地图文件、纯 CARLA API**：固定路线起点，分别测试默认加载与加载前关闭 `spectator_as_ego`；先无传感器、后单 RGB；完整记录加载、出生、tick、销毁各阶段。不要把 Bench2Drive 全部加入第一轮复现。
2. **无 hero 的初始化负载**：官方 API 说明默认 spectator 会触发周边 tile 载入；[官方 leaderboard evaluator，固定提交 b00c8bd7](https://github.com/carla-simulator/leaderboard/blob/b00c8bd7fe9ea7a2783cbbdbc53e419c65d6a0ad/leaderboard/leaderboard_evaluator.py#L166-L213) 在初始设置中关闭该行为，`load_world(reset_settings=False)` 后重设 tile/actor 距离为 650 m。引用文件提交于 2024-05-17，本次核实于 2026-09-28；这是其评估器配置，不是 Town13 全部场景的稳定性承诺。`reset_settings=False` 不足以保证大地图距离保留，必须读取返回值。hero 生成时立即加载并等待所需 tile，是需要单独测量的初始化阶段。[WorldSettings 文档](https://carla.readthedocs.io/en/0.9.15/python_api/#carla.WorldSettings)
3. **引擎线程路径**：单独 A/B `-norhithread`；若需要，再测试 `-norenderthread` 或 `-onethread`，每次只改变一个。Epic 的[线程诊断说明](https://dev.epicgames.com/documentation/unreal-engine/render-dependency-graph-in-unreal-engine)描述了关闭 RHI 线程会改变并可能掩盖某些跨线程错误。当前官方在线文档是新版，因此本机仅额外核实这些字符串确实存在于 4.26 shipping 二进制，见 [binary-options-readonly.json](binary-options-readonly.json)；是否实际生效仍需运行证据。性能可能下降，不能默认作为正式优化。
   若原配置仍有可复现的 Vulkan 故障，再单独测试 `'-ini:Engine:[SystemSettings]:r.Vulkan.WaitForIdleOnSubmit=1'`。本机只读字符串核查发现变量名位于文件偏移 10606108，GPU 提交后等待空闲的帮助位于 10046302；帮助列出默认 0 不等待、1 每次提交等待，以及 2/3 针对不同提交类型等待。这里只核实二进制包含相应配置与帮助，没有运行或修改配置，也未测得它对本机崩溃的效果。该选项减少 GPU/CPU 并行，属于有性能代价的隔离诊断，不能与低负载配置合并后笼统称为修复。
4. **资源和系统关联**：连续采集显存、RSS、可用内存、GPU 利用率、温度、Xid、PCIe AER，并与故障时间对齐。Linux 将 correctable AER 定义为硬件可自行恢复、无数据丢失，因此已有 AER 增长只是待排查线索，不等于 GPU 损坏或崩溃原因。[内核 AER 文档](https://docs.kernel.org/PCI/pcieaer-howto.html)
5. **最后才换驱动/内核**：固定最小复现，使用另一个明确支持 RTX 5060 Ti 的驱动及匹配内核对比。NVIDIA 的 [580 支持列表](https://download.nvidia.com/XFree86/Linux-x86_64/580.126.09/README/supportedchips.html)包含该卡；[内核模块说明](https://download.nvidia.com/XFree86/Linux-x86_64/580.126.09/README/kernel_open.html)明确 Blackwell 仅支持 open kernel modules。不能照抄老帖安装 530、切换 proprietary 模块或关闭 GSP。

## 可采用的减负措施及边界

| 措施 | 明确作用与限制 |
| --- | --- |
| 按地图设置 `tile_stream_distance`、`actor_active_distance` | 影响加载范围及远处参与者休眠，适合大地图，但会影响仿真语义。必须覆盖场景触发、感知及背景交通需求，记录实际值。650 m 是上述官方 evaluator 的值，不是所有研究场景的保证。 |
| `spectator_as_ego=False` | 防止无 hero 时 spectator 造成不需要的 tile 加载；应纳入加载前配置，结束清理同样需考虑。正常有 hero 时仍加载其周边。 |
| `-quality-level=Low` | CARLA 官方支持；关闭阴影和后处理，缩短绘制距离，改变相机输入。[渲染说明](https://carla.readthedocs.io/en/0.9.15/adv_rendering_options/) 本机后续对照在 Low 下复现 Vulkan 崩溃，因此当前诊断默认使用 Epic；Low 仅保留为显式复现条件，不作为本机稳定运行建议，见[实测报告](README.md)。 |
| `-RenderOffScreen` | 隐藏窗口但继续渲染，RGB 可用；不等同于关闭 GPU 渲染。 |
| `no_rendering_mode=True` | 适合结构化状态研究，GPU 相机数据不可用；它是世界设置，不能证明引擎初始化/资源装卸完全绕过 Vulkan。 |
| 降低相机分辨率、数量/频率 | 直接降低渲染与传输负载；帧同步须相应配置，不能把缺帧当作有效观测。减小 spectator 窗口不等于减少所有相机分辨率。 |
| `MapLayer.NONE` | [API 示例](https://carla.readthedocs.io/en/0.9.15/python_api/#carla.World.load_map_layer)针对 `_Opt` 分层地图；不能只因参数被接受就声称普通 Town12/13 已卸载建筑/植被，需检查实际效果。 |
| 纹理池与 mip 配置 | UE4 有 `r.Streaming.PoolSize`、`r.Streaming.LimitPoolSizeToVRAM` 等配置；池只约束部分纹理内存，不是整个显存硬上限。需实际测量后再启用，避免降低纹理或造成流送抖动。[Epic UE4.27 文档](https://dev.epicgames.com/documentation/en-us/unreal-engine/texture-streaming-configuration?application_version=4.27) |

本机二进制也含 `r.Vulkan.UseRealUBs`，帮助明确 0 为“仅调试”的 uniform buffer 模拟路径；其在预编译 shader 下的兼容性与配置生效时机未验证，不能包装成已知修复。延长客户端 timeout 不会修复原生 SIGSEGV；关闭 RenderThread watchdog 可能把可见崩溃变成无限挂起。0.9.15 Linux 使用 Vulkan，`-opengl` 不是该版本可用的替代方案。[官方渲染说明](https://carla.readthedocs.io/en/0.9.15/adv_rendering_options/)

最终验收应分别列出：原始画质/传感器配置、声明的低负载配置、地图切换重复次数、路线/区域覆盖、运行小时数，以及未覆盖部分；不能用成功加载一次替代稳定性结论。

## Uniform buffer 分支的追加只读核查

为确认候选参数并非不存在的“调优秘籍”，对本机 shipping 二进制及其随附 `.debug` 符号做了静态反汇编，没有运行/附加 CARLA。摘录与复现命令见 [binary-startup-readonly.txt](binary-startup-readonly.txt)。没有取得 Epic 官方公开的 UE4.26 完整源码，以下是本机二进制证据及相应推断：

- `FEngineLoop::PreInitPreStartupScreen` 在 `0x270b328` 调用 `FSystemSettings::Initialize`，随后处理 device profile 和 console variables；`RHIInit` 的调用在 `0x270bfa0`。因此应通过启动配置做对比，不能依赖初始化之后的 console command。
- `RHICreateUniformBuffer` 查找字符串地址 `0xac065a`，对应 `r.Vulkan.UseRealUBs`。值大于 0 的分支调用 `AllocUniformBuffer`；小于等于 0 的分支创建 emulated uniform buffer 并复制到 CPU 数组。关闭真实 UB 确实改变已有崩溃栈所经过的分支，但未证明预编译 shader、正常 RGB 输出以及长期运行兼容。
- 单独候选启动参数是 `'-ini:Engine:[SystemSettings]:r.Vulkan.UseRealUBs=0'`。其形式符合 [Epic 配置覆盖语法](https://dev.epicgames.com/documentation/unreal-engine/configuration-files-in-unreal-engine#overrideconfigurationfromthecommand-line)，且本机二进制存在 ini 覆盖解析。仍须运行时确认取值，排除配置未生效；当前仅是隔离诊断候选。
- 二进制含 `r.Vulkan.EnableDefrag`、`r.Vulkan.MemoryFallbackToHost`、`r.Vulkan.SingleAllocationPerResource`，仅有名字不构成推荐理由。尤其未测得 OOM 时，不能据此开启 host-memory fallback 或增大纹理池作为修复。

同样应把退出阶段 `FVulkanResourceHeap` 析构崩溃与路线运行时崩溃分开记录：前者在不加载目标地图时也发生，并不能单独证明 Town13 资源损坏或正式运行不可用。
