# Town13 原生长批次：Low / Epic 失败对照

2026-09-28，只读分析两次已结束的原生场景批次；未启动 CARLA、未重放失败动作、未修改安装。**Epic 没有修复这次原生网格上传崩溃。** 这与 Epic 下有限 RGB 全图检查通过并不矛盾：工作负载和故障栈不同。

| 观察 | Low 批次 | Epic 批次 |
| --- | --- | --- |
| 原始监督记录 | [lifecycle.json](native-town13-default-rhi/lifecycle.json) | [summary.json](native-town13-epic/summary.json) |
| 派生结论 | [outcome.json](native-town13-default-rhi/outcome.json) | [outcome.json](native-town13-epic/outcome.json) |
| 服务退出 | 运行期 SIGSEGV / -11 | 运行期 SIGSEGV / -11，未发送停止信号 |
| 已返回路线 | 38：16 completed、22 tick_budget_reached | 38：16 completed、22 tick_budget_reached |
| 已确认物理 tick | 30192 | 29914 |
| 中断路线 | 第 39 条 31736 / VehicleOpensDoorTwoWays | 相同 |
| 最后阶段 | `LOADED 31736` 后，未出现 `PREPARED 31736` | 相同 |
| 余下路线 | 31654、31124 未运行 | 相同 |
| 监督墙钟时间 | 1684.38 秒 | 1675.58 秒 |
| 采样峰值服务 RSS | 8.53 GiB | 7.99 GiB |
| 采样峰值整卡显存 | 6362 MiB | 7023 MiB |
| 采样最低可用 RAM | 17.07 GiB | 17.18 GiB |

两份原始 `probe.json` 保持 `status=running`，中断条目也保持 `running`；这是客户端被监督器结束前最后写下的材料。派生 outcome 将其解释为未完成/执行结果未知，没有改写原始记录。38 条返回不等于 38 条驾驶任务成功，也不能把两次相同 seed 当成相同轨迹：总 tick 数已经不同。

## 栈的共性和差异

两次都在 RenderThread 的 `FVulkanResourceMultiBuffer` 构造期间、`VulkanIndexBuffer.cpp:136` 对应的 `memcpy` 中非法写。

- Low 的调用方是 `RHICreateVertexBuffer` → `FStaticMeshVertexBuffer` 的切线缓冲初始化，故障写地址为 `0xe57d9000`。
- Epic 的调用方是 `RHICreateIndexBuffer` → `FRawStaticIndexBuffer` 初始化，故障写地址为 `0x0`。
- 这两次不是 Town03 RGB 测试中的 `FVulkanVertexInputStateInfo::Generate / 0x94` 空顶点声明读取故障。不能用后者的画质或异步 PSO 结论覆盖前者。
- 两份 XML 的 `MemoryStats.bIsOOM` 都为 0；采样也不支持系统 RAM 已耗尽。特定池、映射、失效指针或生命周期问题仍可能存在。

原始栈分别见 [Low Diagnostics](native-town13-default-rhi/crash/Diagnostics.txt) 和 [Epic Diagnostics](native-town13-epic/crashes/crashinfo-CarlaUE4-pid-110417-7682427C4C194E94ADD117757551EFA7/Diagnostics.txt)。最新 XML 分享副本已去除设备标识并追加 [redaction-manifest.json](redaction-manifest.json)；堆栈及安装目录原件未改。

## 上传路径的只读复核

未取得本机版本的完整 UE4 源文件；以下依据安装二进制、随附 `.debug` 的文件行映射和机器指令，不把未知源码内容写成已证实事实。二进制摘要和原先的构造函数指令已记录于 [binary-cvar-readonly.json](binary-cvar-readonly.json)。

| 地址 | 符号/行为 |
| --- | --- |
| `0x469e8eb` | 构造函数调用 `FVulkanResourceMultiBuffer::Lock`；返回值被保存为复制目的地址 |
| `0x469e8d8` | 复制长度在前置指令中取两个 unsigned 值的最小值，调试符号对应 `FGenericPlatformMath::Min` |
| `0x469e906` | `memcpy@plt`；返回地址 `0x469e90b` 对应崩溃记录中的构造函数帧 |
| `0x469f052` | Lock 的一个分支调用 `FStagingManager::AcquireBuffer`；对应 VulkanIndexBuffer.cpp:272 |
| `0x469f05d` | 随后调用 `FStagingBuffer::GetMappedPointer`；对应 VulkanIndexBuffer.cpp:274 |
| `0x469f1c9` | 另一个分支调用 `FVulkanAllocation::GetMappedPointer` |

可复核命令（只读，不附加进程）：

```bash
addr2line -f -C \
  -e /home/czh/software/carla/CarlaUE4/Binaries/Linux/CarlaUE4-Linux-Shipping.debug \
  0x469efb0 0x469f052 0x469f05d 0x46b6d60 0x46b6b70 0x46b5ee0 0x469e8d8
objdump -d --start-address=0x469e8b5 --stop-address=0x469e920 \
  /home/czh/software/carla/CarlaUE4/Binaries/Linux/CarlaUE4-Linux-Shipping
```

这些证据把问题缩小到 Vulkan 缓冲锁定/映射后初始数据复制；没有 core 寄存器就无法确认当次走了哪条 Lock 分支、真实目的指针和长度。不能由 Epic 的零地址直接断言 defrag、OOM 或驱动返回空映射。相邻的 [CARLA #6312](https://github.com/carla-simulator/carla/issues/6312) 有 0.9.14 地图切换时 `AcquireBuffer` → `Lock` → 同类构造函数崩溃的一手报告，但它的访问地址和栈顶不同，且没有已确认修复。

`no_rendering_mode` 禁止绘制，并不销毁 Vulkan 设备或禁止流送网格的 RHI 初始化；[CARLA 0.9.15 CarlaEngine.cpp](https://github.com/carla-simulator/carla/blob/0.9.15/Unreal/CarlaUE4/Plugins/Carla/Source/Carla/Game/CarlaEngine.cpp) 设置的是 viewport 的 `bDisableWorldRendering`。前两条 30008、37391 结束时 owned registry 都为 0，只证明 Python 管理的 actor 已获销毁确认，不能证明引擎缓冲池和 GPU 工作没有残留。

## 有界的后续对照

Epic 批次的 1632 个有效 CVar 样本中，`r.Vulkan.EnableDefrag=1`、`r.Vulkan.WaitForIdleOnSubmit=0`；初始化后 AsyncPipelineCompile/RHIThread 为 `[1,1]`，RHICmdBypass 为 `[0,0]`。请求 Epic 并没有令这里的 RHICmdBypass 变成 1，必须继续以实际读回为准。

先用新进程运行 30008 → 37391 → 31736 三条、保持种子/步数/距离/无渲染设置，确认是否保留触发条件。若复现，再单独通过已验证的隔离 UserDir 配置入口设 `r.Vulkan.WaitForIdleOnSubmit=1`，读回确认后比较；它串行等待 GPU，针对提交和资源重用时序，但会牺牲性能。另一个独立候选是 `r.Vulkan.EnableDefrag=0`，同样必须读回。既往关闭 defrag 仍失败的是 Low/Town03 的另一栈，尚不能排除此处的 defrag 假设。

若短序列不能复现，应明确它未保留长前缀的条件，再缩小必要前缀；不把短序列通过写成长批次修复，也不盲目重复完整 41 条。若短序列能复现且两个单变量均无效，下一步价值在完整 core/寄存器及分配映射的证据，而不是再叠加更多未经核实的 CVar。
