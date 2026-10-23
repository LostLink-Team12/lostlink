# API 契约（A1）

本目录由代码生成，供前端、Matching/Delivery 和测试参考。请不要手工修改 JSON 文件。

| 文件 | 内容 |
| --- | --- |
| `venue-service.openapi.json` | UC1 场馆、成员、成员校验内部接口 |
| `item-service.openapi.json` | UC2 物品、上传、导入，以及供 Claim/Matching 使用的内部接口 |
| `matching-service.openapi.json` | UC3 失物报告、检索，以及通知上下文内部接口 |
| `claim-service.openapi.json` | UC5 认领、审批、QR 交接、操作查询 |
| `notification-service.openapi.json` | UC4 通知状态与运营接口 |
| `events.schema.json` | 版本一事件信封，以及十种事件的发布方、是否要求租户和载荷 schema |

路径以 `/internal/v1` 开头的接口仅供服务间调用，并要求指定的机器客户端和 scope，不经公网网关暴露。通用约定（access token、幂等 key、If-Match、202 操作轮询、错误体）见[后端接入指南](../team-alignment.md)。

业务接口的错误响应复用 `libs` 中的 `ErrorResponse`：

```json
{
  "error": {"code": "version_mismatch", "message": "Resource version has changed"},
  "request_id": "b7e9d96f-5eac-4ec0-81e4-c68543c805ba"
}
```

- 401、403、422、500、503 为公共业务错误；404、409、412、428 按接口行为声明，Matching 另声明请求体过大的 413。
- 请求校验失败的 422 使用同一错误体，`error.details` 列出 `{ "location": ["body", "字段名"], "type": "错误类型" }`，不包含输入值；业务校验错误可以不包含 `details`。
- `X-Request-ID` 对应响应体的 `request_id`；401 包含 `WWW-Authenticate: Bearer`，409 仅在 `error.code=operation_in_progress` 时包含 `Retry-After`。
- 428 表示缺少该接口要求的 `Idempotency-Key` 或 `If-Match`；412 表示版本已过期，需要重新读取资源。
- `/health`、`/ready` 是运维接口，保持各自响应结构；`/ready` 的 503 不使用业务错误体。

服务代码修改后，在仓库根目录重新生成并检查：

```powershell
python scripts/export_contracts.py
python scripts/export_contracts.py --check
```

`--check` 在文件过期时以退出码 1 结束，可以加入 CI。契约由代码生成；按原计划 A1，由架构负责人 Yang Dongwei 审阅。
