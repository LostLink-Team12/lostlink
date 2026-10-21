# 架构审阅：Phase 1 后端与 A1 契约

审阅人：YANG DONGWEI（架构负责人）。日期：2026-10-21。范围：`phase1-backend` 分支上的 `docs/api/` 契约、`libs/`、`matching-service`，以及与 ADR 的一致性。

**结论：契约通过，附 3 项高优先级修改。** 接口约定（access token、幂等 key、`If-Match`、202 操作轮询、分页、错误体）在五个服务里一致，事件信封和十种事件的 schema 完整且由代码生成。下面的问题主要影响扩容演示和前端对接，不影响 Phase 1 功能。

## 发现与处理

| # | 优先级 | 发现 | 处理 | 负责人 |
| --- | --- | --- | --- | --- |
| 1 | 高 | Embedding 的积压在 PostgreSQL 的 `matching.work_jobs` 里，SQS 只是转存，批量导入时 SQS 深度接近 0。按 ADR-0002 原方案用 SQS 深度驱动 KEDA，演示时 worker 不会扩容。 | 已修订 ADR-0002：`tasks` 进程改用 KEDA `postgresql` scaler，查询待处理的 `embed` 任务数，目标约 20 个/副本，`minReplicaCount: 1`。需要一个只读数据库角色给 KEDA。 | LU JINGXING |
| 2 | 高 | `POST /v1/search` 对每个候选物品同步调用一次 Item 内部接口（`item_context`），候选窗口最多 100 个。压测时搜索延迟和 Item 负载都会随之放大。 | 先用 k6 测 p95；若超过 500 ms，加批量内部接口（一次传多个 `item_id`）或 30 秒内的短缓存（ElastiCache）。 | JIANG YIBAI、YANG DONGWEI |
| 3 | 高 | OpenAPI 只声明了 2xx 和 422 响应。接入指南里的 401/403/404/409/412/428/503 和统一错误体 `{"error":{"code","message"},"request_id"}` 没有写进契约，前端无法据此生成类型和分支。 | 在各路由加公共 `responses`（错误体 schema 放在 `libs` 里复用），重新运行 `scripts/export_contracts.py`。 | JIANG YIBAI |
| 4 | 中 | 自动匹配阈值 0.25 未标定。文字查询对"文字+照片"融合向量的分数明显低于照片查询，单一阈值可能让文字查询漏报或让照片查询误报。 | 新增 `scripts/calibrate_match_threshold.py` 和 `matching_app/calibration.py`（含单元测试），按查询方式分别给出满足精确率目标的最低阈值。需要 D1 演示数据里约 100 对带标签的查询/物品对。 | ZHANG JI 提供数据，YANG DONGWEI 标定 |
| 5 | 中 | 搜索在 API 请求内跑 CLIP 推理，API Pod 是 CPU 密集型。 | ADR-0007 记录；部署时给 API 设 CPU request/limit 和基于 CPU 的 HPA，压测前预热（`/ready` 会先加载模型）。 | LU JINGXING |
| 6 | 中 | 契约由代码生成，但 CI 没有检查是否过期。 | 在 CI 的 `shared-library` 作业加 `python scripts/export_contracts.py --check`。CI 文件目前在 `codex/ci-ecr-oidc` 分支也在改，合并时一起加，避免冲突。 | LU JINGXING |
| 7 | 低 | proposal 里独立的 `embedding-worker` 服务实际放进了 `matching-service`。 | 新增 ADR-0007 记录理由；事件 `source` 保持 `embedding-worker`，以后拆分时消费方不受影响。服务清单已更新。 | YANG DONGWEI |
| 8 | 信息 | 所有服务采用事务发件箱、收件箱去重和数据库任务表，但没有 ADR。 | 新增 ADR-0008，报告的"架构决策"和"可用性"章节可直接引用。 | YANG DONGWEI |

## 契约检查清单

- [x] 外部路由要求 access token，租户只来自令牌，客户端不能提交 `tenant_id`
- [x] 创建和动作型 POST 都带 `Idempotency-Key`；修改带 `If-Match`（Claim 6/6、Item 11 处、Venue 4/4）
- [x] 异步动作返回 202 和操作资源
- [x] `/internal/v1/*` 只供机器客户端，网关不能暴露（需在 API Gateway 路由里显式排除）
- [x] 分数语义是余弦相似度（-1 到 1），不是概率
- [ ] 错误响应写入契约（见第 3 项）
- [ ] CI 检查契约是否过期（见第 6 项）

## Seed 2 准备

`delivery-service` 已建骨架：运输状态机（`delivery_app/domain.py`）和 `claim.handed_over` / `claim.cancelled` 的处理逻辑（`delivery_app/handlers.py`），带单元测试；设计见 ADR-0009。Phase 2 需要补持久化、消费进程、`delivery.status_changed` 事件 schema，以及由 JIANG YIBAI 实现的交付授权、Partner API 和 Webhook。
