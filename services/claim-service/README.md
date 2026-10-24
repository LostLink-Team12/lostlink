# Claim Service (UC5)

Owns claims, verification answers, encrypted contact snapshots, approval/cancel/
handover coordination and QR handover codes. Item remains the reservation authority;
Claim never writes Item tables and holds no SQL lock across an Item call.

## Setup

Start the root Compose dependencies and install the root requirements-dev.lock and
editable libs package. Through a local database administrator, create:

    CREATE ROLE lostlink_claim_api LOGIN PASSWORD 'REPLACE_API_PASSWORD'
      NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOINHERIT;
    CREATE ROLE lostlink_claim_worker LOGIN PASSWORD 'REPLACE_WORKER_PASSWORD'
      NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOINHERIT;

Copy .env.example to .env and replace credentials, Cognito/client IDs, the user pool,
Venue/Item URLs, the machine client, the field-encryption keyring and the handover
key. The Claim machine client needs lostlink/members.check (Venue) and
lostlink/items.context plus lostlink/items.reserve (Item); list it in Venue's
MEMBER_CHECK_CLIENT_IDS and Item's CLAIM_CLIENT_IDS. Internal clients require HTTPS.

    cd services/claim-service
    python -m alembic upgrade head
    python -m uvicorn claim_app.main:app --host 127.0.0.1 --port 8005 --no-access-log

/health is liveness. /ready returns 503 until the migrated database, token verifier,
user clients, Venue membership, Item client, identity directory, encryption keyring
and handover key are configured. /docs contains the typed OpenAPI.

## Owner flow

1. GET /v1/claim-questions?item_id=... returns prompts and versions, never answers.
2. POST /v1/claims with item_id, one answer per question (question_id, version,
   answer) and an optional contact {name, phone}. The verified email comes from
   Cognito by exact subject; unverified accounts get 409 email_not_verified.
   A replay of the same Idempotency-Key returns the stored response before any
   Item check, so a later reservation cannot change a committed answer.
3. GET /v1/claims, GET /v1/claims/{id} and GET /v1/claims/{id}/contact show only the
   caller's own claims.
4. POST /v1/claims/{id}/cancel with reason_code (found_elsewhere, no_longer_needed,
   submitted_in_error, other) and If-Match. A submitted claim cancels at once (200);
   an approval in flight or an approved claim moves to cancellation_pending (202)
   and becomes cancelled only after Item confirms the release.
5. After approval, POST /v1/claims/{id}/handover-token returns qr_payload. Issuing
   a new code revokes the previous one. The same Idempotency-Key replays the same
   code for ten minutes at most; afterwards issue a new code.

## Venue flow

Every staff request checks current Venue membership with no positive cache; missing
or failing membership returns 503. Lists and details are pinned to the token's venue.

- GET /v1/venue/claims?state=... and GET /v1/venue/claims/{id}: answers and prompt
  snapshots. Reference answers stay in Item's staff detail.
- POST .../approve (If-Match): 202 with operation_id. Another claim already holding
  or approving the same item returns 409 item_claim_in_progress, and the claim stays
  submitted. Staff cannot approve or hand over their own claim.
- POST .../reject with reason_code (answers_incorrect, insufficient_detail,
  duplicate, other) and If-Match: 200, only for submitted claims.
- GET .../contact: name, phone and verified email during review and pending handover;
  contact access is tenant scoped. Every disclosure writes an audit entry.
- POST .../handover with {"token": qr_payload} and If-Match: 202 with operation_id.
  Hand the item over only when the operation completes and the claim is handed_over.

Poll GET /v1/claim-operations/{operation_id} (owner or staff of the venue) for
state, claim_state and result_code. A 202 response is never a completed handover.

## Coordinator

| Command | Responsibility | Configuration |
| --- | --- | --- |
| python -m claim_app.workers tasks | Reserve, release and commit-handover against Item | WORKER_DATABASE_URL, ITEM_BASE_URL, machine client |
| python -m claim_app.workers outbox | At-least-once SNS publication of claim events | WORKER_DATABASE_URL, EVENT_TOPIC_ARN |

Each claim has at most one active operation. The coordinator reads the claim state:
approval_pending reserves, cancellation_pending releases, handover_pending commits
the fixed handover ID. A cancellation during approval reuses the same operation and
releases even if the reserve already succeeded. Item calls use stable keys
(claim:{id}:reserve, :release, :handover:{handover_id}); a lost response is resolved
by reading the reservation, never by assuming the request failed.

Leases last 60 seconds; a crashed worker's operation is re-leased and replays the
same Item call. Retries back off up to five minutes. After ten attempts, or once a
started operation passes its 15-minute deadline, it becomes needs_attention and
the claim keeps its pending state. Nothing is approved, released or cancelled by
guesswork. After fixing the cause, replay with an audit entry:

    python -m claim_app.workers tasks --retry-id OPERATION_ID --operator operator-sub --reason dependency_restored
    python -m claim_app.workers outbox --retry-id EVENT_ID --operator operator-sub --reason manual_review

Events: claim.approved after the local approval commit, claim.cancelled after
cancellation (including a direct submitted cancel), claim.handed_over with
mode=counter after Item commits the handover. Event payloads contain no contact data.

## Data protection

- RLS per role: the API role sees rows it owns or rows of the venue in its context;
  contacts are visible to staff only for approved or handover_pending claims. The
  worker role has no access to contacts.
- Column grants stop runtime roles from changing tenant_id, owner_sub or item binding.
- Contacts and QR replay values are AES-GCM encrypted, bound to service, table,
  column and row. QR codes are stored as an HMAC with HANDOVER_TOKEN_KEY.

## Verification

    $env:LOSTLINK_TEST_ADMIN_URL='postgresql+psycopg://lostlink:lostlink@localhost:5432/lostlink'
    python -m pytest -q services/claim-service/tests
    ruff check .
    ruff format --check .
    docker build -f services/claim-service/Dockerfile -t lostlink/claim-service:uc5 .

Integration tests run the real Venue and Item applications in-process against one
disposable database with nine restricted roles. Cognito identity lookups use the
static double; real Cognito, SES and cloud IAM remain deployment work. Deployment
requirements are in [the backend integration guide](../../docs/team-alignment.md).


Venue approval accepts an optional `contact: {name, phone}` body. The staff email
is resolved and verified through Cognito, then stored with that contact inside the
existing encrypted claim contact record. Owners read it with
`GET /v1/claims/{id}/venue-contact` after approval (including completed handovers).
Staff can backfill or update it with `PUT /v1/venue/claims/{id}/reply-contact`
while the handover is pending, using If-Match and Idempotency-Key. Contact reads
are audited and return Cache-Control: no-store. Legacy empty-body approval stays
compatible, and returns contact_not_available until staff shares a contact.

Run `alembic upgrade head` for Claim before deploying: migrations/env.py refreshes
contact RLS for submitted/approval_pending states and grants UPDATE only on
key_id/encrypted_contact. No new columns or revision are required. The CD migration
hash guard still requires the normal migration baseline approval.
