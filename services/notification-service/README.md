# Notification Service (UC4)

Consumes Matching's match.found events and emails the owner through SES. Matching owns
the match decision; this service only decides whether, when and how to notify.
Partner webhooks (T5) will be a separate module in this service.

## Setup

Start the root Compose dependencies and install the root requirements-dev.lock and
editable libs package. Through a local database administrator, create three login
roles with NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOINHERIT:
lostlink_notification_api, lostlink_notification_admin and
lostlink_notification_worker. Copy .env.example to .env and replace the values.

    cd services/notification-service
    python -m alembic upgrade head
    python -m uvicorn notification_app.main:app --host 127.0.0.1 --port 8006 --no-access-log

/health is liveness; /ready checks the token verifier, owner clients and the migrated
databases (the admin database only when configured). /docs has the typed OpenAPI.

## Processes

| Command | Responsibility | Configuration |
| --- | --- | --- |
| python -m notification_app.workers consume | match.found inbox, notification and job creation, SQS acknowledgement after commit | WORKER_DATABASE_URL, EVENT_QUEUE_URL |
| python -m notification_app.workers tasks | Revalidate with Matching, resolve the verified email, render and send | WORKER_DATABASE_URL, MATCHING_BASE_URL, machine client, COGNITO_USER_POOL_ID, OWNER_APP_BASE_URL, SES settings |

Subscribe the consumer queue to match.found only and configure a DLQ with
maxReceiveCount=5. Invalid or conflicting messages are never deleted. The consumer
records (consumer, event_id) in its inbox; a repeated event ID is acknowledged, and a
different event ID for the same match finds the existing notification through the
unique (match_id, owner, channel) key.

The tasks process, for each leased job (60 seconds, at most five attempts, backoff
from 30 seconds to 30 minutes):

1. Calls GET /internal/v1/matches/{id}/notification-context with the
   lostlink/matches.notify machine scope. Unknown matches, closed reports, unavailable
   items and owner/report/item/tenant mismatches with the event are suppressed.
2. Resolves the owner's verified email by exact subject in the Cognito user pool
   (IAM: cognito-idp:ListUsers). Missing or unverified accounts fail with that reason.
   Event or request addresses are never used.
3. Renders match-found-v1: fixed English subject, escaped HTML and plain text, and a
   link built from OWNER_APP_BASE_URL plus the match UUID. No hidden answers, phone
   numbers or images. Matching currently returns no venue name; a generic label is used.
4. Commits a started attempt, then calls SES with SDK retries disabled.
   - A MessageId is recorded as sent.
   - Rejections, throttling and connection failures before the request count as
     not sent; only throttling and connection failures retry.
   - Read timeouts, provider 5xx, missing IDs, and a crash after the attempt started
     become delivery_unknown and are never resent automatically.

Notification states: pending, sending, sent, retry_wait, failed, delivery_unknown,
suppressed. "sent" means SES accepted the message, not that the owner read it.

## APIs

| Method and path | Caller | Behaviour |
| --- | --- | --- |
| GET /v1/notifications | owner | Own notifications: IDs, state, reason code, times |
| GET /v1/notifications/{id} | owner | Own notification; others get 404 |
| GET /v1/admin/notifications?state= | platform_admin via an operator client | Metadata and job attempts; no addresses or content |
| GET /v1/admin/notification-stats | operator | Counts by state and age of the oldest pending notification |
| POST /v1/admin/notifications/{id}/retry | operator | Idempotency-Key; reason_code; failed or delivery_unknown only |

Retrying delivery_unknown requires acknowledge_duplicate_risk=true because the owner
may already have the email. Retries reuse the notification, add attempts and write an
audit entry. There is no API to send to an arbitrary recipient.

## Data protection

The api role reads only the caller's notifications. The admin role reads metadata and
can only reset state and job fields. The worker role cannot delete, and no runtime
role can change owner, match or tenant columns. Addresses and message bodies are not
stored; logs and errors carry codes only.

## Verification

    $env:LOSTLINK_TEST_ADMIN_URL='postgresql+psycopg://lostlink:lostlink@localhost:5432/lostlink'
    $env:LOSTLINK_TEST_AWS_ENDPOINT='http://localhost:4566'
    python -m pytest -q services/notification-service/tests
    docker build -f services/notification-service/Dockerfile -t lostlink/notification-service:uc4 .

Integration tests run the real Item and Matching applications with a deterministic
encoder so match.found comes from Matching's own outbox. A capture sender replaces
SES (EMAIL_SENDER=capture is refused in dev/prod). Real SES identity verification and
test recipients remain deployment work. See
[the UC4 implementation record](../../docs/uc4-implementation.md).
