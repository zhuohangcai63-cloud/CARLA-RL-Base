# CARLA 0.9.15 安装与额外地图包完整性排查

日期：2026-09-28。安装位置：`/home/czh/software/carla`。本次检查只读，不重新导入、不替换资产、不启动 CARLA。

## 结论

**没有发现 Town12/Town13 下载损坏或额外地图包导入遗漏。** 两个本地压缩包与 CARLA 官方下载存储当前对象的分段 ETag 完全一致；额外地图包 gzip CRC 通过，全部 21,912 个常规文件与安装目录逐个 SHA256 相同。主包与额外包合并的 44,711 条常规文件路径也全部匹配预期最终内容。这包括 Town12/Town13 的分块地图、专属资产、OpenDRIVE、Traffic Manager 缓存及共享资产。

这组结果可以排除已审计归档路径中的文件缺失、截断、其他版本内容覆盖，以及重新导入即可修好的字节损坏。它不能证明官方资产本身没有缺陷，也不能排除运行时引擎、驱动、内存或线程问题。归档之外额外安装的文件未包含在这项内容校验中。无需为当前崩溃重新下载或盲目覆盖地图安装。

## 包来源与完整性

本地 `VERSION` 为 `0.9.15`。归档仍然保留在安装目录：

| 归档 | 字节数 | 官方对象时间 | 官方 multipart ETag | 本地复算 |
|---|---:|---|---|---|
| `CARLA_0.9.15.tar.gz` | 8,386,636,048 | 2023-12-08 21:05:57 UTC | `e7e759a251bbd7c62bcd38b789ad8499-84` | 一致 |
| `Import/AdditionalMaps_0.9.15.tar.gz` | 7,375,946,087 | 2024-05-27 08:20:28 UTC | `25fb0bda594ad2fef5431a056c9ee990-880` | 一致 |

本地 SHA256：

```text
CARLA_0.9.15.tar.gz
7b2f432ce74b251593f95dcc8dee6d99ad9625c2a71a2d6d48f24d879de19ef7

AdditionalMaps_0.9.15.tar.gz
49db78d14fef1f403f69fe1a13f1f1802f2111a6d98161826b2ae0d6dd96bba8
```

SHA256 是此次本地审计生成的摘要，不冒充官方发布的签名/摘要。远端 HEAD 查询使用历史官方发布链接指向的 `carla-releases.s3.us-east-005.backblazeb2.com/Linux/`，当前 release 页的 `downloads.carlasim.com` 在此网络返回 HTTP 403。进一步通过 `?partNumber=1` 取得真实分段大小：主包 100,000,000 字节，额外包 8,388,608 字节；对每段计算 MD5，再对所有分段 MD5 的二进制拼接计算 MD5，并附上分段数量。两个结果均与远端 ETag 完全一致。Multipart MD5 用作传输/内容一致性校验，不代表发布者数字签名。

原始 HTTP 头、存储版本 ID、分段头及本地校验结果见 [official-archive-identity.json](official-archive-identity.json)。

## 安装文件对照

额外包所有常规文件流式读取，对每个归档成员及对应已安装文件计算 SHA256，不仅比对文件名和长度。读取 tar 结束块后继续读取 gzip 到 EOF，从而检查 gzip CRC/长度尾部。重复成员按最后一个条目为准；本包实际上没有重复成员。

| 检查 | 结果 |
|---|---|
| gzip CRC | 通过 |
| 常规文件 | 21,912 |
| 常规文件解压后总字节 | 18,545,040,524 |
| 逐文件 SHA256 相同 | 21,912 |
| 缺失、内容差异 | 0 |
| 重复归档路径 | 0 |
| 耗时 | 83.69 秒 |

详见 [additional-maps-full-audit.json.gz](additional-maps-full-audit.json.gz)，保存每个文件的归档/安装摘要、长度、时间及状态。本次原始逐包审计 JSON 以 gzip 无损保存（解压后字节未改动），避免大量文件摘要撑大仓库。核验逻辑随后整理为 [scripts/audit_carla_assets.py](../../../scripts/audit_carla_assets.py)，不重复执行真实大包检查。

Town12 专属内容在额外包中为 1,395 个文件，Town13 为 1,651 个文件，全部相同。每张地图另有安装中的 `TownXX.Package.json`（来自主包），所以目录统计分别是 1,396 和 1,652 个文件。两个地图没有零字节文件或缺少 `.uasset/.umap` 搭配的孤立 `.uexp`。文件路径含地图名的统计不包括无地图名的共享资产；全包比对包括这些共享资产。

关键地图文件 SHA256 和目录元数据见 [installed-map-metadata.json](installed-map-metadata.json)。

主包补充检查同样完成，gzip CRC 通过，耗时 94.82 秒。34,246 个常规文件中 28,006 个与原主包直接相同，另外 6,240 个全部是官方额外包提供的新内容，安装内容与额外包摘要一致。没有无法解释的差异或缺失文件。这是正常的额外包覆盖关系，不能把 6,240 个差异误报为损坏。

两包路径去重合并后共 44,711 个常规文件，全部符合“主包 + 官方额外包覆盖”的最终安装内容。见 [base-package-full-audit.json.gz](base-package-full-audit.json.gz) 和 [combined-installation-audit.json](combined-installation-audit.json)。两个归档均无重复路径。

两个压缩审计报告均已重新读取验证 gzip 和 JSON 可读性，并从条目重算合并路径、差异覆盖关系及数量。可用 Python 标准库直接查看，无需解压到安装目录：

```python
import gzip, json
with gzip.open("docs/testing/town13-investigation/additional-maps-full-audit.json.gz", "rt") as file:
    audit = json.load(file)
print(audit["regular_file_count"], audit["matched"], audit["gzip_crc"])
```

## 上游已知打包问题的核对

官方曾确认 0.9.15 额外包包含重复文件，2024-05-28 关闭了修复记录。本机包时间为 2024-05-27，实际全扫描无重复条目，与更新后的官方存储对象一致，不能把旧问题直接当成本机损坏原因。[上游 issue #7049](https://github.com/carla-simulator/carla/issues/7049)

官方 release 链接在 2024 年初曾把显示为 0.9.15 的额外包错误指向 0.9.14。本机包与明确以 `AdditionalMaps_0.9.15.tar.gz` 命名的官方对象一致，并包含 0.9.15 新增的 Town13，当前没有误用旧包的证据。[上游 issue #7054](https://github.com/carla-simulator/carla/issues/7054)、[0.9.15 release](https://github.com/carla-simulator/carla/releases/tag/0.9.15)

本机 `ImportAssets.sh` 使用 `tar --keep-newer-files -xvf`。这类策略理论上可能保留人为修改过且时间较新的文件，但本次全部额外包文件字节一致，未出现该情况。官方安装说明要求下载与 CARLA 版本对应的额外包，在根目录运行导入脚本。[官方安装文档](https://carla.readthedocs.io/en/0.9.15/start_quickstart/#import-additional-assets)

## 复查命令

正式工具只使用 Python 标准库，无需 CARLA、NumPy 或网络。按主包、额外包的参数顺序覆盖，归档内重名采用最后一个文件。使用流式解压、1 MiB 哈希读块和临时 SQLite 索引限制内存，默认只输出摘要及最多 100 项差异；`--manifest` 可选输出完整逐文件 JSONL。它不解压、不修复安装，也拒绝把报告写入 CARLA 安装目录。常规文件以外的归档成员统计但不校验；安装中额外的用户文件不作为损坏报告。没有常规文件的归档不能通过。摘要先原子写入 `running`，结束后原子替换为最终状态；manifest 在完整写入后才发布，输出路径即使是输入的硬链接也不会截断输入内容。

以下命令只读安装。全量检查有磁盘读取和解压开销，避免与性能测量同时运行。

```bash
nice -n 10 ionice -c 2 -n 7 python scripts/audit_carla_assets.py \
  --carla-root /home/czh/software/carla \
  --base-archive /home/czh/software/carla/CARLA_0.9.15.tar.gz \
  --extra-archive /home/czh/software/carla/Import/AdditionalMaps_0.9.15.tar.gz \
  --output /tmp/carla-assets-audit.json
```

如需完整文件摘要，加 `--manifest /tmp/carla-assets-hashes.jsonl`。可重复 `--extra-archive`，后者优先。退出码为 `0` 检查通过、`1` 安装有差异、`2` 输入或归档无效。摘要不会把 gzip CRC 失败当作安装成功。

正式工具的 14 个小型 tar.gz 测试全部通过，覆盖损坏 CRC、缺失、篡改、主包/额外包覆盖、归档重复条目、多额外包顺序、不安全归档路径、安装路径逃逸、输出保护、标准 `./` 根目录、空归档拒绝和原子输出/硬链接保护。详见 [tests/test_asset_audit.py](../../../tests/test_asset_audit.py) 和 [asset-audit-tests.xml](asset-audit-tests.xml)。这组测试不访问真实 CARLA 或联网。

此次官方对象身份复算另由 [verify_official_archive.py](verify_official_archive.py) 记录，可按需复查，**该辅助脚本会联网 HEAD 查询官方存储**：

```bash
python docs/testing/town13-investigation/verify_official_archive.py \
  /tmp/official-archive-identity.json \
  /home/czh/software/carla/Import/AdditionalMaps_0.9.15.tar.gz \
  /home/czh/software/carla/CARLA_0.9.15.tar.gz
```

未验证项：未通过 UE 编辑器逐一解析所有资产引用；未验证新下载的独立副本（已有官方存储 ETag 一致证据）；未对存储介质或 RAM 做硬件压力测试；以上完整性检查不构成运行稳定性验收。
