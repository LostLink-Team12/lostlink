# ADR-0009: Seed 2 return delivery is driven by owner authorization and the venue's handover scan

Status: Proposed (21 Oct 2026) for Phase 2. Owner: YANG DONGWEI (delivery lifecycle),
JIANG YIBAI (delivery authorization, Partner API and webhooks).

## Context
Seed 2 lets a partner courier return a claimed item to its owner. It must reuse the platform rather
than add a parallel flow, and it must not leak the owner's contact details: `claim.approved` alone
does not prove the owner wants a courier, nor which courier.

## Decision
1. **Authorization first.** After approval the owner creates a delivery authorization naming one
   partner (Partner API, Phase 2). Only then does Delivery create a shipment in `awaiting_pickup`
   and release the delivery address to that partner.
2. **Pickup only through the venue scan.** The courier presents the owner's QR code at the counter;
   Claim records the handover and publishes `claim.handed_over` with `mode=courier`. Delivery's
   consumer moves the shipment to `picked_up`. A partner can never set `picked_up` itself.
3. **Delivery owns the transport state** after pickup: `picked_up -> in_transit ->
   out_for_delivery -> delivered`, with at most 2 failed attempts before `returned_to_venue`.
   An owner can cancel only before pickup; `claim.cancelled` cancels a pending shipment.
   The rules live in `services/delivery-service/delivery_app/domain.py` and are published at
   `GET /v1/shipment-lifecycle`.
4. **Events.** Each change is published as `delivery.status_changed` (to be added to
   `lostlink_common.event_schemas` with the persistence work) through the outbox (ADR-0008);
   notification-service and partner webhooks subscribe to it.
5. **Proving platform reuse.** Seed 2 reuses Cognito identities, tenancy, the event bus, outbox/inbox,
   Notification and the CI/CD template. We log its effort per task and compare it with Seed 1 in the
   final report.

## Consequences
+ The courier gets the address only for an item the owner explicitly assigned to them.
+ The physical handover is proven by the same single-use QR scan as counter pickups.
- A courier pickup with no authorization is an integrity alarm: the consumer leaves the message for the
  DLQ instead of creating a shipment.
- Phase 2 must add persistence, the consumer process and partner routes before the demo of UC6.
