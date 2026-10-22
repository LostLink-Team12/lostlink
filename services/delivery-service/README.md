# Delivery Service (Seed 2)

Return delivery of claimed items by partner couriers (UC6). Design: [ADR-0009](../../docs/adr/0009-seed2-delivery-authorization.md).

```text
owner authorizes partner ----------------------------------> awaiting_pickup
awaiting_pickup --(system: claim.handed_over, mode=courier)--> picked_up
awaiting_pickup --(owner | system: claim.cancelled)----------> cancelled
picked_up --(partner)--> in_transit --(partner)--> out_for_delivery
out_for_delivery --(partner)--> delivered
out_for_delivery --(partner)--> delivery_failed --(partner, attempts < 2)--> out_for_delivery
delivery_failed --(partner | venue)--> returned_to_venue
```

## Components

- `delivery_app/domain.py`: the lifecycle as a pure, immutable state machine (allowed actors per
  transition, optimistic versions, at most 2 delivery attempts).
- `delivery_app/handlers.py`: reactions to `claim.handed_over` and `claim.cancelled`.
- `delivery_app/routes.py`: owner and partner APIs (below).
- `delivery_app/workers.py`: `consume` (claim events from SQS, inbox-deduplicated) and `outbox`
  (publishes `delivery.status_changed` to SNS, ADR-0008).
- `migrations/`: schema `delivery` with forced RLS and two non-owner roles. Owners see their own
  rows; partners see only shipments assigned to them; ownership, partner, claim and address
  columns cannot be updated at runtime.

Every state change writes the shipment row, one history row and one `delivery.status_changed`
outbox event in one transaction. The delivery address is stored only encrypted (AES-GCM, bound to
the shipment ID), is never in events or logs, and is released to the partner only while the
shipment is active.

## API

All writes need `Idempotency-Key`; state changes also need `If-Match` with the shipment `ETag`.

| Caller | Route | Purpose |
| --- | --- | --- |
| Owner (Owner App client, `owner` role) | `POST /v1/delivery-authorizations` | Name one partner and an address for an **approved** claim of yours; creates the shipment in `awaiting_pickup` |
| Owner | `GET /v1/shipments`, `GET /v1/shipments/{id}` | Status, history and address |
| Owner | `POST /v1/shipments/{id}/cancel` | Cancel before pickup |
| Partner (machine client, scope `lostlink/deliveries.update`) | `GET /v1/partner/shipments`, `GET /v1/partner/shipments/{id}` | Assigned shipments; address while active |
| Partner | `POST /v1/partner/shipments/{id}/status` | `in_transit`, `out_for_delivery`, `delivered`, `delivery_failed`, `returned_to_venue` |
| Anyone | `GET /v1/shipment-lifecycle` | The state machine, for UIs |

A partner can never report pickup. The courier shows the owner's QR code at the counter and the
venue scans it with `POST /v1/venue/claims/{id}/handover` and `{"token": ..., "mode": "courier"}`
(Claim). Claim publishes `claim.handed_over` with `mode=courier`, and the consumer moves the
shipment to `picked_up`. A courier handover without an authorized shipment raises
`MissingAuthorization` and is never acknowledged, so it reaches the DLQ as an integrity alarm.

The consumer also keeps a projection of `claim.approved` (who may authorize which claim), so
the Delivery API never calls Claim synchronously.

## Configuration

See `.env.example`. API: `DATABASE_URL`, Cognito issuer, `USER_CLIENT_IDS` (Owner App),
`PARTNER_CLIENT_IDS` (courier machine clients; the partner ID is the client ID),
`FIELD_ENCRYPTION_KEYS` and `FIELD_ENCRYPTION_ACTIVE_KEY_ID`. Workers:
`WORKER_DATABASE_URL`, plus `EVENT_QUEUE_URL` (consume) or `EVENT_TOPIC_ARN` (outbox).

## Deployment inputs for C1/C3

| Item | Value |
| --- | --- |
| Database roles and secrets | `delivery/api`, `delivery/worker`, `delivery/migration` (same JSON shape as the others), `delivery/crypto` with `FIELD_ENCRYPTION_KEYS` and `FIELD_ENCRYPTION_ACTIVE_KEY_ID` |
| Queue | `lostlink-dev-delivery-events` + DLQ, subscribed to the domain topic with `event_type` in `claim.approved`, `claim.handed_over`, `claim.cancelled` |
| Cognito | resource-server scope `lostlink/deliveries.update`; one confidential client per demo courier (client credentials) |
| IRSA processes | `delivery-api` (database/delivery/api, delivery/crypto), `delivery-consume` (worker secret, queue), `delivery-outbox` (worker secret, `sns:Publish` on the topic) |
| Helm | `services.delivery.enabled: true` with `consume` and `outbox` workers |

## Run and test

```bash
pip install -r services/delivery-service/requirements-dev.txt
cd services/delivery-service
pytest -q -m "not integration"
LOSTLINK_TEST_ADMIN_URL=postgresql+psycopg://lostlink:lostlink@localhost:5432/lostlink pytest -q -m integration
MIGRATION_DATABASE_URL=... alembic upgrade head
uvicorn delivery_app.main:app --port 8005
python -m delivery_app.workers consume   # or: outbox
```

Build the image from the repository root: `docker build -f services/delivery-service/Dockerfile .`

Effort for the Seed 1 / Seed 2 comparison is logged in [docs/phase2-plan.md](../../docs/phase2-plan.md).
