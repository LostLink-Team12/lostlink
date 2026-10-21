# 后端接入指南

更新时间：2026-10-21。维护人：JIANG YIBAI。

本文汇总 Phase 1 后端已实现的服务、对外契约，以及各成员需要完成的接入工作。这里记录的是后端采用的约定和接入要求，并不表示队友已经确认，也不表示真实云端联调已经完成。接口细节以 [API 契约](api/README.md)和各服务 README 为准。

## 1. 服务现状

| 服务 | 用例 | 状态 | 运行说明 |
| --- | --- | --- | --- |
| venue-service | UC1 场馆入驻、员工角色、成员实时校验、Cognito 令牌触发器 | 已实现，本地验证 | [README](../services/venue-service/README.md) |
| item-service | UC2 物品登记、照片封存、批量导入、认领预留 | 已实现，本地验证 | [README](../services/item-service/README.md) |
| matching-service | UC3 失物报告、文字/照片检索、CLIP embedding、UC4 匹配产生 | 已实现，真实 CLIP 本地验证 | [README](../services/matching-service/README.md) |
| claim-service | UC5 认领、审批、QR 交接 | 已实现，与真实 Venue/Item 本地集成验证 | [README](../services/claim-service/README.md) |
| notification-service | UC4 匹配邮件提醒 | 已实现，SES 以捕获发送器替代 | [README](../services/notification-service/README.md) |
| Partner API / Webhooks | Seed 2 合作方接口 | Phase 2，未实现 | 见第 5 节 |

本地验证使用 Docker PostgreSQL/pgvector 与 LocalStack（S3/SNS/SQS）。真实 Cognito、SES、AWS IAM、EKS 部署与 5,000 张照片云端吞吐尚未验收。

## 2. 通用接口约定（前端与其他服务）

- 使用 Cognito **access token**，拒绝 ID token。租户只来自令牌中的 `custom:tenant_id`，客户端不能提交 `tenant_id` 或 `owner_id`。角色组为 `venue_admin`、`venue_staff`、`owner`、`platform_admin`；令牌触发器会给所有用户附加 `owner`。
- 创建或动作型 POST 必须带 `Idempotency-Key`（8—128 个可打印 ASCII 字符）。同 key 同请求会重放原结果，同 key 不同请求返回 409。修改已有资源还要带 `If-Match`，值为响应 `ETag`（如 `"3"`）：缺失返回 428，版本过期返回 412。
- 错误体统一为 `{"error":{"code","message"},"request_id"}`。前端按 `error.code` 分支处理，并区分 401、403、404、409、412、422、428、503。
- 异步动作返回 202 和 `operation_id`，需要轮询对应的操作接口。**202 不代表完成**，物品交接必须等到 `claim_state=handed_over`。
- 列表参数为 `limit`（≤100）和 `offset`，响应带 `has_more`，不提供总数。
- 检索和匹配的 `score` 是 -1 到 1 的余弦相似度，不能展示成概率或百分比。
- `/internal/v1/*` 与 `/metrics` 只供集群内部使用，不能经公网网关暴露。

## 3. 事件与队列

事件格式为版本一信封，载荷禁止多余字段，消费方按 `event_id` 去重。机器可读 schema 见 [events.schema.json](api/events.schema.json)。

| 事件 | 发布方 | 当前消费方 |
| --- | --- | --- |
| `item.registered`、`item.catalog_changed` | item-service | matching-service `consume` |
| `item.embedded`、`item.embedding_failed` | embedding（matching-service tasks） | item-service `consume` |
| `match.found` | matching-service | notification-service `consume` |
| `lost_report.created` | matching-service | 暂无 |
| `venue.membership_changed` | venue-service | 暂无（敏感写入仍实时校验成员） |
| `claim.approved`、`claim.cancelled`、`claim.handed_over` | claim-service | 暂无（Seed 2 Delivery 以后订阅） |

每个消费队列用 SNS 订阅过滤，只接收上表中自己的事件类型，并配置 DLQ（`maxReceiveCount=5`）。消费进程在数据库提交之后才删除 SQS 消息，无效或冲突的消息不会被删除。

## 4. Lu Jingxing：身份、数据库与部署

### 进程与数据库角色

每个服务都有独立的迁移凭据和运行角色。运行角色不能是超级用户，不能有 `BYPASSRLS`，也不能是表的所有者。部署时先运行 `alembic upgrade head`，再启动应用；API 和 worker 容器不注入迁移凭据。镜像统一从仓库根目录构建：`docker build -f services/<svc>/Dockerfile .`。本地角色可以用 `scripts/init-local.sql` 一次创建。

| 服务 | 运行角色 | 进程 |
| --- | --- | --- |
| venue | api、platform、worker、identity | API；`venue_app.workers outbox`；`venue_app.workers identity`；Cognito Pre Token Generation V2 Lambda |
| item | api、internal、worker | API；`item_app.workers tasks / outbox / consume / cleanup` |
| matching | api、internal、worker | API（推理镜像 `INSTALL_ML=true` 并只读挂载权重）；`matching_app.workers consume / tasks / outbox` |
| claim | api、worker | API；`claim_app.workers tasks / outbox` |
| notification | api、admin（可选）、worker | API；`notification_app.workers consume / tasks` |

### 机器客户端与 scope

| 调用方 | scope | 加入接收方白名单 |
| --- | --- | --- |
| item-service | `lostlink/members.check` | Venue `MEMBER_CHECK_CLIENT_IDS` |
| claim-service | `lostlink/members.check`、`lostlink/items.context`、`lostlink/items.reserve` | Venue `MEMBER_CHECK_CLIENT_IDS`、Item `CLAIM_CLIENT_IDS` |
| matching-service（含 embedding） | `lostlink/items.index` | Item `INDEX_CLIENT_IDS` |
| notification-service | `lostlink/matches.notify` | Matching `NOTIFICATION_CLIENT_IDS` |

### Cognito、秘密与 AWS 资源

- 用户池需启用 Pre Token Generation **V2** 触发器（Venue），由它从成员目录写入租户和角色。`platform_admin` 组只能由可信部署引导授予。运营控制台客户端加入 Venue `PLATFORM_CLIENT_IDS` 和 Notification `OPERATOR_CLIENT_IDS`。
- Claim、Notification 和 Venue identity worker 需要目标用户池的 `cognito-idp:ListUsers`。Venue identity worker 另外需要 `AdminUserGlobalSignOut`。
- 秘密：
  - Claim 的 `FIELD_ENCRYPTION_KEYS`，格式为 JSON `{"key-id":"base64 32 字节"}`，并配 `FIELD_ENCRYPTION_ACTIVE_KEY_ID`；
  - Claim 的 `HANDOVER_TOKEN_KEY`（至少 32 字节，base64）；
  - 各服务的机器客户端密钥。

  轮换加密密钥时新增一个 key 并切换当前 key ID，旧 key 保留到旧数据重加密完成。
- S3：照片桶为私有。只有 Item 的封存流程可以写 `sealed/` 前缀，embedding 只能读。CORS 只允许 Venue Portal 的 POST 上传。
- SES：需要验证发件身份，并授予 `ses:SendEmail`（限定该身份）；沙箱期间只能发给已验证的测试收件人。Notification 需要配置 `OWNER_APP_BASE_URL` 和 `SES_FROM_ADDRESS`；dev/prod 环境会拒绝 `EMAIL_SENDER=capture`。
- 伸缩：Matching 的 `tasks` 积压在 PostgreSQL 中，不能只按 SQS 深度把它缩到零（ADR-0002 需据此调整）。
- 探针：`/health` 是存活检查。`/ready` 在缺配置、迁移不匹配或依赖未就绪时返回 503，探针不能假设它总是成功。日志从 `lostlink` logger 输出 JSON。
- 已知告警误报：Item 会对持有超过 5 分钟的预留报 `reservation_recovery_required`。已批准但还没现场交接的认领本来就会长期持有预留，这类告警应结合 Claim 的操作状态过滤；后续计划由 Claim 批准后通知 Item 停止观察。

## 5. Yang Dongwei：匹配、契约与 Seed 2

- Matching 和 Embedding 已在 `matching-service` 中实现（独立进程，共享 `matching` schema）：真实 OpenCLIP ViT-B/32、512 维向量、权重 SHA256 固定。API 和 tasks 必须使用同一份权重。自动匹配阈值 0.25 只是初值，需要用演示数据重新标定。
- Embedding 的成功/失败结果必须原样带回登记事件的 `correlation_id` 和 `item_version`。Matching 只展示 `available` 状态的物品，其他状态保留版本墓碑。
- 通知上下文接口 `GET /internal/v1/matches/{id}/notification-context` 由 Notification 在每次发送前调用复核。`venue_name` 目前为 null，等 Venue 提供公开目录后再填充。
- A1 契约：[docs/api](api/README.md) 由 `scripts/export_contracts.py` 从代码生成。请作为架构负责人审阅，后续修改以代码为准，生成后再提交。
- Seed 2（Phase 2）：
  - Delivery 不能凭 `claim.approved` 读取联系人或地址，必须以失主为指定合作方创建的**交付授权**为依据；
  - 取件必须由场馆扫码确认，`claim.handed_over` 中 `mode=courier` 表示已取件，之后的运输状态由 Delivery 维护；
  - 交付授权、合作方 API 与 Webhook（HMAC 签名、重试、目标地址校验）由后端在 Phase 2 实现；需要提前对齐时联系 Jiang Yibai。

## 6. Zhang Ji：前端流程

- **Venue Portal（UC1、UC2、UC5）**
  - 入驻需要绑定已有的 Cognito 账号；员工角色提升后要刷新 access token。
  - 物品登记流程：创建 → 申请上传 → 直传 S3 POST（不带 Cognito 头）→ 确认上传 → 轮询 `GET /v1/item-operations/{id}` → 物品进入 processing，embedding 完成后才可被检索。物品列表不含参考答案，员工详情页可以看到参考答案。
  - 批量导入使用稳定的 `batch_reference`，可以逐行修复；也可以用 `scripts/import_items.py`。
  - 认领审核：`GET /v1/venue/claims` → 详情 → 批准（202，轮询操作）或拒绝（`answers_incorrect` / `insufficient_detail` / `duplicate` / `other`）。审批同一物品的第二个认领会得到 409 `item_claim_in_progress`；员工不能审批或交接自己的认领（403）。
  - 联系人只在 approved / handover_pending 状态可读。
  - 交接：扫描失主的 QR，把 `qr_payload` 原文作为 `token` 提交，同时带 `If-Match`；返回 202 后轮询，等到 `handed_over` 才把物品交给失主。
- **Owner App（UC3、UC4、UC5）**
  - 报告：`POST /v1/lost-reports`（需要幂等 key），关闭需要 `If-Match`。
  - 检索：`POST /v1/search`，支持 `text` 和/或 `photo_base64`（原始 base64，≤5 MiB，不带 data URL 前缀），以及分类、场馆、拾得时间过滤。候选窗口和实时过滤可能导致结果短页。
  - 邮件提醒中的链接固定为 `OWNER_APP_BASE_URL/matches/{match_id}`，Owner App 需要提供这个页面；`GET /v1/notifications` 可以显示通知状态。
  - 认领：先 `GET /v1/claim-questions?item_id=`，再 `POST /v1/claims`（每题作答，联系人姓名和电话可选；账号邮箱必须已验证，否则 409 `email_not_verified`）。
  - 取消认领原因限 `found_elsewhere` / `no_longer_needed` / `submitted_in_error` / `other`。
  - 批准后用 `POST /v1/claims/{id}/handover-token` 获取 QR 内容；同一 key 只能在 10 分钟内重放，过期后重新签发（旧码立即失效）。
- **测试数据**：至少两个场馆及各自员工、两个失主、一个邮箱未验证的账号，覆盖越权访问、重复认领、并发审批和重复扫码。

## 7. 已知限制

- 真实 Cognito、SES、IAM 与云端部署联调尚未进行；CI 在本分支首次 PR 时运行。
- 交接 30 天后清理联系人、Notification 的 Prometheus 指标、SES 退信/投诉处理尚未实现。
- 文档语言约定：`docs/` 使用中文，代码、配置与服务 README 使用英文。
