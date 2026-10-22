"""Durable claim coordination: Item calls outside SQL transactions, lease-token commits."""

import argparse
import logging
import random
import signal
import threading
from datetime import timedelta
from uuid import UUID, uuid4

from lostlink_common.db import Database
from lostlink_common.events import DomainEvent, SnsPublisher, aws_client
from lostlink_common.observability import configure_logging
from lostlink_common.worker_health import heartbeat
from sqlalchemy import and_, func, or_, select

from claim_app.clients import ItemRejected, ItemUnavailable, make_item_client, make_token_provider
from claim_app.config import ClaimSettings
from claim_app.models import (
    DATABASE_REVISION,
    DATABASE_SCHEMA,
    AuditEntry,
    Claim,
    ClaimOperation,
    HandoverRecord,
    HandoverToken,
    OutboxEvent,
)
from claim_app.service import ClaimService

logger = logging.getLogger("lostlink.claim.worker")
MAX_ATTEMPTS = 10
WORKER = "claim-worker"
ACTIONS = {
    "approval_pending": "reserve",
    "cancellation_pending": "release",
    "handover_pending": "commit",
}


class Retry(Exception):
    """The Item outcome is unknown or transient; keep the claim state and retry later."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


class ClaimWorker:
    def __init__(self, database: Database, *, item_client=None, publisher=None):
        self.database, self.item_client, self.publisher = database, item_client, publisher

    def claim(self, model):
        with self.database.scoped_session(subject=WORKER) as session:
            now = session.scalar(select(func.now()))
            row = session.scalar(
                select(model)
                .where(
                    or_(
                        and_(model.state == "pending", model.available_at <= now),
                        and_(model.state == "leased", model.lease_until < now),
                    )
                )
                .order_by(model.available_at, model.id)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if row is None:
                return None
            exhausted = row.attempts >= MAX_ATTEMPTS
            if isinstance(row, ClaimOperation):
                # The deadline bounds active retries; a worker outage alone does not exhaust it.
                exhausted = exhausted or (row.attempts > 0 and row.deadline_at <= now)
            if exhausted:
                # Business state stays pending: nothing is released or approved by guesswork.
                row.state = "needs_attention" if isinstance(row, ClaimOperation) else "failed"
                row.last_error_code = row.last_error_code or "retry_exhausted"
                row.lease_token = row.lease_until = None
                logger.warning("claim_operation_needs_attention")
                return None
            row.state, row.attempts, row.lease_token = "leased", row.attempts + 1, uuid4()
            row.lease_until = now + timedelta(seconds=60)
            session.flush()
            return row

    def current(self, session, model, claimed):
        return session.scalar(
            select(model)
            .where(
                model.id == claimed.id,
                model.state == "leased",
                model.lease_token == claimed.lease_token,
            )
            .with_for_update()
        )

    def tenant_session(self, claimed):
        return self.database.scoped_session(subject=WORKER, tenant_id=claimed.tenant_id)

    @staticmethod
    def finish(row, state, *, code=None, error=None, now=None):
        row.state, row.last_error_code = state, error
        row.lease_token = row.lease_until = None
        if isinstance(row, ClaimOperation):
            row.result_code = code
            if state in {"completed", "needs_attention"}:
                row.completed_at = now

    def retry(self, claimed, code):
        with self.database.scoped_session(subject=WORKER) as session:
            row = self.current(session, ClaimOperation, claimed)
            if row is None:
                return
            now = session.scalar(select(func.now()))
            self.finish(row, "pending", error=code)
            row.available_at = now + timedelta(seconds=random.uniform(1, min(300, 2**row.attempts)))

    def snapshot(self, claimed):
        with self.tenant_session(claimed) as session:
            if self.current(session, ClaimOperation, claimed) is None:
                return None
            claim = session.get(Claim, claimed.claim_id)
            record = session.scalar(
                select(HandoverRecord).where(HandoverRecord.claim_id == claim.id)
            )
            return (
                ACTIONS.get(claim.state),
                claim.item_id,
                claim.item_version,
                record.handover_id if record else None,
            )

    def call_item(self, action, claim_id, item_id, item_version, handover_id):
        client = self.item_client
        try:
            if action == "reserve":
                try:
                    reservation = client.reserve(item_id, claim_id, item_version)
                except ItemUnavailable:
                    # The reserve may have committed; the reservation record is authoritative.
                    reservation = client.reservation(item_id, claim_id)
                if reservation.state == "held" and reservation.item_version == item_version:
                    return "held"
                return "refused"
            if action == "release":
                reservation = client.release(item_id, claim_id, "claim_cancelled")
                return "released" if reservation.state == "released" else "conflict"
            try:
                reservation = client.commit_handover(item_id, claim_id, handover_id, "counter")
            except ItemRejected as exc:
                if exc.code != "reservation_not_held":
                    raise
                reservation = client.reservation(item_id, claim_id)
            if reservation.state == "committed" and reservation.handover_id == handover_id:
                return "committed"
            return "conflict"
        except ItemRejected as exc:
            if action == "reserve" and exc.code in {
                "item_not_available",
                "reservation_closed",
                "item_not_found",
            }:
                return "refused"
            if action == "reserve" and exc.code == "reservation_not_found":
                raise Retry("reservation_unknown") from exc
            return "conflict"
        except ItemUnavailable as exc:
            raise Retry("item_unavailable") from exc

    def apply(self, claimed, action, outcome):
        """Return True when the operation reached a final state."""
        with self.tenant_session(claimed) as session:
            operation = self.current(session, ClaimOperation, claimed)
            if operation is None:
                return True
            now = session.scalar(select(func.now()))
            claim = session.scalar(
                select(Claim).where(Claim.id == operation.claim_id).with_for_update()
            )
            service = ClaimService(session, WORKER)
            if ACTIONS.get(claim.state) != action:
                # An owner cancelled during approval: release is now required.
                if claim.state == "cancellation_pending":
                    return False
                self.finish(operation, "completed", code="superseded", now=now)
                return True
            if outcome == "conflict":
                if action == "commit":
                    record = session.scalar(
                        select(HandoverRecord).where(HandoverRecord.claim_id == claim.id)
                    )
                    record.state = "needs_attention"
                self.finish(
                    operation,
                    "needs_attention",
                    code="item_conflict",
                    error="item_conflict",
                    now=now,
                )
                logger.warning("claim_operation_needs_attention")
                return True
            claim.version += 1
            if action == "reserve" and outcome == "held":
                claim.state, claim.approved_at = "approved", now
                service.event(claim, "claim.approved", {"owner_id": claim.owner_sub})
                service.audit(claim, "claim.approved")
                result = "approved"
            elif action == "reserve":
                claim.state, claim.decision_code, claim.closed_at = (
                    "rejected",
                    "item_unavailable",
                    now,
                )
                service.audit(claim, "claim.auto_rejected")
                result = "item_unavailable"
            elif action == "release":
                claim.state, claim.closed_at = "cancelled", now
                service.event(
                    claim,
                    "claim.cancelled",
                    {"owner_id": claim.owner_sub, "reason_code": claim.decision_code},
                )
                service.audit(claim, "claim.cancelled")
                result = "cancelled"
            else:
                record = session.scalar(
                    select(HandoverRecord).where(HandoverRecord.claim_id == claim.id)
                )
                token = session.get(HandoverToken, record.token_id)
                record.state, record.completed_at = "completed", now
                token.state = "used"
                claim.state, claim.closed_at = "handed_over", now
                service.event(
                    claim,
                    "claim.handed_over",
                    {
                        "owner_id": claim.owner_sub,
                        "mode": record.mode,
                        "handed_over_at": now.isoformat(),
                    },
                )
                service.audit(claim, "claim.handed_over")
                result = "handed_over"
            self.finish(operation, "completed", code=result, now=now)
            return True

    def coordinate(self, claimed):
        # A cancellation during approval needs at most reserve -> release.
        for _ in range(3):
            snapshot = self.snapshot(claimed)
            if snapshot is None:
                return
            action, item_id, item_version, handover_id = snapshot
            if action is None:
                with self.tenant_session(claimed) as session:
                    operation = self.current(session, ClaimOperation, claimed)
                    if operation:
                        now = session.scalar(select(func.now()))
                        self.finish(operation, "completed", code="superseded", now=now)
                return
            outcome = self.call_item(action, claimed.claim_id, item_id, item_version, handover_id)
            if self.apply(claimed, action, outcome):
                return
        raise Retry("coordination_incomplete")

    def once(self, mode="tasks"):
        model = OutboxEvent if mode == "outbox" else ClaimOperation
        claimed = self.claim(model)
        if claimed is None:
            return False
        if mode == "outbox":
            try:
                self.publisher.publish(DomainEvent.model_validate(claimed.envelope))
            except Exception as exc:
                logger.warning("claim_outbox_failed", extra={"error_type": type(exc).__name__})
                with self.database.scoped_session(subject=WORKER) as session:
                    row = self.current(session, OutboxEvent, claimed)
                    if row:
                        now = session.scalar(select(func.now()))
                        terminal = row.attempts >= MAX_ATTEMPTS
                        self.finish(
                            row, "failed" if terminal else "pending", error="publish_failed"
                        )
                        row.available_at = now + timedelta(
                            seconds=random.uniform(1, min(300, 2**row.attempts))
                        )
                return True
            with self.database.scoped_session(subject=WORKER) as session:
                row = self.current(session, OutboxEvent, claimed)
                if row:
                    self.finish(row, "sent")
            return True
        try:
            self.coordinate(claimed)
        except Retry as exc:
            self.retry(claimed, exc.code)
        except Exception as exc:
            logger.warning("claim_operation_failed", extra={"error_type": type(exc).__name__})
            self.retry(claimed, "dependency_unavailable")
        return True

    def replay(self, mode, row_id, operator, reason, deadline_minutes=15):
        """Audited operator recovery; a rejected business outcome is never rewritten here."""
        model = OutboxEvent if mode == "outbox" else ClaimOperation
        allowed = {"failed"} if mode == "outbox" else {"needs_attention"}
        with self.database.scoped_session(subject=operator) as session:
            row = session.scalar(select(model).where(model.id == row_id).with_for_update())
            if row is None or row.state not in allowed:
                raise ValueError("Only failed outbox rows or operations needing attention replay")
            now = session.scalar(select(func.now()))
            row.state, row.attempts, row.available_at = "pending", 0, now
            row.last_error_code = None
            if isinstance(row, ClaimOperation):
                row.result_code, row.completed_at = None, None
                row.deadline_at = now + timedelta(minutes=deadline_minutes)
            session.add(
                AuditEntry(
                    actor_sub=operator,
                    tenant_id=row.tenant_id,
                    action=f"{mode}.replay.{reason}",
                    resource_id=row.id,
                )
            )


def main():
    parser = argparse.ArgumentParser(description="Run Claim durable workers")
    parser.add_argument("mode", choices=["tasks", "outbox"])
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--retry-id", type=UUID)
    parser.add_argument("--operator")
    parser.add_argument("--reason", choices=["dependency_restored", "manual_review"])
    args = parser.parse_args()
    settings = ClaimSettings(process_mode="worker", worker_mode=args.mode)
    configure_logging(settings.service_name, settings.log_level)
    if not settings.worker_database_url:
        parser.error("WORKER_DATABASE_URL is required")
    if args.retry_id and (not args.operator or not args.reason or len(args.operator) > 128):
        parser.error("Replay requires an operator and reason")
    db = Database(settings.model_copy(update={"database_url": settings.worker_database_url}))
    clients = []
    stopped = threading.Event()
    for sig in [signal.SIGINT, signal.SIGTERM]:
        signal.signal(sig, lambda *_: stopped.set())
    try:
        db.check_ready(DATABASE_SCHEMA, DATABASE_REVISION)
        worker = ClaimWorker(db)
        if args.retry_id:
            worker.replay(
                args.mode,
                args.retry_id,
                args.operator,
                args.reason,
                settings.operation_deadline_minutes,
            )
            return
        if args.mode == "tasks":
            worker.item_client = make_item_client(settings, make_token_provider(settings))
            if worker.item_client is None:
                parser.error("ITEM_BASE_URL and machine credentials are required")
        else:
            if not settings.event_topic_arn:
                parser.error("EVENT_TOPIC_ARN is required")
            client = aws_client("sns", settings)
            clients.append(client)
            worker.publisher = SnsPublisher(client, settings.event_topic_arn)
        while not stopped.is_set():
            worked = worker.once(args.mode)
            heartbeat()
            if args.once:
                break
            if not worked:
                stopped.wait(1)
    finally:
        for client in clients:
            client.close()
        db.close()


if __name__ == "__main__":
    main()
