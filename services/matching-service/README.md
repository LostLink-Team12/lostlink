# Matching Service and Embedding Worker

UC3: owner reports, cross-venue text/photo retrieval, pgvector indexes, and durable
rematching. The API and workers are separate processes in one service-owned bounded
context. They never query another service's schema.

## Runtime setup

From the repository root:

```bash
pip install -r services/matching-service/requirements-dev.txt
pip install -r services/matching-service/requirements-ml.lock
python scripts/prepare_clip_model.py
```

The script downloads the official OpenAI ViT-B/32 artifact, validates SHA256, and
stores it under the ignored `.models/` directory. Inference does not download
models. Mount only a trusted checkpoint and configure:

```text
CLIP_CHECKPOINT=/absolute/path/to/.models/ViT-B-32.pt
CLIP_SHA256=40d365715913c9da98579312b702a82c18be219cc2a73407c4526f58eba950af
```

The official artifact uses the legacy TorchScript loader. Its full digest is
checked before loading. CLIP uses 512-dimensional unit vectors, normalized
equal-weight text/image fusion, and the OpenAI QuickGELU architecture. Text is
tokenized to the model's 77-token context. Scores are cosine similarities, not
probabilities. Missing dependencies, weights or a digest mismatch fail readiness
and encoding; there is no synthetic-vector fallback.

Copy `.env.example` to a private `.env` in this directory, replace every
placeholder, and provision three distinct non-owner database roles:

```sql
CREATE ROLE lostlink_matching_api LOGIN PASSWORD 'replace-api-password'
  NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOINHERIT;
CREATE ROLE lostlink_matching_internal LOGIN PASSWORD 'replace-internal-password'
  NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOINHERIT;
CREATE ROLE lostlink_matching_worker LOGIN PASSWORD 'replace-worker-password'
  NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOINHERIT;
```

Use a separate migration identity permitted to install the `vector` extension,
create `matching`, and grant privileges. Runtime roles must not inherit the
migration role or each other. The migration enables FORCE RLS and validates the
runtime flags; it does not create login passwords.

```bash
cd services/matching-service
alembic upgrade head
uvicorn matching_app.main:app --host 0.0.0.0 --port 8003 --no-access-log
python -m matching_app.workers consume
python -m matching_app.workers tasks
python -m matching_app.workers outbox
```

Run each command in a separate process with appropriate credentials. The API gets
API/internal database credentials; workers get only their worker credential.
Migration credentials must not be present in runtime containers. Item must
allowlist the Matching machine client for `lostlink/items.index`. Item access
and token endpoints use fixed HTTPS origins; local direct HTTP tests use injected
transports, never a deployment authentication bypass.

The consumer queue subscribes to `item.registered` and `item.catalog_changed`.
Item's existing result queue subscribes to `item.embedded` and
`item.embedding_failed`. Configure standard queues, DLQs, SNS source policies
and least-privilege IAM. The task worker also needs read-only access to the sealed
prefixes of `ITEM_PHOTO_BUCKETS`.

## API

All external routes require an allowed user client and the `owner` role.
No venue claim is required for an owner. Swagger/OpenAPI is available at `/docs`.

| Method | Path | Behavior |
| --- | --- | --- |
| POST | /v1/lost-reports | Create an owner report; Idempotency-Key required |
| GET | /v1/lost-reports | Page the caller's reports |
| GET | /v1/lost-reports/{id} | Read a report and its ETag |
| POST | /v1/lost-reports/{id}/close | Close; Idempotency-Key and If-Match required |
| GET | /v1/lost-reports/{id}/matches | Page current available matches |
| POST | /v1/search | Text, photo, or fused search across venues |
| GET | /internal/v1/matches/{id}/notification-context | Notification client + lostlink/matches.notify |

Search example:

```json
{
  "text": "red backpack",
  "category": "bags",
  "found_from": "2026-10-01T00:00:00Z",
  "found_until": "2026-10-20T00:00:00Z",
  "limit": 20,
  "min_score": 0.0
}
```

`photo_base64` is raw base64, without a data URL prefix; use it instead of or
together with `text`. Report creation uses `description` instead of `text`,
with the same optional photo and category/venue/date filters. Query photos are
at most 5 MiB, single-frame JPEG/PNG/WebP, and 16 million pixels. JSON bodies
are limited to 8 MiB, including chunked requests. Query photos are not persisted;
reports retain only the vector and `has_photo`.

The API checks the current Item context outside SQL transactions before returning
each candidate. Dependency failure returns 503. Results expose public fields and
short-lived signed photo URLs, never verification answers or S3 object keys.
Candidate retrieval is bounded to 100 by default; `potentially_truncated` flags
an exhausted candidate window. HNSW is approximate. Lists use stable ordering;
match pagination applies offsets before live-state filtering, so a page may be
shorter than its limit.

## Recovery and operations

The inbox commits jobs before SQS acknowledgment. Jobs and outbox messages use
SKIP LOCKED, 300-second leases, token-based completion, bounded backoff and ten
attempts. Each rematching page covers at most 50 report IDs and persists a cursor.
Failed non-embedding tasks or outbox messages can be replayed using the worker
credential, with an operator recorded in the audit trail:

```bash
python -m matching_app.workers tasks --retry-id UUID --operator operator-sub
python -m matching_app.workers outbox --retry-id UUID --operator operator-sub
```

After a terminal embedding failure, retry through Item's existing retry-embedding
API to get a fresh correlation ID. Replaying that failed embedding job directly
is deliberately rejected. `--once` runs one delivery or leased job for diagnostics.
Monitor failed/pending jobs, outbox age, DLQs and worker liveness. HTTP probes
and metrics are `/health`, `/ready`, and `/metrics`; readiness checks model and
database migrations but does not prove end-to-end cloud connectivity.

Keep at least one task worker running or scale it from the PostgreSQL pending-job
count: the SQS consumer acknowledges after durable enqueue, before CPU inference,
so SQS depth alone cannot scale the task process to zero safely. Use the same
checkpoint digest in every API and task replica. A model change needs a controlled
reindex; vectors from different digests never mix in a search.

## Tests and container

Root pytest includes this service. Base tests need no ML packages. Integration
tests create and remove random databases and six restricted roles. Optional
model tests use the actual pinned checkpoint:

```bash
export LOSTLINK_TEST_ADMIN_URL=postgresql+psycopg://lostlink:lostlink@localhost:5432/lostlink
export LOSTLINK_TEST_AWS_ENDPOINT=http://localhost:4566
export CLIP_CHECKPOINT=/absolute/path/to/.models/ViT-B-32.pt
export CLIP_SHA256=40d365715913c9da98579312b702a82c18be219cc2a73407c4526f58eba950af
python -m pytest -q services/matching-service/tests
docker build --build-arg INSTALL_ML=true -f services/matching-service/Dockerfile -t lostlink/matching-service:uc3-ml .
```

PowerShell uses `$env:NAME='value'` for environment variables. Build from the
repository root. The default lightweight Docker build supports queue/outbox
processes and health checks. Build with `INSTALL_ML=true` and mount the trusted
checkpoint read-only for an API/task deployment that performs inference.
The universal ML lock includes Linux CUDA dependencies; choose an explicitly
tested CPU wheel build when optimizing a CPU-only deployment.

Notification email delivery, the Venue public-name directory, production
Cognito/IAM/SES, multilingual quality calibration and throughput benchmarks
remain separate integration work. The notification context currently returns
`venue_name: null`; consumers must use a generic venue label until the directory
contract is implemented. See the Chinese UC3 implementation and handoff documents.
