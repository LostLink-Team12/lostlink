"""claim.* consumption (inbox-deduplicated) and the delivery.status_changed outbox publisher.

``consume`` keeps a projection of approved claims (who may authorize a delivery) and
applies claim.handed_over / claim.cancelled to shipments. A courier handover without an
authorized shipment raises MissingAuthorization: the message is not deleted, so after the
queue's maxReceiveCount it lands in the DLQ as an integrity alarm (ADR-0009).
"""

import argparse
import hashlib
import json
import logging
import random
import signal
import threading
from datetime import timedelta
from uuid import UUID, uuid4

from lostlink_common.db import Database
from lostlink_common.event_schemas import ClaimApproved
from lostlink_common.events import DomainEvent, SnsPublisher, aws_client, decode_message
from lostlink_common.observability import configure_logging
from sqlalchemy import and_, func, or_, select, text

from delivery_app.config import DeliverySettings
from delivery_app.handlers import MissingAuthorization, on_claim_cancelled, on_claim_handed_over
from delivery_app.models import (
    DATABASE_REVISION,
    DATABASE_SCHEMA,
    ClaimApproval,
    InboxReceipt,
    OutboxEvent,
    ShipmentRecord,
)
from delivery_app.service import apply, to_domain

logger = logging.getLogger("lostlink.delivery.worker")
WORKER = "delivery-worker"
CONSUMER = "delivery.claim_events"
ACCEPTED = {"claim.approved", "claim.handed_over", "claim.cancelled"}
MAX_ATTEMPTS = 10
LEASE_SECONDS = 60


class ProtocolError(Exception):
    """Malformed or conflicting messages must not be acknowledged."""


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class DeliveryWorker:
    def __init__(self, database: Database, *, publisher=None):
        self.database, self.publisher = database, publisher

    def session(self):
        return self.database.scoped_session(subject=WORKER)

    def consume(self, event: DomainEvent) -> str:
        if event.event_type not in ACCEPTED or event.tenant_id is None:
            raise ProtocolError("Event type is not accepted by the Delivery consumer")
        value_hash = digest(event.model_dump(mode="json"))
        data = event.data
        claim_id = UUID(str(data["claim_id"]))
        with self.session() as session:
            # One claim's events are applied one at a time, in any arrival order.
            session.execute(
                text("SELECT pg_advisory_xact_lock(:key)"),
                {"key": int.from_bytes(claim_id.bytes[:8], signed=True)},
            )
            receipt = session.get(InboxReceipt, (CONSUMER, event.event_id))
            if receipt:
                if receipt.payload_hash != value_hash:
                    raise ProtocolError("Event ID was reused with different data")
                return "duplicate"
            now = session.scalar(select(func.now()))
            approval = session.get(ClaimApproval, claim_id, with_for_update=True)
            if event.event_type == "claim.approved":
                payload = ClaimApproved.model_validate(data)
                code = "duplicate_approval"
                if approval is None:
                    session.add(
                        ClaimApproval(
                            claim_id=payload.claim_id,
                            tenant_id=event.tenant_id,
                            item_id=payload.item_id,
                            owner_sub=payload.owner_id,
                            state="approved",
                            source_event_id=event.event_id,
                        )
                    )
                    code = "approved"
            else:
                code = self.close_claim(session, event, data, approval, claim_id, now)
            session.add(
                InboxReceipt(
                    consumer_name=CONSUMER,
                    event_id=event.event_id,
                    payload_hash=value_hash,
                    result_code=code,
                )
            )
            return code

    def close_claim(self, session, event, data, approval, claim_id, now):
        state = "handed_over" if event.event_type == "claim.handed_over" else "cancelled"
        if approval is None:
            # Arrived before (or without) claim.approved: record the terminal state first.
            session.add(
                ClaimApproval(
                    claim_id=claim_id,
                    tenant_id=event.tenant_id,
                    item_id=UUID(str(data["item_id"])),
                    owner_sub=str(data["owner_id"]),
                    state=state,
                    source_event_id=event.event_id,
                )
            )
        elif approval.state == "approved":
            approval.state, approval.updated_at = state, now
        row = session.scalar(
            select(ShipmentRecord).where(ShipmentRecord.claim_id == claim_id).with_for_update()
        )
        current = to_domain(row) if row else None
        if event.event_type == "claim.handed_over":
            # Raises MissingAuthorization for a courier pickup with no authorized shipment.
            outcome = on_claim_handed_over(data, event.tenant_id, current)
            if outcome:
                code = "picked_up"
            else:
                code = "counter" if data.get("mode") == "counter" else "duplicate_pickup"
        else:
            outcome = on_claim_cancelled(data, current, now)
            code = "shipment_cancelled" if outcome else "cancelled"
        if outcome:
            apply(session, row, *outcome)
        return code

    def poll(self, client, queue_url, *, wait_seconds=10):
        messages = client.receive_message(
            QueueUrl=queue_url,
            WaitTimeSeconds=wait_seconds,
            VisibilityTimeout=60,
            MaxNumberOfMessages=1,
        ).get("Messages", [])
        for message in messages:
            try:
                self.consume(decode_message(message["Body"]))
            except MissingAuthorization:
                logger.error("delivery_courier_handover_without_authorization")
                continue
            except Exception as exc:
                # Not deleted: redelivery and the queue's DLQ policy handle it.
                logger.warning(
                    "delivery_message_rejected", extra={"error_type": type(exc).__name__}
                )
                continue
            client.delete_message(QueueUrl=queue_url, ReceiptHandle=message["ReceiptHandle"])
        return bool(messages)

    def lease(self):
        with self.session() as session:
            now = session.scalar(select(func.now()))
            row = session.scalar(
                select(OutboxEvent)
                .where(
                    or_(
                        and_(OutboxEvent.state == "pending", OutboxEvent.available_at <= now),
                        and_(OutboxEvent.state == "leased", OutboxEvent.lease_until < now),
                    )
                )
                .order_by(OutboxEvent.available_at, OutboxEvent.id)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if row is None:
                return None
            if row.attempts >= MAX_ATTEMPTS:
                row.state, row.last_error_code = "failed", row.last_error_code or "retry_exhausted"
                row.lease_token = row.lease_until = None
                logger.warning("delivery_outbox_failed")
                return None
            row.state, row.attempts, row.lease_token = "leased", row.attempts + 1, uuid4()
            row.lease_until = now + timedelta(seconds=LEASE_SECONDS)
            session.flush()
            return {"id": row.id, "lease_token": row.lease_token, "envelope": row.envelope}

    def settle(self, claimed, *, sent):
        with self.session() as session:
            row = session.scalar(
                select(OutboxEvent)
                .where(
                    OutboxEvent.id == claimed["id"],
                    OutboxEvent.state == "leased",
                    OutboxEvent.lease_token == claimed["lease_token"],
                )
                .with_for_update()
            )
            if row is None:
                return
            row.lease_token = row.lease_until = None
            if sent:
                row.state, row.last_error_code = "sent", None
                return
            now = session.scalar(select(func.now()))
            row.state = "failed" if row.attempts >= MAX_ATTEMPTS else "pending"
            row.last_error_code = "publish_failed"
            row.available_at = now + timedelta(seconds=random.uniform(1, min(300, 2**row.attempts)))

    def publish_once(self):
        claimed = self.lease()
        if claimed is None:
            return False
        try:
            self.publisher.publish(DomainEvent.model_validate(claimed["envelope"]))
        except Exception as exc:
            logger.warning("delivery_publish_failed", extra={"error_type": type(exc).__name__})
            self.settle(claimed, sent=False)
            return True
        self.settle(claimed, sent=True)
        return True


def main():
    parser = argparse.ArgumentParser(description="Run Delivery durable workers")
    parser.add_argument("mode", choices=["consume", "outbox"])
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    settings = DeliverySettings(process_mode="worker", worker_mode=args.mode)
    configure_logging(settings.service_name, settings.log_level)
    if not settings.worker_database_url:
        parser.error("WORKER_DATABASE_URL is required")
    if args.mode == "consume" and not settings.event_queue_url:
        parser.error("EVENT_QUEUE_URL is required")
    if args.mode == "outbox" and not settings.event_topic_arn:
        parser.error("EVENT_TOPIC_ARN is required")
    db = Database(settings.model_copy(update={"database_url": settings.worker_database_url}))
    stopped = threading.Event()
    for sig in [signal.SIGINT, signal.SIGTERM]:
        signal.signal(sig, lambda *_: stopped.set())
    client = None
    try:
        db.check_ready(DATABASE_SCHEMA, DATABASE_REVISION)
        if args.mode == "consume":
            client = aws_client("sqs", settings)
            worker = DeliveryWorker(db)
        else:
            client = aws_client("sns", settings)
            worker = DeliveryWorker(db, publisher=SnsPublisher(client, settings.event_topic_arn))
        while not stopped.is_set():
            worked = (
                worker.poll(client, settings.event_queue_url)
                if args.mode == "consume"
                else worker.publish_once()
            )
            if args.once:
                break
            if not worked:
                stopped.wait(1)
    finally:
        if client:
            client.close()
        db.close()


if __name__ == "__main__":
    main()
