# Item Service (UC2)

Owns item content/state, private verification questions, immutable photos,
bulk imports and Claim reservations. Embedding and Matching remain separate services.

## Setup

Start the root Compose dependencies and install the root requirements-dev.lock
and editable libs package. Through a local database administrator, create:

    CREATE ROLE lostlink_item_api LOGIN PASSWORD 'REPLACE_API_PASSWORD'
      NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOINHERIT;
    CREATE ROLE lostlink_item_internal LOGIN PASSWORD 'REPLACE_INTERNAL_PASSWORD'
      NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOINHERIT;
    CREATE ROLE lostlink_item_worker LOGIN PASSWORD 'REPLACE_WORKER_PASSWORD'
      NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOINHERIT;

Copy .env.example to .env in this service directory and replace credentials,
bucket, Cognito/client IDs, and Venue service settings. Configure the Item machine
client in Venue with lostlink/members.check. The shared membership client requires
HTTPS; use TLS termination for local authenticated service integration. Tests inject
a transport that calls the actual Venue test application with its own credentials.

Supply MIGRATION_DATABASE_URL only to migration tooling. If role names differ, set
ITEM_API_ROLE, ITEM_INTERNAL_ROLE and ITEM_WORKER_ROLE for Alembic.

    cd services/item-service
    python -m alembic upgrade head
    python -m uvicorn item_app.main:app --host 127.0.0.1 --port 8003 --no-access-log

The item schema is independent of Venue. API, internal and worker roles are distinct,
cannot bypass RLS, and have no privileges in other service schemas. Migration
credentials never belong in an API/worker container. /health is liveness; /ready
checks migrated databases, identity/membership configuration and the photo bucket.
Without configuration, /health is 200 and /ready is 503. /docs contains typed OpenAPI.

## Employee flow

Every employee request checks the current membership through Venue, with no positive
cache. A missing dependency returns 503. Tenant IDs come from verified access tokens.

1. POST /v1/items with title, description, category, found_at and 1-3
   verification_questions containing prompt/reference_answer. The item starts
   awaiting_upload; no embedding job is published.
2. POST /v1/items/{id}/uploads with content_type/size and If-Match. The service
   generates an upload ID and exact-key, size/type-bound presigned S3 POST.
3. Submit the returned form fields and a file directly to S3, without a Cognito
   Authorization header. JPEG/PNG/WebP, maximum 10 MiB, ten-minute upload window.
4. POST /v1/items/{id}/uploads/{upload_id}/confirm. A 202 response contains
   operation_id. Poll GET /v1/item-operations/{operation_id}; 202 alone does not mean
   the photo was validated or the item is searchable.
5. The tasks worker checks and seals the object, then atomically changes the item
   to processing and records item.registered plus item.catalog_changed in the outbox.
6. The consumer accepts only the current content version and processing correlation ID.
   Successful embedding makes the item available; terminal failure makes it
   processing_failed. Retry uses POST /v1/items/{id}/retry-embedding and a new
   processing correlation ID. Real image decoding and CLIP execution belong to Embedding.

Lists support limit/offset and state/category filters, and exclude reference answers.
Employee details include the private questions. Public/internal DTOs and events omit
answers. PATCH and withdraw are version-controlled. Reserved, handed_over and withdrawn
items cannot be edited. Replacing a photo invalidates the old processing round.

All writes require Idempotency-Key (8-128 printable ASCII). Existing-resource employee
mutations require quoted integer If-Match, except confirm which binds a specific upload.
Same request/key replays the logical result after authorization. Presigned credentials
are regenerated only inside the original upload expiry and are not stored in replay
snapshots. Missing prerequisites return 428, stale versions 412, business conflicts 409.

## Independent workers

Inject only WORKER_DATABASE_URL and each worker's required AWS settings:

| Command | Responsibility | Configuration |
| --- | --- | --- |
| python -m item_app.workers tasks | Photo sealing, per-row imports, reservation recovery alerts | PHOTO_BUCKET |
| python -m item_app.workers outbox | At-least-once SNS publication | EVENT_TOPIC_ARN |
| python -m item_app.workers consume | Transactional embedding results, inbox deduplication, SQS acknowledgement | EVENT_QUEUE_URL |
| python -m item_app.workers cleanup | Paginated orphan/staging collection after at least 24 hours | PHOTO_BUCKET |

Tasks/outbox use 60-second leases, conditional lease-token acknowledgement, and ten
active attempts. Scheduling uses the database clock. Network work runs outside SQL
transactions. Source reads use the observed object ETag/version, while final writes
use If-None-Match: *. A crashed operation reuses its first sealed snapshot.

The consumer handles only item.embedded and item.embedding_failed. Set its queue
subscription filter accordingly and configure a DLQ with maxReceiveCount=5. Conflicting
same-ID payloads and invalid messages are not deleted. Receipt, domain state and new
events commit together before SQS deletion. Allowlisted sources and tenant/locator
checks complement IAM queue/topic policy.

Cleanup retains confirmed sealed objects, including historical versions. It paginates
the full prefix, checks ownership/reference state and protects pending work. It marks
unconfirmed records purging before deletion so a concurrent manual retry cannot
publish a deleted object. Run it as a separate scheduled operation.

Protected operations can replay failed outbox/dependency tasks with an audit entry:

    python -m item_app.workers outbox --retry-id UUID --operator operator-sub --reason dependency_restored
    python -m item_app.workers tasks --retry-id UUID --operator operator-sub --reason manual_review

The worker credential authorizes this command; operator is attribution. Invalid or
expired photos require a fresh upload. Failed import rows use the repair API below.
Reservation recovery alerts never automatically release a Claim reservation.

## Bulk import and local photos

POST /v1/item-imports takes a stable batch_reference and at most 100 entries.
Each entry has client_reference plus normal item fields. Batch identity remains unique
after request-replay records expire. Invalid row bodies fail independently; duplicate
references or oversized envelopes fail before submission. Remote photo URLs are not fetched.

GET /v1/item-imports/{id} returns job totals and paginated row outcomes. To repair only
failed rows, POST /v1/item-imports/{id}/retry with:

    {"entries":[{"entry_id":"UUID","item":{"title":"...","description":"...",
      "category":"...","found_at":"2026-10-20T08:00:00Z",
      "verification_questions":[{"prompt":"...","reference_answer":"..."}]}}]}

The root scripts/import_items.py handles batches, local multipart uploads and confirmation.
Its manifest is an array of normal entries with an additional local photo_path:

    [{
      "client_reference": "umbrella-001",
      "photo_path": "photos/umbrella.png",
      "title": "Blue umbrella",
      "description": "A blue umbrella found near the entrance",
      "category": "umbrella",
      "found_at": "2026-10-20T08:00:00Z",
      "verification_questions": [
        {"prompt": "What initials are inside?", "reference_answer": "XY"}
      ]
    }]

Photo files must remain inside the manifest directory. The script sends no local paths
to the API, emits only progress counts, and never forwards a Cognito token to S3.
From the repository root:

    $env:LOSTLINK_ACCESS_TOKEN = "YOUR_STAFF_ACCESS_TOKEN"
    python scripts/import_items.py path/to/manifest.json --api-url http://127.0.0.1:8003

Start the tasks worker first. The script resumes the same durable batches and skips
already confirmed items; interrupted pending uploads use a fresh slot. It waits for
photo confirmation, not CLIP completion. Renew an expired token before resuming.

## Internal Claim/Matching contracts

Claim clients require lostlink/items.context or lostlink/items.reserve. Embedding/Matching
clients require lostlink/items.index. Token client allowlists are additional to scopes.
Resource lookup is constrained to one item ID, followed by a tenant transaction.

- GET /internal/v1/items/{id}/claim-context: question prompts/versions without answers.
- GET /internal/v1/items/{id}/index-context: current catalogue, processing correlation,
  sealed reference; available photos have a 120-second signed read URL.
- POST /internal/v1/items/{id}/reservations: claim_id and expected item_version.
- GET /internal/v1/items/{id}/reservations/{claim_id}: held/released/committed.
- POST the reservation's /release: creates a released tombstone even before reserve arrives.
- POST the reservation's /commit-handover: fixed handover_id and counter/courier mode.

Concurrent claims serialize on the item. A released claim cannot reacquire it.
An identical committed handover can replay; another ID or releasing it conflicts.
Pre-reservation versions must match. Held reservations older than five minutes produce
a durable needs_attention record for Claim recovery; they never expire into availability.

## Verification and deployment

    $env:LOSTLINK_TEST_ADMIN_URL='postgresql+psycopg://lostlink:lostlink@localhost:5432/lostlink'
    $env:LOSTLINK_TEST_AWS_ENDPOINT='http://localhost:4566'
    $env:LOSTLINK_TEST_SCALE='1'
    python -m pytest -q
    ruff check .
    ruff format --check .
    docker build -f services/item-service/Dockerfile -t lostlink/item-service:uc2 .

Default tests skip the optional 5,000-record exercise unless LOSTLINK_TEST_SCALE=1.
Tests create and clean only random local resources. No new runtime dependencies or
lockfile versions are introduced. Existing CI discovers this service and enables S3.

For containers, supply the API's two runtime URLs through an ignored .env.item-api
file and use the same image with separate commands/env files for workers. Ensure
LocalStack URLs used for presigning are reachable by both the API and uploading client.
Production uses the normal AWS endpoint, workload IAM, a private bucket and CORS
limited to the Venue Portal. Only the sealing workload may write sealed/; enforce
conditional creation there, and restrict embedding workers to sealed-object reads.

Cloud Cognito/IAM/S3 policy and actual CLIP/Matching integrations remain team deployment
work. Decisions, test evidence and handoff boundaries are in
[the UC2 implementation record](../../docs/uc2-implementation.md).
