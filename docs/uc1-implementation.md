# UC1 实现与接入记录

本轮在 `phase1-backend` 实现场馆入驻与员工角色管理。设计来源：[UC1 移交设计](backend-handoff/03-venue-service.md)。本记录区分本地已实现能力和仍需云端接入的步骤，不表示 Cognito 或其他业务服务已经真实联调。

## 实际交付

- `services/venue-service/venue_app`：独立服务包、严格请求/响应模型、OpenAPI、探针、日志和指标。
- 场馆运营创建、分页查询、详情与停用/恢复；首位管理员与场馆在一个事务建立。
- 员工查看当前场馆；管理员列出、新增、修改角色及停用/恢复本场馆成员。
- 一个用户最多一个活动场馆；同场馆成员不重复插入；最后一名活动管理员不能被停用或降级。
- 当前成员检查和受限身份解析接口；同时检查访问令牌、客户端白名单及内部 scope。
- PostgreSQL `venue` schema、Alembic 迁移、四种运行身份、强制 RLS；事务内幂等、审计、身份任务和成员变更发件箱。
- 独立 outbox/identity 后台入口，租约、条件确认、重试预算、过时任务跳过及带审计的人工恢复。
- Cognito V2 令牌触发器和会话撤销适配；共享 `ClientCredentials`、`MembershipClient` 供后续 Item/Claim 接入。
- 运行说明、环境示例、独立 Dockerfile；根 pytest 与现有动态服务 CI 可以发现新服务。无新增第三方依赖，沿用现有锁文件。

## 本轮确定的业务语义

### 入驻和账号

由可信部署人员在 Cognito 建立运营账号及 `platform_admin` 组关系，运营应用客户端单独列入白名单。运营随后创建场馆并绑定已有用户的 Cognito `sub` 为首位管理员；普通员工接口不能授予运营身份。员工账号也先在 Cognito 建立，再加入成员表。邀请邮件、自助审核与密码管理不由 Venue 实现。

新建场馆/成员返回 `identity_sync_status=pending` 和操作 ID，表示会话撤销任务已持久化。用户不存在时任务明确失败，修复账号后可重放；不会把身份提供方故障伪装为成功。获取新令牌时，以实时成员目录生成角色为准，不依赖异步同步完成。

### 权限和并发

外部租户只来自已验证令牌。员工每次操作都检查当前成员状态、令牌角色与当前角色是否一致；敏感写入还检查场馆 active。旧管理员令牌在降级后不能继续管理成员，旧员工令牌也不能直接享受新提升的管理员权限。

修改成员和修改场馆状态都锁定同一场馆行，在锁后重新读取权限和管理员数量。数据库部分唯一索引保证用户不会并发加入两个活动场馆。成员表、场馆表没有跨服务外键。

写入均要求 `Idempotency-Key`，PATCH 同时要求 `If-Match`。幂等作用域包括客户端、用户、当前租户、操作路径和 key，防止换场馆后误重放旧结果。先检查当前权限，再重放结果；首轮执行才比较当前版本。记录保存 24 小时，关键唯一约束继续保护资源。

### 身份触发器

采用直接读取数据库的方案，Lambda 只持有 `IDENTITY_DATABASE_URL`；成员 RLS 根据事务内的可信 `sub` 过滤。触发器不获取 M2M 令牌、不回调用户鉴权接口，不产生令牌生成递归。身份目录故障时拒绝签发新令牌。

触发器清除持久用户组中的旧 `venue_admin/venue_staff`，保留其他可信组，添加 owner，再从当前目录附加场馆角色及 `custom:tenant_id`。无活动成员或场馆停用时不附加租户。后台只执行已有账号的全局会话退出，不回写 Cognito 用户组、租户属性，因此旧任务不能恢复旧权限；检查时已过时的任务标记 superseded。

Cognito 需要启用 `V2_0` 才能定制用户 access token，当前方案要求相应功能套餐；`V1_0` 不满足本方案。部署参数以 [AWS 官方触发器文档](https://docs.aws.amazon.com/cognito/latest/developerguide/user-pool-lambda-pre-token-generation.html)为准。代码也对机器凭据触发源直接返回，不把机器主体解析为员工。

### 运行角色与任务恢复

| 身份 | 权限边界 |
| --- | --- |
| API | 本场馆业务读写、事务操作记录写入；不能确认事件投递或修改场馆状态 |
| Platform | 全局场馆目录、入驻与状态管理；不读取其他服务 schema |
| Worker | 本服务任务与 outbox 认领/确认、审计追加；领域表按任务租户只读 |
| Identity | 场馆目录只读、按 sub 读取自身成员关系；无写权限 |
| Migration | 独立 DDL 凭据，不注入运行容器 |

API 对场馆 `version` 只获得列级 UPDATE 权限以满足 PostgreSQL 行锁要求，不能修改 status/name/slug。outbox/审计只额外授予 ORM INSERT RETURNING 所需的元数据列读取权，不向 API 开放事件正文或完整审计查询。

后台通过短事务领取 60 秒租约，外部请求不占用领域事务；确认使用 lease token 条件。事件重复投递保持原 event ID。身份任务同时绑定成员版本和场馆版本，避免旧停用/恢复任务误认为当前状态。

任务最多主动尝试 10 次，失败不删除。受信任运维通过 worker CLI 重放，仅接受 failed 状态，原子重置预算并追加审计。CLI 的 operator 是审计归属字段，授权来自受保护的 worker 凭据，不能把填写 operator 当作认证。

## 启动与核验

可执行的角色创建、迁移、API、worker、触发器及容器步骤见 [服务 README](../services/venue-service/README.md)。

本地测试使用随机独立数据库和四种非超级用户角色，验证真正 PostgreSQL RLS、事务及行锁；消息链路使用 LocalStack。Cognito 的用户查找/撤销及触发器事件使用测试适配，不调用真实 AWS 账号。

本轮完整回归 `python -m pytest -q`：106 项通过，其中 83 项为单元测试、23 项为 PostgreSQL/LocalStack 集成测试。运行时设置了 LOSTLINK_TEST_ADMIN_URL 和 LOSTLINK_TEST_AWS_ENDPOINT；沙箱不允许本地端口/事件循环通信的检查使用了外部执行权限。本轮新增 35 项测试，包含角色隔离、并发管理员保护、幂等重放、旧权限拒绝、身份失败恢复、租约 CAS、审计重放和真实 SNS→SQS 投递。

`ruff check .` 和 `ruff format --check .` 通过；Linux 镜像 lostlink/venue-service:uc1 构建通过。Cognito/云端检查仍使用适配器测试，不计入真实云端验证。

临时容器以 UID 1000 运行，pip check 通过；真实 HTTP 检查 /health=200、未配置时 /ready=503、未登录访问当前场馆=401，OpenAPI 可读取。检查结束后临时容器已删除，未常驻额外端口。

## 对后续任务的影响

- T1/UC1 的服务实现已落地；全局 T0 仅补齐本次必需的共享客户端及事件，不能据此标记全部完成。
- Venue 的显式访问会话、幂等和任务表仍在服务内。后续抽取通用实现时保持事务、RLS 与权限语义，不复制为 `session(None)` 全局访问模式。
- 通用 owner 数据会话、其他服务的 outbox/inbox、加密、其余新事件注册和更完整内部调用工具仍属于 T0/后续实施。
- Item/Claim 还未实现；本轮用共享客户端调用真实 Venue 测试接口，验证旧权限拒绝和依赖不可用返回 503，不能描述成完整跨服务联调。
- 其他事件消费者须识别 `venue.membership_changed`，但敏感写入仍实时查询成员，不仅依赖事件缓存。

## 云端与团队接入缺口

1. 云端负责人提供 Cognito 用户池、运营/员工应用、Item/Claim M2M scope、账户及可信运营组；配置 Lambda V2 触发器及只读数据库网络路径，先完成迁移再启用触发器。
2. 配置独立数据库角色及 Secrets、API/worker 进程、SNS 订阅和队列策略；identity 工作者 IAM 只允许目标用户池的 `ListUsers`、`AdminUserGlobalSignOut`。
3. 公网网关禁止转发 `/internal/v1` 和 `/metrics`，运营入口额外限制为运营应用；这不替代后端 JWT/角色校验。
4. 前端处理 pending 身份任务、ETag、幂等 key 和 409/412/428；成员新增绑定现有账号，升权后重新获取 access token。
5. Item/Claim 写接口必须接入无正向缓存的 `MembershipClient.require_current`；云端验证停用后持旧 access token 被拒绝。

这些事项只记录在文档中，未向其他成员发送消息，也未修改真实 AWS 资源。
