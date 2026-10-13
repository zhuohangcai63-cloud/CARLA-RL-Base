# CARLA 0.9.15 Vulkan 诊断候选配置

**未通过长期稳定性验收，不作为已修复方案推荐。** 追加的 `UseBufferBinning=1` 单变量试验同样失败。 最新 Town13 41 条路线批次在第 11 条初始化发生原生 SIGSEGV；以下保留精确配置与复现方法，供后续驱动/引擎排查。

本配置用于本机 Linux / CARLA 0.9.15 的渲染兼容性验证：Epic 画质、关闭独立 RHI 线程，并通过独立用户目录加载 [Engine-vulkan.ini](../configs/carla/Engine-vulkan.ini)。它与已经实际读回验证的 v2 诊断配置逐字节相同，不修改 CARLA 安装目录或原有 Saved 配置。

| 项目 | 显式设置 |
| --- | --- |
| 画质 | `-quality-level=Epic` |
| 独立 RHI 线程 | `-norhithread` |
| Vulkan 内存整理 | `r.Vulkan.EnableDefrag=0` |
| 每次提交等待 GPU 空闲 | `r.Vulkan.WaitForIdleOnSubmit=0` |
| 异步图形流水线编译 | `r.AsyncPipelineCompile=0` |

`WaitForIdleOnSubmit` 保持 **0**；曾设置为 1 的组合在本机初始化失败。此配置会改变渲染并行度、内存行为及性能，不能把它当作完全等价的默认引擎设置，也不能把单项参数认定为所有崩溃的原因。参数与实际线程/CVar 的读回证据见 [渲染测试记录](testing/town13-investigation/render-results.md)。

## 启动

从仓库根目录，在 Bash 中执行；按安装位置设置 `CARLA_ROOT`。每次命令创建一个独立用户目录并保存启动命令和配置摘要，保留实验可追溯性。

```bash
EASYCARLA_CARLA_ROOT="${CARLA_ROOT:-$HOME/software/carla}"
EASYCARLA_VULKAN_USERDIR="$(mktemp -d "${TMPDIR:-/tmp}/easycarla-vulkan.XXXXXX")"
mkdir -p "$EASYCARLA_VULKAN_USERDIR/Saved/Config/LinuxNoEditor"
cp configs/carla/Engine-vulkan.ini "$EASYCARLA_VULKAN_USERDIR/Saved/Config/LinuxNoEditor/Engine.ini"
sha256sum "$EASYCARLA_VULKAN_USERDIR/Saved/Config/LinuxNoEditor/Engine.ini" > "$EASYCARLA_VULKAN_USERDIR/profile.sha256"

EASYCARLA_CARLA_ARGS=(
  -quality-level=Epic -norhithread "-UserDir=$EASYCARLA_VULKAN_USERDIR"
  -RenderOffScreen -nosound -ResX=400 -ResY=300
  -carla-rpc-port=2000 -carla-streaming-port=2001 -carla-secondary-port=2002
)
printf '%q ' "$EASYCARLA_CARLA_ROOT/CarlaUE4.sh" "${EASYCARLA_CARLA_ARGS[@]}" > "$EASYCARLA_VULKAN_USERDIR/launch-command.txt"
"$EASYCARLA_CARLA_ROOT/CarlaUE4.sh" "${EASYCARLA_CARLA_ARGS[@]}"
```

需要 GUI 时，从参数数组中删去 `-RenderOffScreen`；环境配置同时保持 `simulation.no_rendering_mode: false`，例如 [bench2drive_eval.yaml](../configs/bench2drive_eval.yaml)。`-RenderOffScreen` 表示离屏渲染，不等于关闭渲染。另一个终端按 [安装与联调说明](installation.md) 启动环境服务。多个实例必须使用不同 CARLA/TM 端口和不同用户目录。

配置文件 SHA-256 应为 `4fde779581b3940e587ee69db46bcdf4d1739d1737a163d837cff26076777215`。共享实验结果时保存该摘要、完整启动命令、实际服务端版本、环境 YAML、地图及路线清单。特别保留画质和流送/激活距离；650 m 距离会影响远处 NPC 激活范围，详见 [地图性能配置](map-performance.md)。本机 shipping 二进制的 `-ini:...`、`-ExecCmds=...` 覆盖曾未实际生效，因此这里使用已验证的 `-UserDir` 配置路径。

## 验证边界（2026-09-28）

| 验证 | 运行结果 | CARLA 进程退出 |
| --- | --- | --- |
| 12 张地图 × 2 轮，RGB 每帧检查 | 24 次加载、2,400 tick/RGB 通过 | 本次正常响应停止请求，整体通过；[原始记录](testing/town13-investigation/rgb-all-conservative-v2/summary.json) |
| Town12/13 四条原生路线，逐回合重载并启用新 episode 就绪屏障 | 3 条完成、1 条达到 1,000 tick 预算，均正常返回；达到预算不等于驾驶任务成功 | 停止超时后被诊断监督器强杀，整体失败；[原始记录](testing/town13-investigation/native-rendered-barrier-v2/summary.json) |
| Town12/13 Scenic 实际环境 | 两项 hero、地面高度、时钟及自有资源清理测试通过；无 NPC、无 RGB | 停止超时后被诊断监督器强杀，整体失败；[原始记录](testing/town13-investigation/scenic-large-maps-barrier-v2/summary.json) |
| Town13 完整 41 条原生路线 | **失败**：10 条返回（4 条原生结束、6 条达到预算），第 11 条 `3816` 初始化崩溃、结果未知；后 30 条未运行 | 服务端 SIGSEGV，NVIDIA 驱动 / Vulkan staging buffer 绑定路径；[运行记录](testing/town13-investigation/native-town13-reload-barrier-v2/summary.json) |

短批次通过之后仍出现上述长批次失败，因此这些渲染参数没有解决本机全部原生崩溃。客户端切图屏障属于环境代码的独立修复，不能把结果全部归功于渲染参数；此前客户端 `get_map()` SIGSEGV 及 UE 渲染线程 SIGSEGV 的证据仍保留在 [调查报告](testing/town13-investigation/README.md)。

CARLA 上游 [PR #9529](https://github.com/carla-simulator/carla/pull/9529) 修复一类服务端 RPC 停止死锁，2026-01-22 合入 UE4 分支。它涉及 C++ 服务端代码，需要构建包含补丁的 CARLA/LibCarla 并重新验收；本仓库没有把该补丁应用到现有 0.9.15 发行二进制，INI 或 Python 配置不能替代它，也尚未证明本机所有停止挂起同因。正常 `env.close()` 只清理客户端会话和回合，不关闭外部启动的 CARLA。本启动说明不设置自动强杀、重启或失败动作重放。
