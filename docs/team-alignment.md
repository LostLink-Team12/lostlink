# 后端协作对齐与通知清单

更新时间：2026-10-20。维护人：JIANG YIBAI。

这是当前分支采用的实施约定和待团队接入事项，并非队友已经确认或已收到通知的记录。当前已实现 UC1—UC5 及 Claim 前置共享能力并记录接入要求，没有向任何成员发送消息。

## UC4 已实现后的接入要求（2026-10-20）

- Lu Jingxing：新增 Notification API/admin/worker 三个数据库角色与 `notification` schema；部署 API、`consume`、`tasks` 三个进程。`consume` 队列只订阅 `match.found`（SNS 过滤）并配置 DLQ（maxReceiveCount=5）。`tasks` 需要：Notification M2M 客户端（`lostlink/matches.notify`，并加入 Matching 的 `NOTIFICATION_CLIENT_IDS`）、用户池 `ListUsers`、`ses:SendEmail`（限定已验证发件身份）、`OWNER_APP_BASE_URL`。SES 需验证发件域/地址；沙箱期间只能发给已验证测试收件人。运营客户端加入 `OPERATOR_CLIENT_IDS`，运营人员属于 `platform_admin` 组。
- Zhang Ji：Owner App 需提供 `/matches/{match_id}` 页面（邮件链接固定为 `OWNER_APP_BASE_URL/matches/{id}`）；可选显示 `GET /v1/notifications` 的状态。
- Yang Dongwei：Notification 每次发送前调用通知上下文接口复核；如需在邮件显示场馆名称，需 Matching 在 Venue 提供公开目录后填充 `venue_name`。
- 详见 [UC4 记录](uc4-implementation.md)；本轮未发送真实邮件。

## UC5 已实现后的接入要求（2026-10-20）

- Lu Jingxing：新增 Claim API/worker 两个数据库角色、迁移凭据与 `claim` schema；Claim M2M 客户端需 `lostlink/members.check`、`lostlink/items.context`、`lostlink/items.reserve`，并加入 Venue 的 `MEMBER_CHECK_CLIENT_IDS` 与 Item 的 `CLAIM_CLIENT_IDS`。秘密：字段加密 keyring、`HANDOVER_TOKEN_KEY`（≥32 字节 base64）、机器客户端密钥。部署 API、`tasks`、`outbox` 三个进程；`tasks` 需访问 Item 内网地址。API 工作负载需用户池 `ListUsers`。
- Lu Jingxing / Item 维护者：Item 的 5 分钟预留观察会对“已批准、等待现场交接”的认领持续报 `needs_attention`。这是预期持有而非故障，告警需按 Claim 操作状态过滤，或后续增加 Claim 批准确认接口后停止观察。详见 [UC5 记录](uc5-implementation.md)。
- Zhang Ji：失主端先 `GET /v1/claim-questions`，提交需幂等 key；取消、审批、拒绝、扫码需 If-Match。审批、获批后取消、扫码返回 202 和 `operation_id`，须轮询 `/v1/claim-operations/{id}` 直到 `claim_state` 为终态，202 不能作为交接完成。QR 显示 `qr_payload` 原文，同 key 只在 10 分钟内可重放。员工审批同一物品第二个认领得到 409 `item_claim_in_progress`；员工不能审批或交接自己的认领（403）。联系人仅在 approved/handover_pending 可读。
- Yang Dongwei：Claim 已发布 `claim.approved`、`claim.cancelled`、`claim.handed_over`（当前 `mode=counter`）；Seed 2 的交付授权（T5）尚未实现，Delivery 不能凭 `claim.approved` 读取联系人。
- Notification 后续 Agent：Claim 事件不含邮箱；如需在认领状态变化时发信，必须通过身份目录按 `owner_id` 解析，不从 Claim 联系人读取。

## Claim 前置共享能力的接入要求（2026-10-20）

- Lu Jingxing：Claim 与 Notification 需各自的字段加密密钥，以秘密注入 JSON `{"key-id": "base64 的 32 字节"}` 和当前 key ID 两项；轮换时新增 key 并切换当前 ID，旧 key 保留到重加密完成。两服务的工作负载 IAM 只需目标用户池的 `cognito-idp:ListUsers`，不授予其他 Cognito 管理权限。Claim 迁移将使用“失主或场馆”双策略，API 角色不得获得 `tenant_id`、`owner_sub` 的 UPDATE 权限。
- Yang Dongwei：共享注册表新增 `claim.handed_over`（`mode` 为 counter/courier，`handed_over_at` 带时区）和 `claim.cancelled`（受控 `reason_code`），来源均为 claim-service 且要求租户。Delivery 以 `mode=courier` 的交接事件作为 picked_up 依据；载荷不含联系方式。
- Zhang Ji：认领取消原因只能选 `found_elsewhere`、`no_longer_needed`、`submitted_in_error`、`other`，不提交自由文本。
- 详情见 [01 第 8 节](backend-handoff/01-contracts-and-access.md)；本轮未发送消息、未变更云资源、未自动 commit。

## UC3 已实现后的接入要求

- Yang Dongwei：按用户要求，Matching 与 Embedding 已在本分支实现；代码统一位于 matching-service，不另建重复向量库。真实 CLIP 固定 512 维和 checkpoint digest；API/worker 必须同模型。完整业务、乱序处理及后续边界见 [09：Matching/Embedding](backend-handoff/09-matching-and-embedding.md)。
- Lu Jingxing：新增 matching API/internal/worker 三类数据库角色；安装 vector 扩展、配置 items.index M2M 和 matches.notify 客户端、sealed 桶只读权限、队列过滤及 DLQ。consume 在数据库入队后确认 SQS，tasks 需常驻或按 PostgreSQL 积压伸缩；不能单靠 SQS 深度缩到零。推理镜像需 INSTALL_ML=true 和只读权重挂载。
- Zhang Ji：UC3 接口接受 owner 用户，报告需幂等 key，关闭需 If-Match。搜索支持 text/photo_base64 和分类/场馆/拾得时间过滤，照片 5 MiB、无 data URL 前缀；分值是余弦相似度，不能展示为概率。候选窗口与实时过滤会导致短页，详情见服务 OpenAPI 和 09 文档。
- Notification 后续 Agent：matches.notify 接口已实现，必须发送前核对事件 ID 归属及 active；venue_name 可为 null，暂无公开 Venue 名称目录，使用通用标签。邮箱解析、SES、发送去重与真实外发仍待 UC4。
- Claim 后续 Agent：搜索结果已经实时校验，但认领仍须重新取得上下文并预留；不能将搜索匹配看作归属证明。
- 本轮只进行本地测试和模型下载，没有向成员发送实际消息、创建云资源、发邮件或自动 commit。实际验证与限制见 [UC3 执行记录](uc3-implementation.md)。


## UC2 已实现后的接入要求

- Yang Dongwei：Item 已产生 item.registered 和完整 item.catalog_changed；Embedding 的成功/失败需要回传原 correlation_id 与 item_version。Matching 仅公开 available 并保留其他状态的 revision 墓碑；不能以旧向量重新公开已预留/撤销物品。新失败事件 item.embedding_failed 已注册，数据不带原始堆栈。
- Claim 后续 Agent：预留、释放墓碑、查询和固定 handover_id 交接已提供；使用 items.context/items.reserve 专用 scope 和稳定幂等 key。持有超过五分钟的预留只报警，不自动释放，由认领协调器恢复。
- Lu Jingxing：配置 Item 三类数据库身份、私有 S3、sealed 前缀条件写策略、前端 POST CORS、SNS 过滤、Item 结果队列与 DLQ，以及 tasks/outbox/consume/cleanup 四类独立进程。默认 API 构建不运行迁移；CI 已启用 LocalStack S3。
- Zhang Ji：上传确认返回持久 operation_id，轮询完成后才显示 processing；列表不带参考答案，详情仅当前场馆员工可见。批量导入带稳定 batch_reference，可逐行修复并分批上传，运行脚本不等待 CLIP 完成。
- 具体决定、运行命令和真实联调缺口见 [UC2 执行记录](uc2-implementation.md)。5,000 条规模测试验证的是数据库登记与批次恢复，不代表已完成云端 5,000 张图片 CLIP 吞吐演示。

## UC1 已实现后的接入要求

- Lu Jingxing：Venue 已提供独立 API、outbox、identity worker 和 Cognito Lambda 入口；按 [UC1 执行记录](uc1-implementation.md)配置四类数据库角色、秘密及用户池范围的 IAM。触发器选择 V2，直接按 sub 读取受 RLS 保护的目录，不回调鉴权接口；默认无需为触发器申请 M2M 令牌。运营组由可信部署引导，普通成员 API 不能授予。
- Yang Dongwei：共享注册表增加 venue.membership_changed，原五种载荷保持兼容。其他服务不能因事件到达较慢而继续允许停用成员敏感写入；需要时复用共享成员客户端。通用其他状态事件尚未注册。
- Zhang Ji：Venue 入驻绑定已有 Cognito 账号；新增成员后可查询身份任务。写入需幂等 key，PATCH 需 If-Match，角色提升后刷新 access token。API 契约见新服务 /docs。
- Item/Claim 实施 Agent：MembershipClient.require_current 已可调用 Venue 的成员检查契约，配置专用客户端和 lostlink/members.check。每次敏感写入实时核验，依赖故障返回 503，不使用允许旧权限的回退逻辑。
- 本轮真实 PostgreSQL/LocalStack 验证已完成；真实 Cognito、Lambda、网关/IAM 和 Item/Claim 联调仍待进行，详情与命令见执行记录。没有向成员发送实际消息。

## 后续移交设计补充

完整方案见[Agent 移交入口](backend-handoff/README.md)。以下是待实现、待接入的契约；下方基础阶段记录中的五种事件仍是当前代码基线。

| 对接方 | 后续决定与接入要求 |
| --- | --- |
| Yang Dongwei | Matching 消费 `item.catalog_changed`，仅索引 available 快照并保留版本墓碑；提供受 `lostlink/matches.notify` 保护的通知上下文接口。详见移交文档 02、06。 |
| Yang Dongwei | Embedding 原样传递登记事件的 `correlation_id` 到成功/失败结果，以隔离相同内容版本的不同处理轮次；人工重试生成新轮次，传输重投保持原轮次。新增事件先注册、测试再启用。 |
| Yang Dongwei | Delivery 以失主授予指定合作方的交付授权为依据，不能凭 `claim.approved` 获取所有认领；取件须由场馆确认，运输状态由 Delivery 管理。详见 07。 |
| Lu Jingxing | 为失主、员工、M2M、后台任务配置明确的访问边界；每服务区分迁移/API/worker 数据库角色。配置新 scope、客户端白名单、发件箱与消费者独立进程、DLQ 及加密密钥。详见 01、02、08。 |
| Zhang Ji | 审批、取消和交接可能返回 202，须查询操作结果；202 不能作为现场交付成功依据。写入使用幂等 key 和适用的 If-Match；处理 412、428 及短期 QR 重放窗口。详见 01、05。 |
| 后续实施 Agent | T0 先补统一访问和可靠消息；其后按 T1—T6 拆分实施。不得把替身测试或历史测试结果描述为真实业务联调已完成。 |

## Yang Dongwei：接口、匹配与 Seed 2

| 内容           | 当前实施决定                                         | 需要接入的工作                                              |
| -------------- | ---------------------------------------------------- | ----------------------------------------------------------- |
| 事件格式       | 使用共享包的版本一信封与五种载荷                     | worker、matching 和 delivery 复用共享模型，避免另写一套字段 |
| embedding 触发 | 照片确认上传后发布 `item.registered`；SNS 订阅到 SQS | 消费公开描述、分类及 S3 对象引用；不在消息中携带图片        |
| 旧任务处理     | 事件携带 `item_version`                              | 旧版本任务不得覆盖新版本 embedding                          |
| 失物报告归属   | matching 服务拥有报告、公开检索读模型及匹配记录      | 补充报告 API、存储和 `lost_report.created` 的处理           |
| 搜索隔离       | 检索公开读模型；不读取隐藏答案及未授权联系方式       | 明确检索字段白名单，查询不绕过业务表 RLS                    |
| 匹配得分       | `match.found.score` 为 -1 到 1 的余弦相似度          | 统一展示与阈值语义，不当作概率                              |
| 通知           | `match.found` 提供匹配、报告、物品和失主标识         | 通知服务通过受保护接口或可信身份数据解析收件地址            |
| 发件箱         | 业务服务落库与待发送事件在同一事务中保存             | 消费端以 `event_id` 去重，提交后确认消息                    |
| Seed 2         | 使用受保护的物品/认领 API 与 `claim.approved`        | delivery 不直接读取其他服务的业务表                         |

OpenAPI 总体契约仍对应原计划 A1；当前后端可先定义负责服务的接口并记录版本，再由架构负责人统一汇总。没有完成团队接入前，不把这些约定描述为已经联调通过。

## Lu Jingxing：身份、数据库与部署

1. 配置 Cognito 用户访问令牌中的可信 `custom:tenant_id`，角色使用 `venue_admin`、`venue_staff`、`owner`。租户属性不允许普通用户自助写入。API 拒绝 ID token。
2. 按环境提供签发方和应用客户端白名单；Partner 客户端使用独立的 scope 授权。若启用资源受众，同时提供 `COGNITO_AUDIENCE`。
3. 每个服务分配独立运行时角色和迁移角色。运行时角色不能是数据库超级用户、不能拥有 `BYPASSRLS`，也不应成为业务表所有者。
4. 部署时先执行 Alembic 迁移，再启动或更新应用。API 容器不注入迁移凭据。
5. Docker 必须从仓库根目录构建，通过 `SERVICE_DIR` 指定服务。CI 已相应调整，并增加共享包及真实依赖集成测试。
6. `/ready` 缺少配置或迁移不匹配时返回 503，不要继续使用“固定成功”的探针假设。
7. `/metrics` 只供集群内部 Prometheus 抓取，公网入口不暴露它。日志从 `lostlink` logger 输出 JSON。
8. SNS/SQS 使用标准队列、主题以及事件类型过滤。IAM 和队列策略限制实际发布源；准备死信队列及重试配置。
9. 后续邮件通知还需要 SES 身份验证、发送权限和测试收件人。当前 Compose 只启用 S3/SQS/SNS，不能据此认为 SES 已可用。
10. 本轮没有申请或创建 AWS 云资源；CI 配置改动尚需提交后由 GitHub Actions 验证。

## Zhang Ji：前端与质量

- 登录后向后端发送 Cognito access token；不能把 ID token 当作 API 凭据。
- 前端不提交租户 ID 来选择场馆，身份上下文由后端令牌校验得到。
- 模板 `/v1/me` 可查看已验证身份；`/v1/examples` 仅演示鉴权与租户存储，不能作为正式物品 API。
- 错误处理统一读取 `error.code`、`error.message` 和 `request_id`；401、403、409、422、503 分别处理。
- 上传计划采用“申请上传→直传 S3→确认上传→异步处理”，登记后 UI 显示处理中，embedding 完成后才可搜索。
- 公开搜索结果不展示核验答案；场馆仅在认领批准后获准看到交接必需的联系方式。
- 测试数据至少包含两个场馆、各自员工、失主及无租户身份，覆盖越权访问、重复认领、重复审批和重复扫码。
- 5,000 张图片的批量导入接口及脚本在 Item Service 阶段联调；当前库与模板没有实现导入功能。

## 后端工作流约定

当前及后续业务实现由本工作流自行设计；遇到涉及队友的决定，先在此记录决定、影响和接入要求，并继续实现可独立验证的部分。中文说明放在 `docs/`，代码、配置、测试名称及其他目录的说明采用英文。
