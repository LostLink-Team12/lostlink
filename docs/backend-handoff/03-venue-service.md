# Venue Service 设计与实施任务

任务：T1。目录：`services/venue-service/`。覆盖 UC1。前置：[统一访问契约](01-contracts-and-access.md)、[可靠消息](02-reliable-messaging.md)。

实施状态：UC1 服务及本地验证已完成，详见 [执行记录](../uc1-implementation.md)和[运行说明](../../services/venue-service/README.md)。采用直接数据库身份触发器、版本化会话撤销任务；真实云端身份与其他业务服务接入仍待联调。

## 1. 范围

负责场馆入驻、当前成员关系和场馆内角色。Cognito 承担用户登录；Venue 不保存用户密码，也不自行签发替代 Cognito 的访问令牌。

首期由 `platform_admin` 创建场馆及首位管理员，不开放匿名入驻即管理员的接口。用户账号先在 Cognito 建立，Venue 绑定其 `sub`。邀请邮件和自助注册审核属于后续扩展。

## 2. 数据模型

| 表 | 字段与约束 |
| --- | --- |
| `venues` | `id`（即 tenant ID）、`name`（1—120 字符）、`slug`（唯一小写标识）、`status`（`active/suspended`）、`version`、创建/更新时间 |
| `memberships` | `id`、`tenant_id`、`user_sub`、`role`（`venue_admin/venue_staff`）、`status`（`active/suspended`）、`version`；唯一 `(tenant_id,user_sub)`；活跃成员对 `user_sub` 建部分唯一约束 |
| `identity_sync_jobs` | 操作 ID、成员 ID、期望成员版本、状态、租约、尝试数、最后错误码 |
| 公共 operational 表 | 发件箱、幂等、审计，遵循 02 |

`memberships` 使用租户 RLS。`venues` 是最小全局目录：公开/普通内部查询仅返回场馆名称和 ID；运营入口使用独立权限更新，员工只查看自身场馆。全局运营不能因此获得物品或联系方式的读取权。

`identity_sync_jobs` 存放待应用的身份变化，不保存客户端密码或用户访问令牌。

## 3. API

除只读接口外遵循幂等要求；修改已存在资源时使用 `If-Match`。

| 方法与路径 | 身份 | 请求核心字段 | 结果 |
| --- | --- | --- | --- |
| `POST /v1/admin/venues` | 运营 | `name,slug,initial_admin_sub` | 201，场馆和首位成员；身份同步状态 |
| `GET /v1/admin/venues` | 运营 | 分页 | 全局最小场馆目录 |
| `GET /v1/admin/venues/{id}` | 运营 | 无 | 场馆详情与 ETag，供状态管理 |
| `GET /v1/venues/current` | 当前员工 | 无 | 自己场馆 ID、名称、状态、版本 |
| `GET /v1/venues/current/members` | 场馆管理员 | 分页 | 成员 ID、sub、角色、状态、版本 |
| `POST /v1/venues/current/members` | 场馆管理员 | `user_sub,role` | 201，成员记录 |
| `PATCH /v1/venues/current/members/{id}` | 场馆管理员 | `role` 和/或 `status` | 200，更新后成员及同步状态 |
| `PATCH /v1/admin/venues/{id}` | 运营 | `status` | 200，变更后的场馆 |
| `GET /v1/admin/identity-sync/{operation_id}` | 运营或本场馆管理员 | 无 | 同步状态；不包含秘密 |
| `POST /internal/v1/members/check` | 允许服务 + `lostlink/members.check` | `subject,tenant_id,allowed_roles` | 200，`active,effective_role,membership_version,venue_status` |
| `POST /internal/v1/identity/resolve` | 可信触发器适配 + `lostlink/identity.resolve` | `subject` | 200，当前活动场馆与角色；没有关系则明确返回空 |

内部 `allowed_roles` 只允许已登记角色集合；调用者 scope 不等于允许其代任意用户创建成员关系。员工 UI 不暴露运营或内部入口。

## 4. 核心业务规则

创建场馆在一个事务中写场馆、初始管理员、审计及身份同步任务。Cognito 同步失败不会导致“场馆存在但错误宣称登录已可用”，响应明确 `identity_sync_status=pending`，可继续重试。

成员停用先在本地事务生效，随后同步身份。敏感操作通过实时成员检查拒绝旧访问令牌，因此安全性不依赖旧令牌立即失效。恢复或提升权限只在可信成员关系和新令牌都满足时生效。

不能删除或降级最后一个活跃场馆管理员。管理员不能停用自己并使场馆失去管理员。并发变更通过锁定对应场馆行并在同一事务重新计算活跃管理员数量实现，不依赖请求前的计数。

成员只能是 `venue_admin` 或 `venue_staff`，员工管理 API 不能赋予 `platform_admin`。首期活跃成员唯一约束阻止同一用户同时加入两个活动场馆；迁移场馆须先停用旧成员，再建立新关系并重新获取令牌。

场馆 suspended 时拒绝成员管理和业务写入；数据保留可供运营恢复。其他服务敏感写入须检查 `venue_status`，不能只检查成员状态。

## 5. Cognito 适配

T1 提供触发器适配实现及输入/输出测试，具体 IaC 由云端负责人接入。触发器查询可信成员目录，将场馆和角色注入访问令牌；没有活动场馆的 owner 不附加租户。

由于令牌生成本身不能递归依赖“先取得同一触发器生成的令牌”，触发器接入身份解析须使用独立工作负载凭据。例如独立 M2M 客户端且其 client credentials 流不触发成员解析，或经过 IAM 验证的内部适配入口。必须测试不存在令牌生成递归；不要在触发器中使用用户可控制的租户属性。

同步任务以成员版本为条件，旧任务不能覆盖新成员状态。需要更新 Cognito 组时保留与本模块无关的组；不得把其他用户组整体覆盖成单个角色。若已由触发器动态生成有效角色，可以把同步任务限定为身份属性与撤销流程，文档中说明实际采用的方式。

## 6. 验收与移交

- 两个场馆成员互不可见、互不可修改；运营创建流程不要求伪造租户令牌。
- 最后管理员的并发降级被阻止；普通员工不能提权。
- 被停用员工持旧 JWT 调用 Item/Claim 敏感写入被拒绝；身份系统故障不放行。
- 身份同步重复、乱序和失败可恢复，不导致本地成员状态回退。
- 用户没有场馆时仍能作为失主使用 Owner API。
- 提供数据库迁移、Cognito 适配测试、服务 OpenAPI、本地替身配置及云端接入说明。

交给 Agent 的任务范围是 Venue 和可信身份接入，不包含 Item/Claim 业务实现；只需用契约测试验证成员检查客户端。完成后把客户端映射、触发器配置及角色约定更新到团队清单。
