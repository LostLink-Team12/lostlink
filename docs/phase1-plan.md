# Phase 1 plan: walking skeleton on AWS (17 to 21 Oct 2026)

**Goal:** by Wednesday 21 Oct 23:59, Seed 1 runs end to end on the AWS dev environment, deployed by the pipeline:
a venue registers a found item with a photo, an owner finds it by text or photo search, claims it, and the venue approves.

Presentation is on **Mon 26 Oct, 9:00 to 9:40am** (Team 12), so Phase 2 (22 to 24 Oct) adds Seed 2 and the scaling demo, and 25 Oct is rehearsal.

## Day 0: Sat 17 Oct (everyone)

- [ ] Accept the GitHub organisation invitation; clone the repo; `docker compose up -d`; run the template tests
- [ ] Agree on the AWS account to use and the monthly budget alarm (LU JINGXING sets it up)
- [ ] Confirm ADR-0006 (Python + FastAPI)

## Tasks and deadlines

| ID | Task | Owner | Due | Done when |
| --- | --- | --- | --- | --- |
| A1 | OpenAPI contracts (item, matching, claim) and event schemas in `docs/api/` | YANG DONGWEI | Sun 18 Oct | Merged; backend and frontend build against it |
| A2 | embedding-worker: SQS consumer, CLIP image and text embeddings, write to pgvector | YANG DONGWEI | Tue 20 Oct | Item uploaded locally becomes searchable |
| A3 | matching-service: text and photo search, top-k with filters; re-match open lost reports, publish `match.found` | YANG DONGWEI | Wed 21 Oct | Search returns the right item in the demo dataset |
| B1 | `libs/`: Cognito JWT auth, tenant context, event publisher; DB schema and Alembic migrations | JIANG YIBAI | Sun 18 Oct | Used by every service |
| B2 | venue-service and item-service: venue onboarding, staff roles, found-item CRUD, presigned S3 upload, enqueue embedding job | JIANG YIBAI | Mon 19 Oct | UC1 and UC2 via API |
| B3 | claim-service (verification questions, venue approval, QR code) and notification-service (SES email on `match.found`) | JIANG YIBAI | Wed 21 Oct | UC4 and UC5 via API |
| C1 | Terraform: VPC (2 AZ), EKS, ECR, RDS + pgvector, S3, SQS, SNS, Cognito; remote state; budget alarm | LU JINGXING | Sun 18 Oct | `terraform apply` creates dev |
| C2 | CI: tests, Trivy, image build and push to ECR through GitHub OIDC | LU JINGXING | Mon 19 Oct | Every merge to `main` pushes images |
| C3 | CD: Helm charts, deploy to dev on merge, ALB ingress, metrics-server, KEDA, Prometheus and Grafana | LU JINGXING | Wed 21 Oct | Services reachable on dev URL with dashboards |
| D1 | Demo dataset: about 5,000 openly licensed item photos with labels; seed script | ZHANG JI | Sun 18 Oct | Dataset in S3, seed script in `tests/data/` |
| D2 | Venue Portal: login, register item with photo, list items, approve claims | ZHANG JI | Tue 20 Oct | UC1, UC2, UC5 in the browser |
| D3 | Owner App: report lost item, search by text or photo, view matches, claim; k6 smoke test | ZHANG JI | Wed 21 Oct | UC3 to UC5 in the browser |
| P1 | Progress report 1 (template from the briefing) | YANG DONGWEI | Check Canvas deadline | Submitted |
| E1 | Integration session and Phase 1 demo on dev | Everyone | Wed 21 Oct, 8pm | Seed 1 end to end on AWS |

## 后端执行进度（2026-10-21）

B1/B2/B3（2026-10-21）：JIANG YIBAI 负责的 Phase 1 后端已全部实现并通过本地验证，包括共享库、Venue（UC1）、Item（UC2）、Claim（UC5）和 Notification（UC4 邮件）；Matching/Embedding（UC3、UC4 匹配产生）也已在后端分支实现。全部服务都使用 `libs/`。每个服务均有独立迁移、受限数据库角色、Dockerfile 与 README，全仓本地回归 266 项通过（真实 PostgreSQL/pgvector、LocalStack、真实 CLIP）。A1 契约已由代码生成到 [docs/api](api/README.md)，请 YANG DONGWEI 审阅。

仍需团队完成：合入 `main` 并通过 CI；C 组配置 Cognito（V2 触发器、M2M scope）、SES、SNS/SQS 订阅与 DLQ、各服务数据库角色和秘密后部署 dev；前端按接口接入。具体要求见[后端接入指南](team-alignment.md)。Partner API 与 Webhooks 属于 Phase 2。

## 架构与 Seed 2 进度（2026-10-21，YANG DONGWEI）

A1–A3 的实现已由后端分支完成（见上）。架构负责人这边完成：

- A1 契约审阅：[架构审阅](reviews/2026-10-21-architecture-review.md)，契约通过，附 3 项高优先级修改（KEDA 伸缩信号、搜索逐个调用 Item、错误响应未写入契约）。
- ADR：修订 ADR-0002（KEDA 改按 PostgreSQL 任务积压伸缩）；新增 ADR-0007（Embedding 放在 Matching 上下文）、ADR-0008（发件箱/收件箱/任务表）、ADR-0009（Seed 2 交付授权与扫码取件）。
- 阈值标定：`scripts/calibrate_match_threshold.py` 按查询方式给出推荐阈值；等 D1 的带标签数据（约 100 对）后运行并更新 `MATCH_THRESHOLD`。
- Seed 2：`services/delivery-service` 骨架（运输状态机和 claim 事件处理，含单元测试）。

## Daily rhythm

- 10-minute stand-up at 10pm each day: done, next, blocked.
- Log effort hours in each PR (`Effort: 3h`); needed for progress reports and the Seed 1 vs Seed 2 comparison.

## Fallback if Phase 1 slips

Keep the cloud deployment, and cut in this order: analytics dashboard, then partner webhooks, then photo search (keep text search).
