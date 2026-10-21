# ADR-0008: Transactional outbox, inbox deduplication and durable job tables

Status: Accepted (21 Oct 2026). Records the pattern used by every service in Phase 1.

## Context
Services change their own database and publish events to SNS. Publishing directly after a commit can
lose the event if the process dies in between; publishing before the commit can announce a change
that never happened. SNS/SQS delivery is at least once, so consumers also see duplicates.

## Decision
- **Outbox:** a service writes the event into its own `outbox_events` table in the same transaction as
  the state change. A separate `outbox` worker publishes pending rows to SNS and marks them sent.
- **Inbox:** consumers record `event_id` (and a payload hash) in `inbox_receipts` in the same
  transaction as their reaction and ignore repeats; a repeated id with a different payload is a
  protocol error kept on the queue for investigation.
- **Jobs:** long or retryable work becomes a row in a service-owned job table, leased with
  `FOR UPDATE SKIP LOCKED`, retried with capped exponential backoff, and parked as `failed` after
  10 attempts for operator retry.
- SQS messages are deleted only after the database commit; poison messages go to a DLQ after 5
  receives.

## Consequences
+ No lost or phantom events, idempotent consumers, and an audit trail per event for the report.
+ Each service scales its workers on its own backlog.
- Events are eventually consistent (seconds). The UIs show "processing" states and poll operation
  resources (HTTP 202).
- Every service runs extra worker processes, which adds Deployments and monitoring.
