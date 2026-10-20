# 后端后续工作移交入口

编写日期：2026-10-20。设计基线：`phase1-backend`，提交 `4b0756c`。本目录记录实施方案和进度；除明确记录为已实现的能力外，其余服务、表、接口和后台进程仍待实施。

目标是让其他 Agent 在不依赖本次聊天历史的情况下，完成 Jiang Yibai 负责的后端工作。范围包括共享能力补齐、Venue、Item、Claim、Notification、Partner API 及联合验收；匹配算法、embedding 模型、正式前端和云资源由原负责人负责，这里定义其接入契约。

## 阅读顺序与权威性

1. 阅读[已完成的基础能力](../backend-foundation.md)，区分可复用代码与待实现能力。
2. 阅读[统一契约与权限](01-contracts-and-access.md)和[可靠消息](02-reliable-messaging.md)。
3. 根据任务阅读相应服务设计。
4. 阅读[联合验收](08-integration-and-acceptance.md)，准备交付证据。

本目录细化并在明确注明时修订[原后续计划](../backend-roadmap.md)。旧文档中的简化状态图以本目录的完整状态及失败恢复语义为准。基础阶段五种版本一事件是兼容性基线；实施进度以本文状态和各任务执行记录为准，未标为已实现的接口与事件仍是设计。

2026-10-20 进度：T1/UC1 和 T2/UC2 已实现并通过本地验证，见 [UC1 记录](../uc1-implementation.md)和 [UC2 记录](../uc2-implementation.md)。Item 已通过真实 Venue 接口验证权限，并通过本地消息/对象存储测试；Cognito/IAM、CLIP/Matching 和真实 Claim 联调仍待接入。T0 仅完成这两个任务必需部分，T3—T6 仍待实施。

UC3 后续更新：用户要求继续实施，现已补齐 Matching、真实 CLIP 适配和独立 Embedding 任务进程，并通过真实模型、PostgreSQL/pgvector、Item API 与 LocalStack 的本地验证。原范围说明中的“匹配算法及 embedding 由原负责人负责”保留为团队分工背景，不再表示代码缺失；接手者先读 [09：Matching/Embedding](09-matching-and-embedding.md)和 [UC3 记录](../uc3-implementation.md)，避免重复实现。Cognito/IAM/SES、真实 Claim 和云端容量验收仍未完成。

T0 Claim 前置更新（2026-10-20）：共享库已补齐失主/场馆/系统会话、失主或场馆 RLS、字段加密、身份目录及 `claim.handed_over`/`claim.cancelled` 注册，见 [01 第 8 节](01-contracts-and-access.md)。通用幂等与 `ServiceClient` 仍在服务内实现。

T3/UC5 更新（2026-10-20）：Claim Service 已实现并通过真实 Venue/Item 本地集成验证，见 [UC5 记录](../uc5-implementation.md)。

T4/UC4 更新（2026-10-20）：Notification Service 邮件能力已实现，并以 Matching 真实产生的 `match.found` 完成本地验证，见 [UC4 记录](../uc4-implementation.md)。Phase 1 后端服务均已落地；剩余为 T6 联合验收/部署交接，T5 属于第二阶段。

| 文档 | 内容 |
| --- | --- |
| [01：统一契约与权限](01-contracts-and-access.md) | 身份、数据库访问模式、接口通则、内部调用、隐私、共享包缺口 |
| [02：可靠消息](02-reliable-messaging.md) | 发件箱、消费去重、租约、乱序、重试、事件扩展 |
| [03：Venue](03-venue-service.md) | 场馆、成员、角色、入驻引导、令牌中的租户来源 |
| [04：Item](04-item-service.md) | 登记、照片封存、状态、批量导入、预留与交接接口 |
| [05：Claim](05-claim-service.md) | 认领、审批协调、QR、联系方式、取消及恢复 |
| [06：Notification](06-notification-service.md) | 匹配通知、身份目录、SES、发送幂等与恢复 |
| [07：Partner](07-partner-api-and-webhooks.md) | 合作方授权、交付范围、Webhook、Seed 2 接口 |
| [08：联合验收](08-integration-and-acceptance.md) | 依赖矩阵、迁移、测试、演示步骤、交付清单 |
| [09：Matching/Embedding](09-matching-and-embedding.md) | UC3 已实现契约、模型/索引边界、报告与搜索、乱序与重试、UC4 接入 |

## 不需要重新询问的工作规则

- 业务逻辑由实施 Agent 按本目录设计推进；实现细节可自行补充，影响接口或边界的决定须更新中文文档。
- 需要其他成员对齐的内容追加到[团队清单](../team-alignment.md)，不要把等待逐项确认作为默认前置条件。没有明确授权时不向队友或其他聊天发送消息。
- `docs/` 内说明使用中文；代码、配置、测试名称、日志和其他目录说明使用英文。
- 不自动 commit、push、创建 PR 或合并；用户手动复查及提交。
- 开始前检查当前分支、未提交改动及适用的 `AGENTS.md`。不覆盖他人改动，不重置工作区。
- 默认先完成所分配任务。不要因本目录列出其他任务就顺便全部实现，也不要自行启动其他 Agent。
- 云端资源变更、真实发信、对外 Webhook 及生产部署不包含在普通代码实现任务中；本地验证使用测试收件器和模拟合作方。

## 任务拆分与依赖

| 编号 | 可移交任务 | 前置条件 | 主要改动范围 |
| --- | --- | --- | --- |
| T0 | 补齐统一访问、幂等、事件可靠性及内部客户端 | 现有共享库 | `libs/`、模板、依赖锁、共享测试；01、02 文档 |
| T1 | Venue Service 与身份引导适配 | T0 的访问契约 | `services/venue-service/`、身份触发器适配、03 文档 |
| T2 | Item Service、照片与批量导入 | T0；T1 可先用契约替身 | `services/item-service/`、04 文档 |
| T3 | Claim Service、审批及交接 | T0；T2 的内部接口 | `services/claim-service/`、05 文档 |
| T4 | Notification Service 邮件能力 | T0；`match.found` 已有契约 | `services/notification-service/`、06 文档 |
| T5 | Partner API 与 Webhooks | T3、T4 | Claim/Notification 的合作方模块、07 文档 |
| T6 | 联合验收与部署交接 | T1—T4；T5 作为第二阶段 | 集成测试、开发编排、08 文档和验证记录 |

T0 完成后，T1、T2、T4 可以分开交给不同 Agent；T3 可先用 T2 的契约替身开发，但不能在真实联调前标为完成。T4 和 T5 都会修改 Notification，建议顺序执行。统一事件注册表、依赖锁和 CI 由一个整合者维护，避免多个任务同时改写。

建议交付粒度：每个任务先做数据迁移与接口正常流程，再做权限、并发、失败恢复，最后补集成证据；不要把权限或幂等留作“以后再补”。

## 可直接复制的 Agent 任务说明

将下面的 `Tn`、文档名和目录替换为表中目标即可。各任务对应文档末尾也列出任务特定要求。

> 在 LostLink 仓库当前分支实现任务 Tn。先读取 docs/backend-handoff/README.md、01-contracts-and-access.md、02-reliable-messaging.md 和对应服务设计；检查实际代码，区分已实现与设计中的能力。按文档完成模型、迁移、API、后台处理及必要测试。你自行决定实现细节，需要团队接入的事项写入 docs/team-alignment.md，不必逐项询问我。docs 使用中文，其余代码库使用英文。保持任务范围，保护他人改动；缺少外部服务时实现明确的测试替身，并列出真实联调缺口。完成后报告改动、运行命令、测试结果及限制。不要自动 commit、push、创建 PR 或发送外部消息。

## 交接基线

- 已存在 `lostlink-common`、模板迁移、JWT/RLS/事件传输和基础 CI。
- `Database.session` 仍只接受租户 UUID；失主、场馆和后台入口改用 `actor_session(principal, venue_scope=...)`、`scoped_session` 与 `system_session`，不得调用 `session(None)`。
- 事件注册表现有十种事件：基础五种、venue.membership_changed、item.catalog_changed、item.embedding_failed、claim.handed_over、claim.cancelled，均禁止多余载荷字段；T5 的交付授权事件仍待注册。
- Venue 和 Item 各有自己的发件箱、幂等表、后台租约及受限访问角色，Item 还有事务 inbox；共享库已有成员检查、M2M 令牌客户端、字段加密与身份目录。通用幂等工具及其他服务的可靠性实现仍待补齐。
- 根测试入口已包含模板、共享库、Venue 和 Item；新增服务时仍须检查测试收集、同名 app 导入、CI、构建上下文及依赖锁。
- 71 项测试通过是基础阶段的历史记录。接手 Agent 应运行与自己改动相关的测试，记录本轮结果，不直接复用旧结果作为完成证据。
