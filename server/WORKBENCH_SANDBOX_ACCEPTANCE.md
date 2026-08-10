# 托管 Sandbox 恢复与验收故障注入

## 生产恢复接口

`GET /workbench/sandbox?conversation_id=<uuid>` 返回当前 Workbench 身份、客户、用户、
App 和会话共同拥有的唯一本地 Sandbox 句柄。ADP 服务内部实际路由是
`GET /sandbox`，`/workbench` 由同源反向代理添加。

该接口只读本地数据库，不调用 `describe` 或任何其他 Provider API。不存在记录，或者
customer、new-api user、App、App Profile、配置版本、授权 epoch、绑定、账号、会话任一
维度不匹配时，统一返回 `404 sandbox_not_found`。响应与普通 Sandbox 投影相同，只包含：

```json
{
  "sandbox_id": "sbx_...",
  "conversation_id": "...",
  "status": "provider_unknown",
  "timeout_seconds": 600,
  "expires_at": null,
  "created_at": "...Z",
  "updated_at": "...Z"
}
```

它用于浏览器丢失首次创建响应后的生产恢复，不返回 Provider instance locator、Token、URL
或原始错误。

## 验收环境故障注入

故障注入用于证明以下不变量：Provider 已成功执行 `Start`、但 ADP 丢失响应时，同一请求
重放只能复用本地 `provider_unknown` 记录，不能再次调用 Provider `Start`。

该能力默认关闭，并有四重门禁：

1. `WORKBENCH_SANDBOX_ACCEPTANCE_FAULTS_ENABLED=true`；
2. `WORKBENCH_DEPLOYMENT_TIER=acceptance`，在 production/staging/development/test 均拒绝启动；
3. 已有可信 Workbench SSO、有效 owner/App context、`sandbox` capability 和最近认证；
4. 请求 Host/Origin 必须与 `WORKBENCH_PUBLIC_BASE_URL` 同源，并提供独立挂载文件中的验收 Token。

生产配置必须保持：

```dotenv
WORKBENCH_DEPLOYMENT_TIER=production
WORKBENCH_SANDBOX_ACCEPTANCE_FAULTS_ENABLED=false
```

隔离验收环境额外配置：

```dotenv
WORKBENCH_DEPLOYMENT_TIER=acceptance
WORKBENCH_SANDBOX_ACCEPTANCE_FAULTS_ENABLED=true
WORKBENCH_SANDBOX_ACCEPTANCE_TOKEN_FILE=/run/secrets/workbench-sandbox/acceptance-token
WORKBENCH_SANDBOX_ACCEPTANCE_REAUTH_SECONDS=300
```

Token 文件必须是当前进程 UID 所有、权限 `0400`/`0600`、非符号链接、32–256 字节的
ASCII Header 安全随机串。不得把内容写入 `.env`、请求体、日志或报告。

### 触发响应丢失

在正常 `POST /workbench/sandbox` 请求上增加：

```http
Origin: https://gateway.example.com
X-Workbench-Acceptance-Token: <mounted-token>
X-Workbench-Acceptance-Run-Id: <uuid>
X-Workbench-Acceptance-Fault: provider_start_response_lost
X-Workbench-CSRF: <same-value-as-claw_workbench_csrf-cookie>
```

请求体仍是正常创建契约：

```json
{"conversation_id":"<uuid>","timeout_seconds":60}
```

Provider 成功返回实例后，服务先持久化一条 acceptance evidence，再故意丢弃响应并把本地
Sandbox 置为 `provider_unknown`。重放同一创建请求时，创建逻辑在 Provider 调用前返回现有
记录，因此计数保持 `1`。

### Evidence 查询与清理

以下路由均为 ADP 内部验收面，不能写入客户 API 文档；外部部署路径均带 `/workbench`：

| 方法 | ADP 路径 | 用途 |
| --- | --- | --- |
| `GET` | `/sandbox/acceptance/provider-start-count?acceptance_run_id=...&conversation_id=...` | 返回精确 Provider Start 次数 |
| `GET` | `/sandbox/acceptance/instances?acceptance_run_id=...&conversation_id=...` | 返回无 locator 的实例证据与清理状态 |
| `POST` | `/sandbox/acceptance/cleanup` | 停止该 run + conversation 下记录的全部 Provider 实例 |

查询请求也必须带 `Origin` 和 `X-Workbench-Acceptance-Token`。清理请求还必须带 Workbench
CSRF Header/Cookie，请求体为：

```json
{"acceptance_run_id":"<uuid>","conversation_id":"<uuid>"}
```

计数响应：

```json
{
  "acceptance_run_id": "<uuid>",
  "conversation_id": "<uuid>",
  "provider_start_count": 1
}
```

实例与清理响应只返回本地 `sandbox_id`、generation、故障模式、清理状态/次数和时间；
数据库中为清理而保留的 `ProviderInstanceId` 永不投影。清理逐一处理所有记录，单实例失败
不会跳过其余实例；`complete=true` 仅表示全部记录已确认 `stopped` 或 `not_found`。

验收结束必须调用清理接口，并再次查询实例列表确认所有 `cleanup_status` 为 `stopped` 或
`not_found`。验收记录保留为审计证据，不通过清理接口删除。
