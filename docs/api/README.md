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

服务代码修改后，在仓库根目录重新生成并检查：

```powershell
python scripts/export_contracts.py
python scripts/export_contracts.py --check
```

`--check` 在文件过期时以退出码 1 结束，可以加入 CI。契约由代码生成；按原计划 A1，由架构负责人 Yang Dongwei 审阅。
