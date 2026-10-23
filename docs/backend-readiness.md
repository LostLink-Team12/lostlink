# 后端联调准备清单

维护人：JIANG YIBAI。更新：2026-10-22。对应 Issue #7，部署依赖 Issue #5/#6。

本清单把可本地完成的准备与需要 AWS dev 的验收分开。OpenAPI、CI 和消费者恢复测试通过，不代表真实 Cognito、SES、IAM、EKS 或性能目标已经验收。

## 1. 资源交接表

联调前由部署方填入实际值，通过秘密管理渠道交付密码；PR、截图和日志只记录配置项名及检查结果。

| 范围 | 需要交接的配置 | 检查方式 |
| --- | --- | --- |
| 环境 | 账号、区域、namespace、完整镜像 SHA | 各服务使用同一批准版本；`ENVIRONMENT=dev`，无 LocalStack endpoint 或测试 AWS 凭据 |
| Cognito | 用户池 ID、issuer、登录域名、Owner/Venue/运营 client ID | issuer 与登录域名分别填写；浏览器客户端无 secret，回调和退出 URL 精确匹配 |
| 用户令牌 | Venue V2 Lambda、受限身份数据库角色、允许的用户客户端 | Lambda 可访问已迁移的 Venue 库；用户 access token 包含正确角色，员工含真实 tenant UUID |
| M2M | token URL、各服务 client ID/secret、自定义 scope | secret 由运行秘密注入；接收方同时验证客户端白名单和 scope |
| 数据库 | RDS endpoint、TLS CA、各模式运行账号、迁移身份 | TLS 校验通过；运行角色不是表所有者、无超级用户/BYPASSRLS；API/worker 不携带迁移身份 |
| 对象存储 | 私有照片桶、前端来源、模型对象及摘要 | 浏览器预签名上传成功；封存路径写权限与 Matching 读权限分离 |
| 事件 | SNS ARN、各 SQS URL、DLQ、订阅过滤 | 按 `event_type` message attribute 过滤；队列允许指定 topic 投递；`maxReceiveCount=5` |
| 服务网络 | Venue/Item/Matching 内部 HTTPS URL、证书信任 | 固定 HTTPS 地址；`/internal/v1/*` 和 `/metrics` 不经公网入口暴露 |
| SES | 已验证发件身份、沙箱测试收件人、发送角色、Owner App URL | 区域一致；`EMAIL_SENDER=ses`，实际邮件可达、链接可用 |
| Claim 密钥 | `FIELD_ENCRYPTION_KEYS`、活动 key ID、`HANDOVER_TOKEN_KEY` | 活动 ID 存在；字段密钥解码为 32 字节，交接密钥至少 32 字节；重启/部署不重新生成 |

## 2. 进程与凭据核对

公共配置包含 `AWS_REGION`、服务运行模式及所需秘密。API 需配置 `COGNITO_ISSUER`、`COGNITO_CLIENT_IDS` 和各自细分白名单。下面列出部署分工，完整字段见各服务 `.env.example` 和 Settings。

| 进程 | 数据库配置 | 其他关键配置 |
| --- | --- | --- |
| Venue API | `DATABASE_URL`、`PLATFORM_DATABASE_URL`；启用 identity resolve 时另配 `IDENTITY_DATABASE_URL` | USER/PLATFORM/MEMBER_CHECK/IDENTITY_RESOLVE 客户端白名单 |
| Venue outbox / identity worker | `WORKER_DATABASE_URL` | outbox 配 topic；identity 配用户池及 ListUsers/AdminUserGlobalSignOut 权限 |
| Venue token Lambda | `IDENTITY_DATABASE_URL` | trigger 模式、用户池 ID、USER/PLATFORM 白名单；V2 调用和数据库连接需在 Cognito 时间预算内完成 |
| Item API | `DATABASE_URL`、`INTERNAL_DATABASE_URL` | Venue URL、机器认证、PHOTO_BUCKET、USER/CLAIM/INDEX 客户端白名单 |
| Item tasks / outbox / consume / cleanup | `WORKER_DATABASE_URL` | tasks/cleanup 的照片权限、outbox topic、consume 的 embedding 结果队列 |
| Matching API | `DATABASE_URL`、`INTERNAL_DATABASE_URL` | 模型、Item HTTPS 与机器认证、USER/NOTIFICATION 客户端白名单 |
| Matching consume / tasks / outbox | `WORKER_DATABASE_URL` | consume 队列；tasks 的模型、照片只读权限和 Item 认证；outbox topic |
| Claim API | `DATABASE_URL` | Venue/Item HTTPS、机器认证、用户池目录权限、字段和交接密钥、USER 白名单 |
| Claim tasks / outbox | `WORKER_DATABASE_URL` | 按 Settings 配置 Item/机器认证和密钥；outbox topic |
| Notification API | `DATABASE_URL`；启用运营接口时另配 `ADMIN_DATABASE_URL` | USER/OPERATOR 白名单 |
| Notification consume / tasks | `WORKER_DATABASE_URL` | 按当前 worker Settings 配齐 Matching/机器认证、用户池、Owner App URL、SES 发件地址；consume 队列 |

机器调用关系：

| 调用方 | Scope | 接收方白名单 |
| --- | --- | --- |
| Item | `lostlink/members.check` | Venue `MEMBER_CHECK_CLIENT_IDS` |
| Claim | `lostlink/members.check`、`lostlink/items.context`、`lostlink/items.reserve` | Venue `MEMBER_CHECK_CLIENT_IDS`、Item `CLAIM_CLIENT_IDS` |
| Matching | `lostlink/items.index` | Item `INDEX_CLIENT_IDS` |
| Notification | `lostlink/matches.notify` | Matching `NOTIFICATION_CLIENT_IDS` |

客户端白名单采用 JSON 字符串数组，必须是实际 client ID；不同数据库模式使用不同登录角色。服务账号需要通过 Pod Identity/IRSA 获得其 AWS 权限。上述 Cognito scope 不替代 IAM 或数据库权限。

## 3. 逐步验收

每一步记录环境、镜像 SHA、时间、request_id/correlation_id、预期结果与实际结果；不要记录令牌、联系人、私密答案或完整请求体。

- [ ] **部署基础**：迁移 Job 成功，各 API `/health` 为 200；五个主要服务 `/ready` 为 200，所有后台进程独立运行且无反复重启。
- [ ] **身份**：Owner/Venue 登录；令牌为 access token；员工角色变更后重新获取令牌；错误客户端、ID token、越权场馆操作被拒绝。
- [ ] **登记**：用小批量数据创建物品、申请并直传 S3、确认上传、轮询操作；最终 embedding 完成且物品 available，不能把 202 或上传成功当作最终完成。
- [ ] **搜索**：文字、照片、融合查询与分类/场馆/日期过滤；已交接物品不返回；上游 Item 不可用时 503，不能以旧状态继续返回结果。
- [ ] **报告与邮件**：创建报告后登记匹配物品；检查 match.found、Notification 记录和真实邮件；邮件链接必须能显示匹配详情，页面缺口属于 #8。
- [ ] **认领**：已验证邮箱用户提交答案；场馆审核、批准、轮询；并发认领冲突、跨失主读取和员工审核自己认领的请求被拒绝。
- [ ] **交接**：签发 QR、提交原始 payload 和 If-Match、轮询直到 handed_over；重复扫码、过期 QR 和旧版本请求按契约拒绝。
- [ ] **幂等**：同 key 同请求重放，异体重用 key 为 409；缺 key/If-Match 为 428，旧版本为 412；422 与其他错误复用统一错误体。
- [ ] **消息恢复**：队列空闲至少两个默认长轮询周期后仍存活；发送合法事件后可消费；临时网络故障后继续；冲突/无效消息不删除并最终进入 DLQ。
- [ ] **邮件限制**：沙箱内验证演示收件人；失败/未知发送结果通过运营恢复流程处理，避免盲目重发造成重复邮件。

Delivery 已由 #23 补齐持久化运单、交付授权、伙伴状态接口、inbox/outbox 与 Claim 消费者；柜台领取会取消待取件运单，重复事件受幂等保护。但 AWS 资源、chart、入口、伙伴客户端和云端演示尚未接入，以上清单仍不把 UC6 记为完成；完整 Webhook 平台需后续实现。当前主线与部署差异见 [进度核对](main-progress-2026-10-23.md)。

## 4. SQS 超时与恢复约定

共享客户端仅对 SQS 使用 25 秒读取超时，覆盖最长 20 秒轮询；SNS/Cognito 保留 3 秒。消费者默认等待 10 秒，SDK 最多尝试 3 次。重试耗尽后的临时网络/服务错误返回工作循环，空轮询后等待 1 秒再继续；权限和错误队列配置仍报错。SDK 重试和长轮询会延长退出时间，应在部署中验证终止宽限期。

业务提交后才确认消息。确认失败会等待可见性超时后的重投，由持久化 inbox 去重；消息内容和 receipt handle 不写日志。不能为了消除告警删除未处理或冲突消息。

取消正在审批的认领时，复用任务的可执行时间使用 PostgreSQL 时钟，与 worker 的领取条件保持一致，避免 API 主机时间偏快导致应立即执行的取消被推迟。Venue 的任务领取、租约、退避和人工重放也使用同一数据库时钟。回归覆盖主机时钟正常、快一小时和慢一小时三种情况。

本地检查使用 Docker Compose 依赖和 `LOSTLINK_TEST_AWS_ENDPOINT`，新建临时队列并清理；AWS 的 IAM、过滤策略和 DLQ 行为仍须在 dev 验收。

## 搜索性能诊断准备

目前仍逐候选调用 Item；本 PR 只增加测量，不增加缓存/批量接口，也不更改实时状态复核或查询结果。正式 k6 负载/扩容脚本由 #10 负责。

### 测量内容

Matching API 在内部 `/metrics` 暴露以下低基数指标，不包含查询内容、照片、令牌、账号或物品 ID：

| 指标 | 含义 |
| --- | --- |
| `lostlink_search_phase_duration_seconds` | Histogram，phase 固定为 encode、candidates、item_context；包含失败阶段 |
| `lostlink_search_item_context_calls_total` | 已尝试的 Item context 调用数，失败调用也计入 |
| `lostlink_search_candidates` | 每次查询从 pgvector 取出的候选数 |
| `lostlink_search_results` | 成功查询的返回数；失败请求不计为零结果成功 |

encode 包括查询照片解码和 CLIP；candidates 包括数据库连接、候选查询和会话结束；item_context 包括顺序 Item 调用、实时过滤和结果组装。整体 HTTP 指标还包括认证、请求校验和其他框架开销。机器令牌缓存冷启动属于 Item 调用耗时。

### 运行步骤

1. 记录机器/Pod CPU、内存、镜像 SHA、模型摘要、数据量、查询方式、candidate_limit、结果 limit 与并发数。
2. 确认 API 和 tasks 使用相同模型；`/ready` 完成加载。分别记录冷启动，再预热代表性文字/照片查询和机器令牌缓存。
3. 先对少量已 available 的物品跑既有 `tests/load/smoke.js`；令牌从当前秘密会话注入，不写文件或命令历史。
4. 从内部网络抓取 `/metrics`，用 Prometheus/Grafana 比较同一时间窗、同一环境的整体延迟、各阶段与调用数。可以本地 port-forward API 的 8000 端口，不开放公网 metrics。
5. 在 #10 的正式负载中逐步增加并发，并比较不同候选/返回数量。记录错误率与结果数，避免把全是失败或空结果的快速响应当作优化。
6. 若预热后的搜索 p95 超过 Issue #7 的 500 ms，结合阶段耗时决定优化：Item 占比高时考虑批量接口/短缓存；推理占比高时检查 CPU/HPA；数据库占比高时检查查询、索引和连接预算。任何优化仍须维持最新状态/版本过滤。

整体成功搜索 p95 的 PromQL 示例，实际使用时另加环境/job/namespace 选择器：

```promql
histogram_quantile(0.95,
  sum by (le) (rate(lostlink_http_request_duration_seconds_bucket{
    service="matching-service",method="POST",route="/v1/search",status="200"
  }[5m]))
)
```

每个阶段的平均时间：

```promql
sum by (phase) (rate(lostlink_search_phase_duration_seconds_sum[5m]))
/
sum by (phase) (rate(lostlink_search_phase_duration_seconds_count[5m]))
```

每次进入 Item 阶段的平均调用数：

```promql
sum(rate(lostlink_search_item_context_calls_total[5m]))
/
sum(rate(lostlink_search_phase_duration_seconds_count{phase="item_context"}[5m]))
```

各阶段包含失败请求，解释平均数时结合错误率；各阶段 p95 不能相加得到整体 p95。低流量或短时间窗没有足够样本时不能据此验收。现有 smoke 的 p95 < 5 秒只证明冒烟门槛，不证明 500 ms 目标。

### 记录模板

| 环境/版本 | 查询方式/过滤 | 数据量 | candidate_limit/limit | 并发与时长 | 总体 p95/错误率 | 三阶段平均耗时 | Item 次数/结果数 | 结论 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 待 dev 验证 | 待填写 | 待填写 | 待填写 | 待填写 | 待测 | 待测 | 待测 | 不提前宣称达标 |

本地 ASGI 测试使用注入的测试身份/传输验证测量及状态过滤；真实 CLIP + PostgreSQL 测试验证模型链路。它们不包含真实 Cognito、HTTPS 网络和云端并发，不能代替 AWS p95 或 5,000 张照片吞吐验收。
