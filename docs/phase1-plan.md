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

## Daily rhythm

- 10-minute stand-up at 10pm each day: done, next, blocked.
- Log effort hours in each PR (`Effort: 3h`); needed for progress reports and the Seed 1 vs Seed 2 comparison.

## Fallback if Phase 1 slips

Keep the cloud deployment, and cut in this order: analytics dashboard, then partner webhooks, then photo search (keep text search).
