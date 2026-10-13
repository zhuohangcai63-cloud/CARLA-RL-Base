# 隔离 Vulkan API 检查复现

这是原生故障诊断，不是正式评估配置或崩溃修复。检查层会改变时间与内存布局；本机测试发现了 Vulkan API 错误，即使进程继续运行也不能称为 API 检查通过。原始参数与包摘要见 [vulkan-validation-profile.json](vulkan-validation-profile.json)，调用点核查见 [driver-bind-buffer-audit.md](driver-bind-buffer-audit.md)。

以下是本机 Ubuntu 24.04、CARLA 0.9.15 的复现步骤，从环境仓库根目录执行。不安装系统包，不替换驱动，仅为本次进程启用已解包的检查层。

```bash
EASYCARLA_VK_TEST="$(mktemp -d /tmp/easycarla-vulkan-check.XXXXXX)"
(cd "$EASYCARLA_VK_TEST" && apt-get download vulkan-validationlayers=1.3.275.0-1)
dpkg-deb -x "$EASYCARLA_VK_TEST/vulkan-validationlayers_1.3.275.0-1_amd64.deb" "$EASYCARLA_VK_TEST/layer"

python - "$EASYCARLA_VK_TEST" <<'PY'
from pathlib import Path
import json, sys
root = Path(sys.argv[1])
manifest = root / 'layer/usr/share/vulkan/explicit_layer.d/VkLayer_khronos_validation.json'
data = json.loads(manifest.read_text())
data['layer']['library_path'] = str(root / 'layer/usr/lib/x86_64-linux-gnu/libVkLayer_khronos_validation.so')
manifest.write_text(json.dumps(data, indent=2) + '\n')
(root / 'vk_layer_settings.txt').write_text(
    'khronos_validation.debug_action = VK_DBG_LAYER_ACTION_LOG_MSG\n'
    'khronos_validation.log_filename = stdout\n'
    'khronos_validation.report_flags = error,warn\n'
    'khronos_validation.enable_message_limit = true\n'
    'khronos_validation.duplicate_message_limit = 3\n')
PY

mkdir -p "$EASYCARLA_VK_TEST/profile/Saved/Config/LinuxNoEditor"
cp configs/carla/Engine-vulkan.ini "$EASYCARLA_VK_TEST/profile/Saved/Config/LinuxNoEditor/Engine.ini"

env VK_LAYER_PATH="$EASYCARLA_VK_TEST/layer/usr/share/vulkan/explicit_layer.d" \
    VK_INSTANCE_LAYERS=VK_LAYER_KHRONOS_validation \
    VK_LAYER_SETTINGS_PATH="$EASYCARLA_VK_TEST/vk_layer_settings.txt" \
    VK_LOADER_DEBUG=error,warn,layer \
python scripts/run_carla_diagnostic.py \
    --carla-root /home/czh/software/carla --port 2600 \
    --output-dir "$EASYCARLA_VK_TEST/result" --quality-level Epic \
    --server-arg=-norhithread \
    --server-arg="-UserDir=$EASYCARLA_VK_TEST/profile" \
    --saved-dir "$EASYCARLA_VK_TEST/profile/Saved" \
    --timeout 1800 --stop-timeout 60 \
    -- env PYTHONFAULTHANDLER=1 python scripts/validate_native_routes.py \
       --port 2600 --tm-port 8600 \
       --manifest docs/testing/town13-investigation/town13-eval41.json \
       --route 34183,3483,37969,3475,32221,29893,28387,31045,36792,29770,3816 \
       --output '{OUTPUT_DIR}/probe.json' --seed 123 --ticks 1000 \
       --timeout 90 --reload --map-layers all \
       --tile-stream-distance 650 --actor-active-distance 650
```

这是最新失败批次的前 11 条，顺序与种子保持不变。选择清单中的子集不修改官方路线。端口必须空闲；工具只回收自己启动的进程。查看 `server.txt` 中检查层实际加载证据与 VUID，查看 `probe.json` 的路线状态以及 `summary.json` 的运行/停止分类。每类信息最多记录三次，因此日志数量不是错误发生频率。

对 `r.Vulkan.UseBufferBinning=1` 的单变量试验必须使用另一个独立目录，在复制的 `Engine.ini` 末尾增加该行，再执行同样命令；该开关会改变分配布局，不应仅凭短批次无错误便作为修复发布。真实取值需另行验证，本调查的二进制专用只读包装器已归档，它不属于普通客户端接口。

不要混用其他版本的 NVIDIA 用户态库与当前内核驱动。检查层和驱动是不同组件；本步骤没有进行驱动版本对照。
