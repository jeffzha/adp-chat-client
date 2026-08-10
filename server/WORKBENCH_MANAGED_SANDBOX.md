# Workbench 托管沙箱约束与实现契约

## 1. 边界

托管沙箱是 ADP Workbench fork 的可选扩展，默认关闭。它不改变 new-api
的认证、余额、模型计费和渠道转发路径。生产启用必须同时满足：

1. 服务端 `WORKBENCH_SANDBOX_ENABLED=true`；
2. 应用配置和客户套餐的 capability 交集包含独立的 `sandbox`；
3. 文件操作还要求 capability 交集包含 `files`；
4. 当前绑定、应用快照和会话所有权仍有效。

`sandbox` 不复用 `tools`。开放沙箱不会隐式开放 Agent 工具，反之亦然。
停用或欠费用户不能执行创建、生命周期、Shell 和文件写入等变更操作。

一个沙箱的唯一作用域固定为：

```text
(customer_id, new_api_user_id, application_id, conversation_id)
```

`conversation_id` 必须是系统中已持久化且属于当前账户、绑定、客户、应用、
Provider App、App Profile 和配置版本的会话，不能由浏览器任意填写。数据库唯一
约束和服务端所有权校验共同防止同客户不同用户、同用户不同会话及蓝绿实例串用。

## 2. 安全约束

- 只允许腾讯 AGSX `NetworkMode=SANDBOX`、`AuthMode=TOKEN`、
  `Persistent=false`。`PUBLIC`、VPC/内部网络、无认证和持久实例均拒绝。
- 不存储、不返回 Provider Token、直连 URL 或网络端口；客户端只看到本地
  `sbx_...` 标识、状态、超时和时间戳。
- Tool ID 和 Tool Name 至少配置一个。上游响应必须同时返回完整 Tool ID/Name，
  并逐项精确匹配本地已配置的字段，防止启动到错误工具模板。
- 源命令、路径、文件、输出、运行时间、实例寿命、并发和启动速率均有服务端
  硬上限；套餐只能收紧，不能突破硬上限。
- 日志和审计不记录命令正文、文件内容、密钥、Token、直连地址和 Provider 原始
  错误消息，只记录作用域标识、操作类型、结果、状态变化及白名单错误码。
- 所有请求继续经过 Workbench SSO；变更请求还要求同源 CSRF Token。代码执行还要求
  当前 browser session 的认证时间不超过 `WORKBENCH_SANDBOX_REAUTH_SECONDS`。

## 3. 当前可用能力

| 能力 | 当前状态 | 说明 |
| --- | --- | --- |
| 创建、复用、查询、暂停、恢复、停止 | 可用 | 走腾讯官方控制面 SDK |
| 非交互 Shell（同步/NDJSON 流式） | 可用 | 强制超时和进程侧输出上限 |
| 工作区文件读写 | 可用 | 只允许 `/workspace` 下相对 POSIX 路径 |
| 代码解释器 | 独立开关 | `WORKBENCH_SANDBOX_CODE_ENABLED=true` 后使用官方 E2B `run_code` |
| 交互 PTY | 独立开关、待真实验收 | 只经同源 WebSocket 代理；不允许浏览器直连 Provider |

代码解释器默认关闭。`e2b-code-interpreter==2.9.0` 会在 callback 前把完整 Jupyter
frame 加入 `Execution`，因此不能在 ADP Web 进程内直接调用后再截断。实现把官方
`AsyncSandbox.connect(...).run_code(...)` 放进一次一进程的 Linux worker；worker
启动即设置 `RLIMIT_AS`、`RLIMIT_CPU`、禁用 core dump 并限制文件描述符。累计输出由
callback 逐帧计数，单帧即使在 callback 前物化也只能消耗 worker 的硬内存额度，不能
拖垮主进程。worker stdout 使用固定上限协议，父进程最多读取
`WORKBENCH_SANDBOX_MAX_OUTPUT_BYTES + 64 KiB`。OOM、协议异常、超时和取消都停止
整个远端 sandbox，并在无法确认停止时保留 `provider_unknown`。浏览器只收到
`stdout`、`stderr`、纯文本 `results` 和裁剪后的 `error`；HTML、图片、traceback、
Provider ID/URL/Token 不返回。`GET /sandbox/config` 仅在总开关、代码独立开关和
当前 App `sandbox` capability 同时满足时返回 `code_execution_enabled=true`。
父进程不会把自身环境复制给 worker；子进程环境白名单只含一次性的 AGSX API Key、
固定 Python/locale 设置和由当前源码位置计算出的 `PYTHONPATH`。worker stderr 最多
读取 64 KiB 后丢弃，不返回浏览器、不写应用日志。

PTY 默认关闭。启用后，`POST /sandbox/{id}/pty` 只签发 30 秒、一次性、数据库仅存
SHA-256 的短票据；浏览器在 `Sec-WebSocket-Protocol` 中提交固定协议和票据，票据不进
URL、query、Cookie 或访问日志。WebSocket 握手前中间件校验同源 Origin/Host、当前
Workbench Session、owner/App/auth snapshot、RUNNING generation 并原子消费票据。
服务端固定以 `user=user`、`cwd=/workspace` 创建 E2B PTY；浏览器只收到二进制终端
输出和裁剪后的 `ready/exit/error` 控制帧，看不到 `ark_`、Provider InstanceId、PID、
直连地址或原始错误。输入帧、速率、累计输入/输出、队列、尺寸、时长和全局/客户/用户
并发均有服务端硬限制；静默连接也持续复核授权并写心跳。正常关闭只杀 PTY 并保留
sandbox；断线、撤权、超限、Provider 歧义或清理不确定会杀 PTY 后停止整个 sandbox。
Blue/Green reaper 通过共享 PostgreSQL 状态接管 stale session，不恢复或回放旧终端。

## 4. Provider 契约

控制面仅使用腾讯官方 `tencentcloud-sdk-python-ags`（API 版本
`2025-09-20`）：

- `StartSandboxInstance`（稳定 `ClientToken`、`Timeout`、`AuthMode=TOKEN`）；
- `DescribeSandboxInstanceList`；
- `PauseSandboxInstance`；
- `ResumeSandboxInstance`；
- `StopSandboxInstance`。

数据面只使用官方 E2B 兼容 SDK 的公开接口：`AsyncSandbox.connect`、`run_code`、
`commands.run`、流式命令、`files.read/files.write`，以及 PTY 的
`create/send_stdin/resize/kill`。不硬编码未公开的传输路径。
Provider 以接口注入，自动化测试只使用本地 fake，不访问真实云服务。

启动或查询得到的实例必须满足：实例 ID 格式合法、返回 ID 与请求 ID 精确一致、
生命周期状态属于显式白名单、网络/认证/持久化/Tool 身份符合上述约束。任何字段
缺失、未知状态或身份不匹配都进入 `provider_unknown`，继续占用容量，等待人工或
查询协调，不能被当作已停止释放容量。

## 5. 幂等、并发与状态机

创建事务按 scope、客户、用户获取排序后的 PostgreSQL advisory lock，再完成过期
处理、容量计算和 provisioning 行写入，因此蓝绿实例不会超额分配。所有
`starting` 行都计入容量，即使 lease 已过期；只有完全相同的 owner scope 可以
接管该行重试。

Provider `ClientToken` 由独立 HMAC 密钥、版本化长度前缀 scope 和随机 generation
确定性生成。数据库只保存 Token 哈希和非秘密 generation。进程在 Provider 已接收
请求、但本地结果尚未提交时崩溃，接管者会以同一 Token 重试并取回同一实例；终态
重建使用新 generation，避免误复用已清理的 Provider 实例。

生命周期和异常停止采用两阶段 compare-and-swap（CAS）：

1. 先以 `Version` CAS 持久化 `pausing/resuming/stopping` 意图、执行实例和短 lease；
2. 提交事务后才调用 Provider，调用期间不持有数据库行锁；
3. 返回后再次以期望版本 CAS 写入结果并清除 lease；
4. 同期查询看到有效的过渡 lease 时只返回本地状态，不抢占版本；
5. 超时、取消或歧义失败写入 `provider_unknown`，不会虚构成功终态。

`StopSandboxInstance` 的接受响应不等于实例已停止。服务端随后 describe，只有确认
上游终态才写 `stopped`；否则保持 `stopping` 或 `provider_unknown`。超时、取消、
输出超限的自动停止同样先 CAS 写停止意图，再发上游请求，避免旧协程停止新状态。

过期行转为 `stopped` 时递增 `Version` 并写审计。终态行按
`WORKBENCH_SANDBOX_TERMINAL_RETENTION_DAYS` 清理。每次启动尝试都写数据库审计，
据此执行每用户和每客户每分钟速率限制，蓝绿进程共享同一计数。

## 6. 输出、文件和取消

- Shell 命令由沙箱内包装器限制 stdout+stderr 总字节数；达到上限会杀死该进程组，
  不依赖 SDK 先把无限输出传回服务端。
- `run_code` worker 同时限制 stdout、stderr、文本/富媒体 result 和错误 traceback 的
  累计字节；富媒体只参与上限计算并立即释放，不进入浏览器响应。worker OOM 或异常
  退出按远端状态未知处理，不能把该实例重新用于下一次有副作用执行。
- 流式桥接使用固定容量队列和背压；浏览器断开会关闭上游迭代器、停止整个沙箱并
  释放运行时并发 lease。
- 同步命令、流式命令、文件读写的成功与失败都写不可含秘密的审计记录；输出超限、
  超时、取消和 Provider 歧义另带状态转换审计。
- 文件读写使用独立运行时并发 lease。上传先检查 `Content-Length`，再边读边计数；
  超过套餐或硬上限立即拒绝。下载上限传入 Provider，并在接收过程中截断检查，不能
  先无限读入内存。
- 路径必须是相对 POSIX 路径；绝对路径、反斜杠、控制字符、`..` 和共享挂载均拒绝。

## 7. 对外 API

所有路径位于现有 Workbench API 前缀下。

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `GET` | `/sandbox/config` | 非秘密功能开关 |
| `POST` | `/sandbox` | 创建或复用当前 scope 实例 |
| `GET` | `/sandbox?conversation_id=...` | 仅从本地恢复当前 owner scope 的唯一句柄，不调用 Provider |
| `GET` | `/sandbox/{id}?conversation_id=...` | 查询/协调状态 |
| `POST` | `/sandbox/{id}/pause` | 暂停 |
| `POST` | `/sandbox/{id}/resume` | 恢复 |
| `POST` | `/sandbox/{id}/stop` | 停止 |
| `POST` | `/sandbox/{id}/shell` | 有界非交互 Shell |
| `POST` | `/sandbox/{id}/shell/stream` | NDJSON Shell 流 |
| `GET` | `/sandbox/{id}/files` | 有界二进制文件读取 |
| `PUT` | `/sandbox/{id}/files` | 有界二进制文件写入 |
| `POST` | `/sandbox/{id}/code` | 有界代码执行；默认关闭且要求近期认证 |
| `POST` | `/sandbox/{id}/pty` | 签发一次性 PTY WebSocket 票据；默认关闭、要求近期认证 |
| `GET`/WebSocket | `/sandbox/{id}/pty/connect` | 同源双向 PTY；票据仅放 WebSocket subprotocol |

路由安全中间件按 HTTP 方法和精确路径模板 fail closed；未列入 allowlist 的变体不能
因为前缀相同而绕过。客户端错误只返回白名单错误码，不透传 Provider 原文。

## 8. 部署设置

启用时必须设置：

- `WORKBENCH_SANDBOX_ENABLED=true`；
- `WORKBENCH_SANDBOX_CODE_ENABLED=true`：仅在真实代码执行验收通过后打开；
- `WORKBENCH_SANDBOX_PTY_ENABLED=true`：仅在目标地域完成真实 PTY 验收后打开；
- `WORKBENCH_INSTANCE_ID`：每个蓝/绿部署实例唯一且稳定；
- `WORKBENCH_AGSX_REGION`；
- `WORKBENCH_AGSX_DOMAIN=<region>.tencentags.com`；
- `WORKBENCH_AGSX_CONTROL_ENDPOINT=ags.tencentcloudapi.com`；
- `WORKBENCH_AGSX_TOOL_ID` 和 `WORKBENCH_AGSX_TOOL_NAME` 至少一个；
- `WORKBENCH_AGSX_API_KEY_FILE`：内容为 `ark_...`；
- `WORKBENCH_AGSX_CAM_SECRET_ID_FILE`；
- `WORKBENCH_AGSX_CAM_SECRET_KEY_FILE`；
- `WORKBENCH_SANDBOX_CLIENT_TOKEN_KEY_FILE`：蓝绿共享、独立随机、至少 32 字节；
- `WORKBENCH_SANDBOX_NETWORK_MODE=SANDBOX`；
- `WORKBENCH_SANDBOX_AUTH_MODE=TOKEN`。
- `WORKBENCH_SANDBOX_CODE_WORKER_MEMORY_BYTES`：每个 SDK worker 的 Linux
  `RLIMIT_AS`，默认 512 MiB；
- `WORKBENCH_SANDBOX_REAUTH_SECONDS`：代码执行可接受的最近认证时间，默认 300 秒。

密钥只能通过绝对路径文件挂载，不能写进 `.env` 或数据库。读取器拒绝符号链接、
非普通文件、非当前进程 UID 所有的文件、POSIX group/other 权限以及大于 4 KiB 的
文件，并在 `open(O_NOFOLLOW)` 后再次校验文件描述符。容器中建议所有者 UID 10001、
权限 `0400`。启动前 readiness 校验会验证全部密钥、域名、端点、Tool 和安全模式；
失败时服务不应接流量。

其余并发、时长、输出、文件、启动速率和终态保留变量以 `server/.env.example` 为准。
真实云验证必须在隔离的预生产客户/应用下执行；仓库测试不会读取生产密钥或调用云。

生产恢复接口以及默认关闭的验收故障注入、证据查询和清理契约见
[`WORKBENCH_SANDBOX_ACCEPTANCE.md`](./WORKBENCH_SANDBOX_ACCEPTANCE.md)。
