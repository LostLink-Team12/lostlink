# Venue Service (UC1)

Authoritative venue onboarding and staff membership. Cognito owns accounts and
authentication. This service binds existing Cognito subjects; it does not store
passwords or issue tokens.

## Processes and credentials

| Process | Entry point | Database credential |
| --- | --- | --- |
| API | `uvicorn venue_app.main:app --no-access-log` | `DATABASE_URL`, `PLATFORM_DATABASE_URL` |
| Membership event publisher | `python -m venue_app.workers outbox` | `WORKER_DATABASE_URL` |
| Session revoker | `python -m venue_app.workers identity` | `WORKER_DATABASE_URL` |
| Cognito pre-token Lambda | `venue_app.token_trigger.handler` | `IDENTITY_DATABASE_URL` |
| Migration | `alembic upgrade head` | `MIGRATION_DATABASE_URL` |

Use separate login roles with no role inheritance or membership between them.
Runtime roles must not own tables, create schemas, be superusers or bypass RLS.
Only the migration identity owns DDL. The API tenant role cannot change venue
status. Workers cannot change membership data. The trigger can only read the
membership of its current subject.

## Local setup

From the repository root, start `docker compose up -d` and install
`pip install -r requirements-dev.lock -e ./libs`. Create the following roles
through a database administrator, replacing the password placeholders:

```sql
CREATE ROLE lostlink_venue_api LOGIN PASSWORD 'REPLACE_API_PASSWORD'
  NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOINHERIT;
CREATE ROLE lostlink_venue_platform LOGIN PASSWORD 'REPLACE_PLATFORM_PASSWORD'
  NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOINHERIT;
CREATE ROLE lostlink_venue_worker LOGIN PASSWORD 'REPLACE_WORKER_PASSWORD'
  NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOINHERIT;
CREATE ROLE lostlink_venue_identity LOGIN PASSWORD 'REPLACE_IDENTITY_PASSWORD'
  NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOINHERIT;
```

In `services/venue-service`, copy `.env.example` to `.env` and replace the
database URLs, pool and client IDs. Supply `MIGRATION_DATABASE_URL` only to the
migration command. Set `VENUE_API_ROLE`, `VENUE_PLATFORM_ROLE`,
`VENUE_WORKER_ROLE`, `VENUE_IDENTITY_ROLE` if using different role names.

```powershell
cd services/venue-service
python -m alembic upgrade head
python -m uvicorn venue_app.main:app --host 127.0.0.1 --port 8002 --no-access-log
```

Open `/docs` for typed OpenAPI contracts, `/health` for liveness and `/ready`
for configured identity and migrated runtime databases. Without configuration,
liveness is 200 and readiness is 503. There is no local authentication bypass.
The API process does not run migrations, publish events or call Cognito on writes.

## Authentication and API flow

- `USER_CLIENT_IDS`: staff-facing applications. Every request verifies current
  membership; a role promotion also requires a fresh token.
- `PLATFORM_CLIENT_IDS`: operations applications. Requires the trusted
  `platform_admin` Cognito group; no tenant claim is needed.
- `MEMBER_CHECK_CLIENT_IDS`: only Item/Claim machine clients, additionally
  requiring `lostlink/members.check`.
- `IDENTITY_RESOLVE_CLIENT_IDS`: optional machine adapters requiring
  `lostlink/identity.resolve`. Leave empty when using the direct-DB trigger.
- `COGNITO_CLIENT_IDS`: union of clients accepted by the JWT verifier. Human
  and machine allowlists must be disjoint.

The deployment operator creates the first `platform_admin` Cognito group and
assigns a designated operations account through trusted deployment tooling.
The Venue API cannot grant that group. Obtain its access token through an
allowed operations app, then call `POST /v1/admin/venues` with
`name`, `slug`, `initial_admin_sub`. The initial admin account must already
exist in Cognito. Onboarding returns the venue, initial member, and a pending
identity operation. Accounts that cannot be resolved remain visibly failed in
the identity job; no fake account or successful synchronization is invented.

Operations can list/get venues and suspend/reactivate them under
`/v1/admin/venues`. Staff use `/v1/venues/current`; admins list/add/patch
`/v1/venues/current/members`. A member cannot be active in two venues, and the
last active administrator cannot be demoted/suspended. Existing suspended
members are reactivated with PATCH rather than inserted again.

All writes require `Idempotency-Key` (8-128 printable ASCII characters).
PATCH also requires a quoted integer `If-Match`, for example `"1"`.
Lists include row versions; single-venue reads and writes return ETag.
Replays use the original body and If-Match. Missing prerequisites return 428,
stale versions 412, business/idempotency conflicts 409. A successful replay
requires that the caller still has current access.

## Cognito integration

Package `venue_app`, `lostlink_common` and their locked runtime dependencies for
Lambda. Use pre-token generation `V2_0` and handler
`venue_app.token_trigger.handler`. Supply only the identity database URL,
`COGNITO_USER_POOL_ID`, allowed human client IDs and `ENVIRONMENT`; the trigger
does not require API/platform database credentials or a machine token.

The trigger removes stale managed venue roles, preserves unrelated groups,
adds `owner`, and obtains the active venue role/tenant from PostgreSQL. Without
an active venue it suppresses `custom:tenant_id`. Database failures fail token
generation. The identity worker only invokes `ListUsers` by exact `sub` and
`AdminUserGlobalSignOut`; it never rewrites groups or tenant attributes.
Its IAM scope is the configured user pool. New sign-ins read current membership
directly, so job completion is not the source of role authority.

Refresh revocation is an additional control. Existing access tokens must still
be checked against `/internal/v1/members/check` on sensitive downstream writes.
Use `lostlink_common.membership.ClientCredentials` and `MembershipClient` for
those calls. They fail closed and do not cache membership decisions.

## Workers and failure recovery

Inject the worker credential only into worker processes. The outbox requires
`EVENT_TOPIC_ARN`; local development may use `AWS_ENDPOINT_URL` for LocalStack.
The identity worker requires `COGNITO_USER_POOL_ID` and workload AWS credentials.
Do not inject migration or API credentials into workers. Both entry points
select worker configuration automatically; the trigger selects trigger configuration.

Workers use short `SKIP LOCKED` claim transactions, 60-second leases and conditional
lease-token acknowledgements. External requests run outside SQL transactions.
Attempts stop at 10 and remain inspectable as `failed`; stale identity jobs are
`superseded`. Graceful termination stops new claims. `--once` processes at most
one eligible row. SNS delivery is at least once; downstream consumers deduplicate
the original event ID.

Authorized operations staff holding the worker credential can retry a failed job:

```powershell
python -m venue_app.workers outbox --retry-id UUID --operator operator-sub --reason dependency_restored
python -m venue_app.workers identity --retry-id UUID --operator operator-sub --reason manual_review
```

The retry and audit entry commit together, preserve the event/job ID, and reset
the attempt budget. Completed or leased jobs cannot be replayed by this command.
The operator argument is audit attribution, not authentication; protect the
worker credential and operational shell. API roles cannot acknowledge delivery,
edit audit records or perform this replay.

## Verification and image

```powershell
# From the repository root
$env:LOSTLINK_TEST_ADMIN_URL='postgresql+psycopg://lostlink:lostlink@localhost:5432/lostlink'
$env:LOSTLINK_TEST_AWS_ENDPOINT='http://localhost:4566'
python -m pytest -q
ruff check .
ruff format --check .
docker build -f services/venue-service/Dockerfile -t lostlink/venue-service:uc1 .
docker run --rm -p 127.0.0.1:8002:8000 --env-file .env.venue-api lostlink/venue-service:uc1
```

The integration suite creates and cleans only randomly named test databases,
four login roles, topics and queues. Real Cognito/IAM/Lambda deployment is a
separate cloud integration check; unit tests use injected identity adapters.

Business rules, handoff details and verification evidence are recorded in
[the UC1 implementation note](../../docs/uc1-implementation.md).
