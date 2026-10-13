# 本机环境服务协议（版本 1）

`easycarla-client` 是独立安装包，只依赖 NumPy 与 posix_ipc。支持同机 Linux、同一用户、每个实例一个服务与一个控制客户端，一次一个请求。协议不使用 socket、pickle 或忙等队列。CARLA 自身的 RPC 属于环境后端，和训练端通信分开。

```python
from easycarla_client import CarlaEnvClient, copy_observation

with CarlaEnvClient("carla_01", timeout=30.0) as env:
    obs, info = env.reset(seed=7)
    saved = copy_observation(obs)
    obs, reward, terminated, truncated, info = env.step([0.0, 0.2, 0.0])
    static = env.get_static(obs["map_id"])
```

安装：`python -m pip install ./packages/client`。客户端环境不需要安装环境主包、CARLA、Scenic、Bench2Drive、Torch。服务端从根目录安装主包前先安装同一版本客户端。

启动：`easycarla serve --instance carla_01 --config configs/scenic.yaml`。也可调用 `python -m easycarla.service.cli serve ...`。服务默认读取 YAML；`.json` 使用 JSON。`backend: fake` 明确选择仅用于协议测试的假后端，没有驾驶仿真能力；`backend: carla` 使用环境运行内核。

## 布局和发布顺序

每个实例有三个固定容量的 POSIX 共享内存对象及两个命名信号量：

| 对象 | 默认总容量 | 用途 |
|---|---:|---|
| request | 1 MiB | 一个操作及参数 |
| response | 32 MiB | 一次结果、观测或结构化错误 |
| static | 16 MiB | 按地图内容 ID 获取的静态数据 |
| request_ready | — | 完整请求写入后通知服务 |
| result_ready | — | 完整结果写入后通知客户端 |

配置 `ipc.request_bytes/response_bytes/static_bytes` 修改总容量。每个槽前 256 字节为 header，容量包含 header；运行时不扩容。结果容量不足会结束当前回合并返回 `result_encoding_failed`，该命令可能已经执行，不构造正常训练转移。请求容量不足在发布前本地报错，不消费序号。

Header 为固定小端格式 `<8sII16s16sQQQ32s32s`，剩余字节保留。依次为：magic `ECRLIPC\0`、协议版本、READY 状态、16 字节服务 epoch UUID、16 字节会话 UUID、uint64 请求序号、uint64 有效载荷长度、uint64 槽净容量、32 字节 ASCII 操作码及 32 字节载荷 SHA-256。

载荷是 uint32 JSON 长度、UTF-8 JSON、填充至 64 字节边界、连续数组数据。数组描述包含 dtype（含字节序）、shape、数据区偏移和有效字节数。数值及 bool 数组支持零长度、零维、非连续输入；不支持 object 数组。字典、元组和数组引用采用显式树节点，普通业务字典可使用任意字符串键。JSON 不接受 NaN/Infinity，数值数组保留其数值表示。

写方先完成载荷与 header，再 `release()` 对应信号量。读方必须先 `acquire()`，然后校验协议、容量、epoch、session、seq、操作和载荷摘要，不能轮询 header 把未发布内容当结果。STATIC 先写 static，后发布 response 确认；客户端核对两者属于同一请求。

## 会话与一次执行

资源名基于 UID 与实例名摘要，实例名为 1–64 位 ASCII 字母、数字、`_ . -`。资源权限 0600；生命周期目录 `/tmp/easycarla-<uid>` 权限 0700。两个永久保留 inode 的文件锁管理服务实例和控制客户端归属。锁文件的 PID、epoch、session 仅是归属信息，动作、观测和业务元数据均经过共享内存。

新服务独占获取实例锁后，才能回收同名残留资源、使用 `O_CREX` 创建资源并声明 ready。真实所有权申请允许至多 250 ms 的短暂锁竞争重试，避免把 liveness 探测临时持锁误判为控制客户端；活实例持续占用则报错。服务启动生成新 epoch，不沿用旧进程的动作执行保证。

新客户端先取得客户端锁并登记新的随机会话，然后等待服务发布 `CLAIMED`。服务在完成或丢弃前任的请求、关闭前任回合并清空旧通知后，才发布 CLAIMED，最后在服务锁的固定长度记录中标记新会话归属。客户端先等此归属标记，才读取握手信号，避免抢走旧结果通知时读到正在改写的槽。客户端在 CLAIMED 之前不能写 request，因而不会覆盖正在执行或尚未读取的旧请求。外部策略可以计算任意长时间；服务通过锁归属判断断连，不以空闲时长驱逐客户端。

CLAIMED 序号为 0，业务请求从 1 严格递增。服务缓存当前会话最后一次完整结果：

- 同序号、同操作、同载荷返回缓存结果，不再次执行。
- 同序号但内容不同返回 `sequence_conflict`。
- 更旧或跳跃序号返回 `invalid_sequence`。
- 旧 epoch 或旧会话返回 `stale_session`。

客户端不自动重试任何已提交请求。请求提交前发现服务停止，抛出 `ServiceUnavailable`；提交后超时、服务死亡、版本/摘要/序号不符，抛出 `UnknownOutcomeError`，并使该连接不可继续执行请求。用户需要关闭该客户端，建立新会话并显式 reset。不能跨服务 epoch 重放 step 来“恢复”。

## 内存有效期与静态缓存

默认 `copy=True`：所有返回数组都是独立副本，后续 step/reset/close 不改变历史观测。

显式 `CarlaEnvClient(..., copy=False)`：返回数组是只读借用视图，有效期仅至下一次请求提交之前；包括 `capabilities()` 和实验查询在内的任何请求都可能覆盖 response。不要把借用视图直接加入经验回放。`copy_observation(obs)` 或 `env.copy_observation(obs)` 对嵌套数组显式深复制。

`get_static(map_id)` 始终返回独立副本，并按 ID 在客户端缓存，命中缓存不发送请求。调用者应使用服务返回的内容 ID；不要以未经版本化的地图显示名称作缓存键。关闭时仍被数组引用的 mmap 在引用释放后回收，客户端不会 unlink 服务对象。

## 关闭、故障与清理

`env.close()` 正常发送 CLOSE，服务仅调用 `backend.close_episode()`；客户端释放自身连接。服务进程与外部 CARLA 继续存在，其他实例不受影响。若已有未知请求结果，close 不再发布 CLOSE，以免覆盖前一命令；只释放连接，服务在完成当前请求后按失联处理回合。

客户端 SIGKILL 后文件锁自动释放，服务发现后清理其回合。服务 SIGKILL 的 POSIX 名称可能残留，下次独占取得实例锁的启动会回收；也可执行 `easycarla cleanup --instance carla_01`，该命令拒绝清理仍被活服务占用的实例。锁文件不能删除或替换 inode。

后端验证失败返回 `RemoteError(code="invalid_request")`；后端异常返回带异常类型与 `episode_faulted` 的结构化错误，不返回观测奖励五元组。后端必须保证验证失败发生在仿真推进之前；已进入仿真阶段的失败必须使用运行时故障异常，不使用输入验证 ValueError。无法安全清理或无法解释的损坏请求使服务停止，客户端报告结果未知。

客户端不支持跨 fork 复用；请在子进程内新建客户端。同一个客户端禁止并行调用。析构函数只释放连接，不隐式发送业务请求。

## 操作与时间测量

| 客户端 | 操作 | 后端接口 |
|---|---|---|
| reset(seed, options) | RESET | reset(seed=None, options=None) |
| step(action) | STEP | step(action) |
| step_trajectory(trajectory, execute_seconds) | TRAJECTORY | step_trajectory(trajectory, execute_seconds=None) |
| capabilities() | CAPABILITIES | capabilities() |
| get_static(map_id) | STATIC | get_static(map_id=None) |
| experiment.method(**kwargs) | EXPERIMENT | experiment(method, **kwargs) |
| close() | CLOSE | close_episode() |

服务退出调用 `shutdown()`，与客户端结束回合分开。实验命令只允许显式公开的方法名；实际允许列表、能力和禁用规则由运行内核负责，传输层不生成候选、不扩展分支、不计算标签。

`env.last_timings` 包含 `roundtrip_seconds`、`backend_seconds` 与 `transport_and_copy_seconds`（前两者之差，包含调度、编码、共享内存及副本复制）；它不是纯内存带宽数值。后端分别报告物理 tick 与传感器等待等时间。测试/基准应分别统计各项 p50/p95，并标明 fake 或真实 CARLA；fake 后端不能证明仿真性能或正确性。

固定观测的本地编码/复制微基准可使用：

```bash
python scripts/measure_codec.py --instance carla_01 --iterations 200 --output codec-report.json
```

该命令只用独立客户端获取一次 reset 观测，关闭会话后对同一份数据重复编码与校验解码复制。报告含载荷尺寸、backend/scenario、`encoding_ms` 和 `validation_decode_copy_ms` 的 p50/p95。前者不含发布时的摘要计算；后者包含 header/容量与 SHA-256 校验、JSON 解码和数组副本。它不测量 IPC 往返或仿真时间；这些指标应另用 `scripts/measure_service.py`。
