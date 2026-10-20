# LostLink

**Cross-venue, AI-assisted lost-and-found platform.**
NUS-ISS Graduate Certificate in Architecting Scalable Systems, Practice Module (SE34FT), Team 12.

LostLink connects the lost-and-found counters of malls, MRT stations, campuses and event venues into one platform.
Venues (producers) register found items; owners (consumers) search every venue at once by text or photo;
partners (e.g. couriers) build services such as return delivery on top of the platform APIs and events.

## Team

| Member | Role | Owns |
| --- | --- | --- |
| YANG DONGWEI | Solution Architect, AI Matching Lead | `docs/`, `services/matching-service`, `services/embedding-worker`, `services/delivery-service` |
| JIANG YIBAI | Platform Backend Lead | `libs/`, `services/venue-service`, `services/item-service`, `services/claim-service`, `services/notification-service` |
| LU JINGXING | Cloud and DevSecOps Lead | `infra/`, `.github/workflows/` |
| ZHANG JI | Frontend and Quality Lead | `frontend/`, `tests/` |

## Repository layout

```
.github/          CI pipeline, PR template
docs/             architecture, ADRs, phase plans
libs/             shared Python libraries (auth, tenancy, events)
services/         one FastAPI microservice per bounded context
  service-template/   golden-path template: copy this to start a new service
frontend/         venue-portal and owner-app (React + Vite)
infra/            Terraform (AWS) and Helm charts
tests/load/       k6 performance and scaling scripts
docker-compose.yml  local dependencies (Postgres+pgvector, Redis, LocalStack)
```

## Tech stack (see `docs/adr/`)

- Services: Python 3.12, FastAPI, SQLAlchemy, Alembic
- AI matching: open-source CLIP (ViT-B/32) embeddings, PostgreSQL `pgvector`
- AWS: EKS, RDS PostgreSQL, S3, SQS, SNS, ElastiCache Redis, Cognito, SES, API Gateway, WAF
- Delivery: GitHub Actions, Trivy, Docker, Amazon ECR, Helm, Terraform
- Scaling: HPA for APIs, KEDA on SQS queue depth for embedding workers
- Frontend: React + Vite + TypeScript
- Tests: pytest, k6

## Getting started

```bash
git clone https://github.com/LostLink-Team12/lostlink.git
cd lostlink
docker compose up -d                 # Postgres+pgvector :5432, Redis :6379, LocalStack :4566
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements-dev.lock -e ./libs
python -m pytest -q -m "not integration"
cd services/service-template
uvicorn app.main:app --reload --no-access-log      # http://localhost:8000/health
```

The template now includes protected example APIs and shared backend foundations.
Follow [the template setup](services/service-template/README.md) to initialize
the restricted database role, apply migrations, and configure Cognito.
Without those settings `/health` is 200 and `/ready` is intentionally 503.

UC1 is implemented in [Venue Service](services/venue-service/README.md), including
onboarding, tenant-isolated staff management, current membership checks, durable
identity tasks and a Cognito pre-token adapter. Follow its setup for separate
database roles, migrations and client allowlists.

UC2 is implemented in [Item Service](services/item-service/README.md): item CRUD,
private verification questions, immutable photo uploads, resumable bulk imports,
embedding-result consumption and Claim reservation contracts. Use
[the import script](scripts/import_items.py) to register and upload local photo manifests.

Backend design and next steps:
[foundation](docs/backend-foundation.md), [roadmap](docs/backend-roadmap.md),
[team alignment](docs/team-alignment.md).

## How we work

Read [CONTRIBUTING.md](CONTRIBUTING.md). In short: never push to `main`; branch, open a pull request, get one review and a green CI, then squash-merge.

Current plan: [docs/phase1-plan.md](docs/phase1-plan.md)
