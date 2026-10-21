# service-template

A working FastAPI foundation with Cognito authentication, PostgreSQL tenant
isolation, Alembic migrations, consistent errors, and Prometheus metrics.
`example_records` is demonstration data, not a LostLink domain model.

## Local setup

From the repository root (PowerShell):

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements-dev.lock -e ./libs
docker compose up -d
Get-Content scripts/init-local.sql | docker compose exec -T postgres psql -v ON_ERROR_STOP=1 -U lostlink -d lostlink
Copy-Item services/service-template/.env.example services/service-template/.env
Set-Location services/service-template
python -m alembic upgrade head
python -m uvicorn app.main:app --reload --no-access-log
```

On Bash, use `source .venv/bin/activate`, pipe `cat scripts/init-local.sql` into
the same Docker command, and use `cp`/`cd` for copying/changing directories.
Preserve an existing `.env` instead of copying over it.

Set `COGNITO_ISSUER` and the JSON array `COGNITO_CLIENT_IDS` in `.env` to use a
real user pool. Venue operations require `custom:tenant_id` in the **access
token** and the `venue_staff` or `venue_admin` Cognito group. Owner and partner
tokens may have no tenant. There is no unsigned-token or header-based auth mode.

`DATABASE_URL` uses the restricted application role. `MIGRATION_DATABASE_URL`
uses a separate schema owner; only migration jobs should receive this credential.
The checked-in passwords are exclusively for the local Compose environment.

## Endpoints

| Endpoint | Behavior |
| --- | --- |
| `GET /health` | Liveness; no external dependency calls |
| `GET /ready` | 200 only with configured identity verification, a safe DB role, and the expected migration |
| `GET /metrics` | Internal Prometheus scrape endpoint; exclude from public ingress |
| `GET /docs` | Swagger UI |
| `GET /v1/me` | Verified caller identity, roles, scopes, and optional tenant |
| `POST /v1/examples` | Venue staff create an example using `{"name": "Example"}` |
| `GET /v1/examples?limit=20&offset=0` | Venue staff list only their own tenant's examples |

Missing database/auth configuration intentionally yields `/ready` **503** while
`/health` and `/docs` remain available. Readiness checks identity configuration,
not a live Cognito call. A JWKS outage yields **503** on protected requests.
JWT failures return **401**; role, scope, or tenant failures return **403**.

Use Swagger's Authorize button with an access token. Client-supplied `tenant_id`
fields are rejected, and `X-Tenant-ID` does not select a tenant.
Every response includes `X-Request-ID`; error responses also include `request_id`.

## Tests

Run the full suite from the repository root:

```bash
python -m pytest -q -m "not integration"
```

See [shared-library instructions](../../libs/README.md) for integration tests.
When working inside this directory, `pip install -r requirements-dev.txt`
and `python -m pytest -q -m "not integration"` also work.

## Container

Build with the **repository root** as context so the shared package is included:

```bash
docker build -f services/service-template/Dockerfile -t lostlink/service-template:dev .
docker run --rm -p 127.0.0.1:8000:8000 lostlink/service-template:dev
```

That minimal command exposes docs/liveness and leaves readiness at 503. For a
configured container on the Compose network, pass `--network lostlink_default`,
use `postgres` as the DB host and `http://localstack:4566` as the AWS endpoint, and
provide Cognito settings. Do not pass migration credentials to the API container.
Use the image in a separate migration job with `python -m alembic upgrade head`.

## Starting a new service

1. Copy this directory without `.venv`, `.env`, caches, or generated metadata.
2. Set `SERVICE_NAME`, a new `DATABASE_SCHEMA`, and a service-specific runtime role.
3. Replace example routes/models/migrations with domain-owned code. Keep
   `DATABASE_REVISION` synchronized with the migration head.
4. Apply `enable_tenant_rls` to every venue-scoped table and test cross-tenant access.
   Public search/read models require a separate explicit design; do not bypass RLS.
5. Add required-dependency readiness checks, domain tests, and versioned event payloads.
6. Build from the root with `--build-arg SERVICE_DIR=services/<name>`.
   Keep the migrations directory, `alembic.ini`, and both requirements files.

Transaction dependencies use `scope="function"` so commit failures occur before
the HTTP response is sent. Endpoints should flush as needed and let the shared
session context commit or roll back. Do not manually commit inside that context.
