# Delivery Service (Seed 2, Phase 2 skeleton)

Return delivery of claimed items by partner couriers (UC6). Design: [ADR-0009](../../docs/adr/0009-seed2-delivery-authorization.md).

## What exists now

- `delivery_app/domain.py`: the shipment lifecycle as a pure, immutable state machine with
  optimistic versioning, allowed actors per transition and a limit of 2 delivery attempts.
- `delivery_app/handlers.py`: reactions to `claim.handed_over` (only `mode=courier` creates a
  pickup; a courier handover without an authorized shipment raises `MissingAuthorization`) and
  `claim.cancelled` (cancels a shipment that is still awaiting pickup). Payloads are validated
  with the shared schemas in `lostlink_common.event_schemas`.
- `delivery_app/main.py`: `/health`, `/ready` and `GET /v1/shipment-lifecycle`, which publishes the
  state machine so the Owner App and partners render the same states.

```text
awaiting_pickup --(system: claim.handed_over, mode=courier)--> picked_up
awaiting_pickup --(owner | system: claim.cancelled)----------> cancelled
picked_up --(partner)--> in_transit --(partner)--> out_for_delivery
out_for_delivery --(partner)--> delivered
out_for_delivery --(partner)--> delivery_failed --(partner, attempts < 2)--> out_for_delivery
delivery_failed --(partner | venue)--> returned_to_venue
```

## Phase 2 work

| Task | Owner |
| --- | --- |
| Delivery authorization, Partner API (OAuth2 client credentials), HMAC-signed webhooks | JIANG YIBAI |
| Persistence (`delivery` schema, RLS, Alembic), outbox, `claim.*` consumer process | YANG DONGWEI |
| `delivery.status_changed` event schema in `lostlink_common.event_schemas`, regenerate `docs/api` | YANG DONGWEI |
| Owner App delivery status page | ZHANG JI |
| Helm chart, SNS subscription filter (`claim.handed_over`, `claim.cancelled`), DLQ | LU JINGXING |

Log effort per task (`Effort:` in each PR): the Seed 1 vs Seed 2 comparison in the final report uses it.

## Run and test

```bash
pip install -r services/delivery-service/requirements-dev.txt
cd services/delivery-service
pytest -q
uvicorn delivery_app.main:app --port 8005
```

Build the image from the repository root: `docker build -f services/delivery-service/Dockerfile .`
