# 统一契约、数据访问与共享能力补齐

状态：部分实施。实施任务：T0。依赖：[现有基础能力](../backend-foundation.md)。

实施更新：Venue 和 Item 已在各自服务中实现显式事务上下文、幂等和任务处理，Item 另有事务 inbox；共享库已有成员检查客户端。这不代表本章全部通用能力已完成，见 [UC1 记录](../uc1-implementation.md)和 [UC2 记录](../uc2-implementation.md)。

2026-10-20 Claim 前置更新：共享库已补齐失主/场馆/系统三类会话、失主或场馆 RLS 辅助函数、字段加密、身份目录和 Claim 两种扩展事件，见本文第 8 节。通用幂等工具和完整 `ServiceClient` 仍未抽取到共享库。

## 1. 服务边界与部署名

| 服务 | schema | 建议 Python 包名 | 负责数据 |
| --- | --- | --- | --- |
| Venue | `venue` | `venue_app` | 场馆、成员、身份同步状态 |
| Item | `item` | `item_app` | 物品、隐藏核验资料、照片、导入、物品预留 |
| Claim | `claim` | `claim_app` | 认领、审批协调、QR、交接、交付授权 |
| Notification | `notification` | `notification_app` | 通知任务、合作方订阅及 Webhook 投递 |

每个服务维护自己的 `DeclarativeBase` 和 Alembic 历史，不建立跨 schema 外键或跨服务 ORM 关系。现有模板的 `app` 包仅保留在模板中，新服务采用独立包名，Docker 启动模块随之调整，避免同一测试进程导入错服务。

每个服务至少区分迁移角色、API 角色、后台角色。迁移角色不注入运行容器。后台角色只处理本服务数据，不能获得其他 schema 的查询权或数据库 `BYPASSRLS`。

## 2. 公共接口约定

- 外部接口以 `/v1` 开头，内部接口以 `/internal/v1` 开头；内部路径不能通过公网入口暴露。
- ID 使用 UUID；用户标识使用 Cognito `sub` 原始字符串，不强制转换为 UUID。
- 时间采用带时区的 ISO 8601，数据库存储 UTC；客户端时间不能作为状态变更的可信时钟。
- JSON 请求拒绝未知字段；失主和员工请求不能自行提交 `owner_id`、`tenant_id`、审批者或系统状态。
- 列表统一 `limit=20`、最大 100，`offset=0`、最大 10000；按 `created_at,id` 稳定排序。响应为 `{"items":[],"limit":20,"offset":0,"has_more":false}`，不默认计算昂贵的总数。
- POST 创建成功返回 201；异步操作返回 202，并带可查询的操作 ID 与状态；同步状态操作返回 200。
- 401 表示无效身份，403 表示身份有但没有操作权限，404 用于不存在或不可见资源，409 用于业务状态或幂等冲突，412 用于版本不匹配，422 用于字段错误，428 用于缺少必需的并发/幂等前提，503 用于依赖不可用。
- 保留现有统一错误体；业务代码补充稳定的英文错误码，如 `item_not_available`、`idempotency_conflict`、`membership_inactive`。
- 对外返回专用响应模型，不能直接序列化完整 ORM 对象。

### 幂等与乐观并发

创建及动作型 POST 必须携带 `Idempotency-Key`，长度 8—128，仅接受可打印的 ASCII。其作用域为“已验证调用者标识 + HTTP 方法 + 路由模板及资源 ID + key”。同 key、同规范化请求摘要重放原结果；同 key 不同请求返回 409。

幂等记录包含 `request_hash`、`state`、`resource_id`、`response_status`、受保护的响应快照、到期时间。普通记录保留至少 24 小时；涉及审批/交接还须由持久业务唯一约束保证记录过期后也不会重复执行。处理中请求返回 409 和 `Retry-After`，不得再启动第二个外部操作。

PATCH、撤销、角色修改等基于已有资源的操作同时要求 `If-Match`，值为读取响应中的 ETag，例如 `"7"`。缺失返回 428，版本不同返回 412。SQL 使用 `WHERE version=:expected` 并递增版本，不采用“先读后无条件更新”。

动作请求先验证当前身份及资源访问权限，再查幂等记录；已完成的同 key、同请求重放不重新执行版本比较，否则成功动作造成的版本递增会让网络重试错误地返回 412。请求摘要包含规范化 body 和原 `If-Match`；换用新的版本头属于不同请求。只有首次执行才检查当前版本并执行状态变更。秘密响应还须满足对应的短期重放窗口。

纯数据库操作的业务变更、审计、幂等结果和发件箱写入应在同一事务提交。涉及 S3、邮件、Cognito 或其他服务时持久化操作状态，先提交短事务，再调用外部系统，最后提交结果；不持有数据库行锁等待网络。

响应快照若包含联系方式或 QR 原文，必须加密且限制访问及保存期，不能以明文写入通用幂等表。QR 采用 Claim 文档中的专用签发和短期重放机制。

## 3. 身份与访问模式

现有 `require_tenant` 不能用于所有业务。T0 必须补齐以下明确模式，禁止把 `session(None)` 解释成不限租户。

| 模式 | 可信上下文 | 数据库范围与典型用途 |
| --- | --- | --- |
| 场馆员工 | 已验证 JWT 的租户、用户、角色；当前成员资格校验 | 本场馆物品与认领 |
| 失主 | 已验证 `sub` 和 `owner` 角色；不要求场馆 claim | 只读写自己的认领、联系方式及报告 |
| 平台运营 | `platform_admin` 与指定运营客户端 | 场馆全局目录及引导，单独受限数据库角色/会话 |
| 内部服务 | 服务客户端白名单和操作 scope | 显式授权的内部 API；目标租户由权威资源定位或可信事件得到 |
| 后台投递 | 本服务专用工作负载身份 | 本服务 operational 表；业务变更另开明确上下文事务 |

新增 `actor_session` 概念：事务内设置 `app.user_sub` 及可选 `app.tenant_id`，来源仅为认证后的主体。现有 `Database.session(UUID)` 继续支持原租户模式。使用独立的方法及依赖，不根据未验证请求头或 body 自动选择模式。

已实现（共享库 `db` 模块）：

| 入口 | 设置的上下文 | 用途 |
| --- | --- | --- |
| `session(tenant_id)` | 仅 `app.tenant_id` | 原租户模式，行为不变；传入非 UUID 仍拒绝 |
| `actor_session(principal, venue_scope=False)` | 仅 `app.user_sub` | 失主访问；不设置租户，即使令牌带场馆 claim |
| `actor_session(principal, venue_scope=True)` | `app.user_sub` + 已验证 `app.tenant_id` | 场馆员工访问；令牌无租户时拒绝 |
| `scoped_session(subject, tenant_id, resource_id)` | 显式三项 | 可信资源定位或可信事件；值经格式校验 |
| `system_session()` | 全部为空 | 后台 operational 表；RLS 表返回空，权限由数据库角色授权决定 |
| `get_owner_session` | 依赖 `owner` 角色 | FastAPI 依赖；M2M 令牌无 owner 组，因此返回 403 |

`venue_scope` 为必填关键字参数，调用方必须明确选择模式。上下文仍采用事务内 `set_config(..., true)`，连接复用不残留。现有 Venue/Item/Matching 的 `context()` 保持各自实现，未强制迁移。

认领主表需要“失主本人可见，或者所属场馆的当前员工可见”的 RLS；联系方式单独建表，额外要求认领已获批，不能照搬主表策略。为不同角色明确设置策略及 SQL 权限，注意多条允许策略叠加后的实际可见范围；集成测试必须验证最终效果。

共享 `enable_owner_or_tenant_rls(connection, schema, table, owner_column="owner_sub")` 为主表建立 `owner_access` 与 `tenant_access` 两条允许策略。真实 PostgreSQL 验证表明：员工若同时是其他场馆认领的 owner，会同时看到两组记录，因此 Claim 必须在业务层拒绝自审；两条策略的 WITH CHECK 也是“或”关系，失主可写任意租户的本人行、员工可写本场馆任意 owner 的行，必须用列级授权限制可写字段，`tenant_id`、`owner_sub` 不能对 API 角色开放 UPDATE。联系方式表不使用此辅助函数，须由 Claim 迁移单独编写策略。

后台发件箱、消费记录、租约等 operational 表不作为普通租户 CRUD 表开放：API 角色仅有必需的写入/自身幂等查询权限，工作者可认领任务。其载荷禁止包含隐藏答案、联系方式和访问令牌。后台数据库角色仍不是超级用户，不能绕过领域表 RLS。

全局资源定位采用极小的映射表，例如 `item.item_locator(item_id,tenant_id)`。只向指定内部处理器提供“按 ID 定位”能力，不提供面向任意员工的全表查询接口。定位后仍须校验调用者、动作和资源状态，再进入目标租户事务。

## 4. 成员变更与令牌有效期

JWT 中的场馆与角色用于验证身份入口，Venue 的成员表才是当前授权事实。敏感员工操作（审批、交接、改角色、撤销及修改物品）必须通过内部成员检查确认成员未停用；读取可以缓存最多 30 秒，写入不使用正向缓存。Venue 无法访问时敏感操作返回 503，不放行旧令牌。

Cognito 组不是可直接表达任意多个场馆内不同角色的权限模型。首期仅支持一个用户一个活动场馆成员关系；未来多场馆支持必须增加受信任的活动场馆选择流程。访问令牌触发器从成员目录生成场馆 claim 与本次有效角色，不能相信用户自助填写的属性。

## 5. 内部调用契约

共享 `ServiceClient` 负责从配置地址调用，不接受来自请求的任意目标 URL。连接超时 2 秒，单次读取超时 5 秒，总体重试预算 15 秒；只对安全读取或携带幂等键的写入重试网络故障、429、502、503、504，最多 3 次并加随机退避。

Cognito M2M 客户端使用 client credentials，客户端密钥从部署环境的秘密存储注入，缓存访问令牌至到期前 60 秒。接收方同时检查 `client_id` 的服务映射和必需 scope；不能仅凭 `sub`、scope 或内网地址放行。内部接口拒绝普通用户应用客户端。

| scope | 合法调用方 | 用途 |
| --- | --- | --- |
| `lostlink/members.check` | Item、Claim | 校验当前员工成员关系 |
| `lostlink/identity.resolve` | 令牌触发器适配 | 生成可信场馆身份 |
| `lostlink/items.context` | Claim | 取得指定物品的认领问题及许可字段 |
| `lostlink/items.reserve` | Claim | 预留、取消预留、提交交接 |
| `lostlink/items.index` | Embedding、Matching | 当前版本检查及公开索引快照 |
| `lostlink/matches.notify` | Notification | 查询匹配通知上下文，核验归属及当前有效性 |
| `lostlink/claims.delivery` | Delivery | 创建/更新用户授权的交付单 |
| `lostlink/partners.authorize` | Notification、Delivery | 验证合作方对指定交付的授权 |
| `lostlink/deliveries.read` | 已登记合作方 | 仅读取分配给自己的交付 |
| `lostlink/deliveries.update` | 已登记合作方 | 更新分配给自己的运输阶段 |

Venue 已实现 members.check 和可选 identity.resolve 内部 API；Item 已实现 items.context/items.reserve/items.index，各自检查客户端白名单。Matching 和 Embedding 使用 items.index，不读取认领参考答案。共享库已有 ClientCredentials 和 MembershipClient，其余业务 scope 与完整通用内部客户端仍待实现。默认 Cognito 触发器直接用独立只读数据库身份，不调用 identity.resolve。

## 6. 私人信息与身份目录

T0 提供 `IdentityDirectory.resolve_verified_email(subject)` 接口、测试替身及 Cognito 适配。调用服务固定用户池，按精确 `sub` 查找且只接受唯一匹配与已验证 email；结果只包含必要字段。不得把事件内任意 email 或用户提交的第三方地址当作验证后的收件人。

适配器只供 Claim/Notification 使用，不开放“任意 sub 查询邮箱”的公共接口。云端实现须确认实际用户池查询方式与 IAM 权限，提供明确的未找到、未验证、依赖失败结果，不通过读取无签名 JWT 获取邮箱。

Claim 保存的联系人快照和 Webhook 签名密钥需要加密。共享加密适配使用经审查库的认证加密，包含 key ID/nonce，AAD 绑定服务、表、记录 ID；密钥由秘密管理配置注入，不硬编码、不自制密码算法。测试使用固定测试密钥，生产密钥缺失时相关功能拒绝启动或保持未就绪。

已实现：

- `identity` 模块：`CognitoIdentityDirectory` 在固定用户池按 `sub = "..."` 精确过滤（转义引号），仅请求 `sub,email,email_verified`，最多三页；结果须唯一、账号未停用、`email_verified="true"` 且邮箱格式有效。失败分为 `IdentityNotFound`（无、多个或停用）、`EmailNotVerified`、`IdentityDirectoryUnavailable`（AWS 错误、超时、分页超限，可重试），各有稳定 `code`。`VerifiedEmail` 的 repr 不含邮箱。`StaticIdentityDirectory` 仅供测试/本地。所需 IAM 只有该用户池的 `cognito-idp:ListUsers`。
- `crypto` 模块：`FieldCipher` 使用 `cryptography` 的 AES-256-GCM，每值随机 96 位 nonce；存储格式为版本字节 + nonce + 密文/标签，key ID 另列保存。AAD 采用长度前缀编码，绑定固定域标签、服务、表、列、记录 UUID 和 key ID，因此密文被移到其他行/列或改标 key ID 都会解密失败。密钥配置为 JSON 对象 `{"key-id": "base64 的 32 字节"}` 加当前 key ID，最多 16 个；仅当前 key 加密，旧 key 可解密，`needs_rotation` 用于重加密。`from_settings` 两项都缺省时返回 None（功能未就绪），只配一项或格式错误抛 `EncryptionUnavailable`。单值上限 64 KiB。
- 依赖：`cryptography` 原已通过 `PyJWT[crypto]` 锁定为 50.0.2，现显式声明于共享包；两份锁文件版本未变化。

## 7. T0 交付清单

- 新增用户上下文会话及明确的系统操作入口，并用真实 PostgreSQL 验证 owner、tenant、worker 三类边界。
- 实现请求幂等工具、受限内部客户端和身份目录接口；领域特定规则仍在服务中。
- 按 02 文档实现可靠消息能力，领域模型不放入共享库。
- 为未来状态事件扩展注册表和契约测试；保留现有五种载荷兼容性。
- 根 pytest 支持新增服务且不混淆同名模块；服务能独立测试。
- 更新依赖锁、模板使用说明和中文接入记录。依赖变动必须说明，不能因为不同 Agent 工作就各自锁一套冲突版本。

## 8. Claim 前置共享能力实施记录（2026-10-20）

范围：为 T3 Claim 和 T4 Notification 补齐共享库，不改动 Venue/Item/Matching 业务代码。

| 交付 | 位置 | 状态 |
| --- | --- | --- |
| 失主/场馆/系统会话、`get_owner_session` | `libs/src/lostlink_common/db.py` | 已实现，真实 PostgreSQL 验证 |
| 失主或场馆 RLS 辅助函数 | 同上 | 已实现，验证叠加可见范围 |
| 主体格式校验 `validate_subject` | `auth.py` | 已实现，会话与身份目录共用 |
| 字段加密 `FieldCipher` | `crypto.py` | 已实现 |
| 身份目录 | `identity.py` | 已实现 Cognito 适配与测试替身；真实用户池未联调 |
| `claim.handed_over`、`claim.cancelled` | `event_schemas.py` | 已注册，见 [02 第 6 节](02-reliable-messaging.md) |
| `aws_client` 支持 `cognito-idp` | `events.py` | 已实现 |

验证（仓库根目录，Docker PostgreSQL/pgvector 与 LocalStack）：

```powershell
$env:LOSTLINK_TEST_ADMIN_URL='postgresql+psycopg://lostlink:lostlink@localhost:5432/lostlink'
$env:LOSTLINK_TEST_AWS_ENDPOINT='http://localhost:4566'
$env:LOSTLINK_TEST_SCALE='1'
$env:CLIP_CHECKPOINT='C:\Workspace\GitHub\lostlink\.models\ViT-B-32.pt'
$env:CLIP_SHA256='40d365715913c9da98579312b702a82c18be219cc2a73407c4526f58eba950af'
.\.venv\Scripts\python.exe -m pytest -q --tb=short -rs
.\.venv\Scripts\ruff.exe check .
.\.venv\Scripts\ruff.exe format --check .
```

结果：共享库 119 项通过；全仓回归 234 项通过（原 178 项 + 本轮新增 56 项），无 skip，6 条警告仍为 UC3 已知的 TorchScript 兼容提示；Ruff 检查与格式检查通过。新增测试覆盖：失主跨场馆只见本人记录且不能代他人写入、员工只见本场馆且不能跨场馆写入、员工兼 owner 的叠加可见、系统会话不可见 RLS 行但保留角色授权、连接复用不残留上下文、M2M/员工令牌不能取得失主会话；加密的随机 nonce、位置绑定、篡改与改标、轮换和错误配置；身份目录的转义、唯一性、停用、未验证、分页上限及 AWS 故障。

仍待后续：

- 通用幂等工具与完整 `ServiceClient` 未抽取。Claim 首版可参照 Item 的服务内实现，在 `claim` schema 自建表；是否抽取共享实现另行决定，不阻塞 T3。
- `claim.delivery_authorized`、`claim.delivery_revoked` 属于 T5，尚未注册。
- 真实 Cognito 用户池、IAM 与密钥存储由云端负责人配置，接入要求见[团队清单](../team-alignment.md)。
