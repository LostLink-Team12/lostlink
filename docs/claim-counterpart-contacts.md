# Claim 双方联系方式

## 行为与业务规则

- 场馆工作人员在 Claims to review 展开 Claim 后，直接看到认领人的姓名、电话及经过 Cognito 验证的邮箱；姓名和电话未填写时显示 Not provided。
- 按本次需求，认领人联系方式从 submitted 阶段向所属场馆开放，包含审批处理中及等待交接阶段；拒绝、撤回或已交接后不再向场馆展示。此项替代 proposal 原先“审批后才向场馆共享联系方式”的规则，需要通知负责安全验收和演示的成员。
- 工作人员审批时填写场馆联系人姓名与可选电话；回复邮箱由后端从该工作人员 Cognito 已验证邮箱取得，不能通过请求伪造。它表示负责此 Claim 的工作人员，不是虚构的场馆统一邮箱。
- Owner 在 My claims 展开已通过审批的 Claim 后看到场馆联系方式，交接完成仍可查看；审批前及撤回后不可查看。
- 既有已审批 Claim 没有场馆联系信息时提示尚未提供；工作人员可以在同一个 Claim 内填写并 Save venue contact，无需重复审批。
- 补填或修改仍限所属场馆、非自行认领的工作人员；使用版本检查和幂等键，联系方式读取和共享均审计。

## 接口与存储

- GET /v1/venue/claims/{id}/contact：读取认领人联系方式，扩展允许的活跃状态。
- POST /v1/venue/claims/{id}/approve：新增可选 contact（name、phone）。兼容旧的空请求体，前端审批表单会提交联系人。
- GET /v1/claims/{id}/venue-contact：失主在审批后读取对方联系方式，受认领归属和状态限制。
- PUT /v1/venue/claims/{id}/reply-contact：场馆补填或修改已审批、等待交接的 Claim 联系方式；需要 If-Match、Idempotency-Key。
- 复用 claim_contacts 的 AES-GCM 加密 JSON，增加内部 venue_contact 对象；双方接口分别抽取自己的视图，不把对方信息混入 Owner 自身联系方式接口。列表、事件、审计正文和幂等响应快照均不包含联系方式。
- 新增对方联系方式读取返回 Cache-Control: no-store。前端隐藏详情、更换 Claim 或状态变化时清理显示；读取失败可在详情中独立重试。

## 部署与验证

不增加数据库列或 Alembic revision，但修改 migrations/env.py 中 RLS 和加密列更新权限。上线前必须执行 Claim 的 alembic upgrade head，刷新权限及行级策略。仅授予 key_id、encrypted_contact 的列级更新权限，不允许修改归属、租户。CD 的迁移哈希保护会检测此变动，需要按既有流程执行迁移并审批基线，再发布应用；本次不触发部署。

补充前端双方展示、审批提交、旧 Claim 补填、状态变化和失败重试测试；后端补充加密视图隔离与状态门禁测试，以及 PostgreSQL 下的跨失主/跨场馆隔离、审批幂等、版本冲突、旧数据补填集成测试。本机没有运行中的 PostgreSQL/Docker，真实 RLS 集成验证交由 CI。

本地验证：前端完整测试 111 项、Claim 非集成测试 27 项通过；TypeScript 与生产构建、Ruff 和 OpenAPI 同步检查通过。PostgreSQL 集成测试已新增，尚未在本机执行。
