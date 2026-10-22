"""match.found consumption and leased email delivery with explicit unknown outcomes."""

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
from lostlink_common.events import aws_client, decode_message
from lostlink_common.identity import (
    CognitoIdentityDirectory,
    EmailNotVerified,
    IdentityDirectoryUnavailable,
    IdentityNotFound,
)
from lostlink_common.observability import configure_logging
from sqlalchemy import and_, func, or_, select, text
from sqlalchemy.dialects.postgresql import insert

from notification_app.clients import MatchingUnavailable, make_matching_client
from notification_app.config import NotificationSettings
from notification_app.mail import (
    TEMPLATE_VERSION,
    SendNotAccepted,
    SendOutcomeUnknown,
    make_sender,
    render_match_found,
)
from notification_app.models import (
    DATABASE_REVISION,
    DATABASE_SCHEMA,
    TERMINAL,
    InboxReceipt,
    Notification,
    NotificationAttempt,
    NotificationJob,
)

logger = logging.getLogger("lostlink.notification.worker")
WORKER = "notification-worker"
CONSUMER = "notification.match_found"
MAX_ATTEMPTS = 5
LEASE_SECONDS = 60


class ProtocolError(Exception):
    """Malformed or conflicting messages must not be acknowledged."""


class Retry(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def backoff(attempts):
    base = min(1800, 30 * 2 ** max(0, attempts - 1))
    return timedelta(seconds=base * random.uniform(0.9, 1.1))


class NotificationWorker:
    def __init__(
        self,
        database: Database,
        *,
        matching_client=None,
        directory=None,
        sender=None,
        owner_app_base_url=None,
    ):
        self.database, self.matching_client = database, matching_client
        self.directory, self.sender = directory, sender
        self.owner_app_base_url = owner_app_base_url

    def session(self):
        return self.database.scoped_session(subject=WORKER)

    def consume(self, event):
        if event.event_type != "match.found" or event.tenant_id is None:
            raise ProtocolError("Event type is not accepted by the Notification consumer")
        value_hash = digest(event.model_dump(mode="json"))
        data = event.data
        with self.session() as session:
            session.execute(
                text("SELECT pg_advisory_xact_lock(:key)"),
                {"key": int.from_bytes(event.event_id.bytes[:8], signed=True)},
            )
            receipt = session.get(InboxReceipt, (CONSUMER, event.event_id))
            if receipt:
                if receipt.payload_hash != value_hash:
                    raise ProtocolError("Event ID was reused with different data")
                return "duplicate"
            # Different event IDs for one match share the business key: one notification.
            notification_id = session.scalar(
                insert(Notification)
                .values(
                    id=uuid4(),
                    tenant_id=event.tenant_id,
                    owner_sub=data["owner_id"],
                    match_id=UUID(data["match_id"]),
                    report_id=UUID(data["report_id"]),
                    item_id=UUID(data["item_id"]),
                    channel="email",
                    template_version=TEMPLATE_VERSION,
                    source_event_id=event.event_id,
                    state="pending",
                )
                .on_conflict_do_nothing(index_elements=["match_id", "owner_sub", "channel"])
                .returning(Notification.id)
            )
            if notification_id:
                session.add(NotificationJob(notification_id=notification_id, state="pending"))
            code = "created" if notification_id else "duplicate_match"
            session.add(
                InboxReceipt(
                    consumer_name=CONSUMER,
                    event_id=event.event_id,
                    payload_hash=value_hash,
                    result_code=code,
                )
            )
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
            except Exception as exc:
                # Not deleted: redelivery and the queue's DLQ policy handle it.
                logger.warning(
                    "notification_message_rejected", extra={"error_type": type(exc).__name__}
                )
                continue
            client.delete_message(QueueUrl=queue_url, ReceiptHandle=message["ReceiptHandle"])
        return bool(messages)

    def claim(self):
        """Return a leased job, 'handled' when a stale or exhausted job was closed, or None."""
        with self.session() as session:
            now = session.scalar(select(func.now()))
            job = session.scalar(
                select(NotificationJob)
                .where(
                    or_(
                        and_(
                            NotificationJob.state == "pending",
                            NotificationJob.available_at <= now,
                        ),
                        and_(NotificationJob.state == "leased", NotificationJob.lease_until < now),
                    )
                )
                .order_by(NotificationJob.available_at, NotificationJob.notification_id)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if job is None:
                return None
            notification = session.scalar(
                select(Notification).where(Notification.id == job.notification_id).with_for_update()
            )
            started = session.scalar(
                select(NotificationAttempt).where(
                    NotificationAttempt.notification_id == job.notification_id,
                    NotificationAttempt.finished_at.is_(None),
                )
            )
            if started is not None:
                # The previous worker may have handed the message to the provider.
                started.result, started.error_code = "unknown", "worker_interrupted"
                started.finished_at = now
                notification.state, notification.reason_code = (
                    "delivery_unknown",
                    "worker_interrupted",
                )
                self.release(job, "stopped", "worker_interrupted")
                logger.warning("notification_delivery_unknown")
                return "handled"
            if job.attempts >= MAX_ATTEMPTS:
                notification.state, notification.reason_code = "failed", "retry_exhausted"
                self.release(job, "stopped", "retry_exhausted")
                return "handled"
            job.state, job.attempts, job.lease_token = "leased", job.attempts + 1, uuid4()
            job.lease_until = now + timedelta(seconds=LEASE_SECONDS)
            session.flush()
            return {
                "notification_id": job.notification_id,
                "lease_token": job.lease_token,
                "attempts": job.attempts,
            }

    @staticmethod
    def release(job, state, error=None):
        job.state, job.last_error_code = state, error
        job.lease_token = job.lease_until = None

    def current(self, session, claimed):
        return session.scalar(
            select(NotificationJob)
            .where(
                NotificationJob.notification_id == claimed["notification_id"],
                NotificationJob.state == "leased",
                NotificationJob.lease_token == claimed["lease_token"],
            )
            .with_for_update()
        )

    def close(self, claimed, state, reason, *, job_state):
        with self.session() as session:
            job = self.current(session, claimed)
            if job is None:
                return
            notification = session.get(Notification, claimed["notification_id"])
            notification.state, notification.reason_code = state, reason
            self.release(job, job_state, reason)

    def retry(self, claimed, code, *, notification_state=None):
        with self.session() as session:
            job = self.current(session, claimed)
            if job is None:
                return
            now = session.scalar(select(func.now()))
            notification = session.get(Notification, claimed["notification_id"])
            if job.attempts >= MAX_ATTEMPTS:
                notification.state, notification.reason_code = "failed", code
                self.release(job, "stopped", code)
                return
            if notification_state:
                notification.state = notification_state
            self.release(job, "pending", code)
            job.available_at = now + backoff(job.attempts)

    def deliver(self, claimed):
        with self.session() as session:
            job = self.current(session, claimed)
            if job is None:
                return
            notification = session.get(Notification, claimed["notification_id"])
            if notification.state in TERMINAL:
                self.release(job, "done")
                return
            snapshot = {
                "match_id": notification.match_id,
                "owner_sub": notification.owner_sub,
                "report_id": notification.report_id,
                "item_id": notification.item_id,
                "tenant_id": notification.tenant_id,
            }
        try:
            context = self.matching_client.context(snapshot["match_id"])
        except MatchingUnavailable as exc:
            raise Retry("matching_unavailable") from exc
        if context is None:
            self.close(claimed, "suppressed", "match_not_found", job_state="done")
            return
        if (
            context.owner_id != snapshot["owner_sub"]
            or context.report_id != snapshot["report_id"]
            or context.item_id != snapshot["item_id"]
            or context.tenant_id != snapshot["tenant_id"]
        ):
            logger.warning("notification_context_mismatch")
            self.close(claimed, "suppressed", "context_mismatch", job_state="done")
            return
        if not context.active:
            self.close(claimed, "suppressed", "match_inactive", job_state="done")
            return
        try:
            address = self.directory.resolve_verified_email(snapshot["owner_sub"]).email
        except (IdentityNotFound, EmailNotVerified) as exc:
            self.close(claimed, "failed", exc.code, job_state="stopped")
            return
        except IdentityDirectoryUnavailable as exc:
            raise Retry(exc.code) from exc
        message = render_match_found(
            to=address,
            match_id=snapshot["match_id"],
            item_title=context.item_title,
            venue_name=context.venue_name,
            base_url=self.owner_app_base_url,
        )
        with self.session() as session:
            job = self.current(session, claimed)
            if job is None:
                return
            notification = session.get(Notification, claimed["notification_id"])
            number = session.scalar(
                select(func.count()).where(NotificationAttempt.notification_id == notification.id)
            )
            attempt = NotificationAttempt(
                id=uuid4(),
                notification_id=notification.id,
                attempt_number=number + 1,
                lease_token=job.lease_token,
            )
            session.add(attempt)
            notification.state = "sending"
            attempt_id = attempt.id
        # The started attempt is committed before the provider call; a crash is now unknown.
        try:
            provider_id = self.sender.send(message)
        except SendNotAccepted as exc:
            self.finish_attempt(claimed, attempt_id, "not_sent", exc.code)
            if exc.retryable:
                self.retry(claimed, exc.code, notification_state="retry_wait")
            else:
                self.close(claimed, "failed", exc.code, job_state="stopped")
            return
        except Exception as exc:
            code = exc.code if isinstance(exc, SendOutcomeUnknown) else "provider_unknown"
            self.finish_attempt(claimed, attempt_id, "unknown", code)
            self.close(claimed, "delivery_unknown", code, job_state="stopped")
            logger.warning("notification_delivery_unknown")
            return
        with self.session() as session:
            now = session.scalar(select(func.now()))
            attempt = session.get(NotificationAttempt, attempt_id)
            attempt.result, attempt.provider_message_id = "sent", provider_id
            attempt.finished_at = now
            notification = session.get(Notification, claimed["notification_id"])
            notification.state, notification.reason_code, notification.sent_at = (
                "sent",
                None,
                now,
            )
            # Record acceptance even if the lease was lost: the provider accepted it.
            job = session.get(NotificationJob, claimed["notification_id"], with_for_update=True)
            self.release(job, "done")

    def finish_attempt(self, claimed, attempt_id, result, code):
        with self.session() as session:
            attempt = session.get(NotificationAttempt, attempt_id)
            attempt.result, attempt.error_code = result, code
            attempt.finished_at = session.scalar(select(func.now()))

    def once(self):
        claimed = self.claim()
        if claimed is None:
            return False
        if claimed == "handled":
            return True
        try:
            self.deliver(claimed)
        except Retry as exc:
            self.retry(claimed, exc.code)
        except Exception as exc:
            logger.warning("notification_task_failed", extra={"error_type": type(exc).__name__})
            self.retry(claimed, "dependency_unavailable")
        return True


def main():
    parser = argparse.ArgumentParser(description="Run Notification durable workers")
    parser.add_argument("mode", choices=["consume", "tasks"])
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    settings = NotificationSettings(process_mode="worker", worker_mode=args.mode)
    configure_logging(settings.service_name, settings.log_level)
    if not settings.worker_database_url:
        parser.error("WORKER_DATABASE_URL is required")
    db = Database(settings.model_copy(update={"database_url": settings.worker_database_url}))
    clients = []
    stopped = threading.Event()
    for sig in [signal.SIGINT, signal.SIGTERM]:
        signal.signal(sig, lambda *_: stopped.set())
    try:
        db.check_ready(DATABASE_SCHEMA, DATABASE_REVISION)
        worker = NotificationWorker(db, owner_app_base_url=settings.owner_app_base_url)
        queue = None
        if args.mode == "consume":
            if not settings.event_queue_url:
                parser.error("EVENT_QUEUE_URL is required")
            queue = aws_client("sqs", settings)
            clients.append(queue)
        else:
            worker.matching_client = make_matching_client(settings)
            if not settings.cognito_user_pool_id:
                parser.error("COGNITO_USER_POOL_ID is required")
            cognito = aws_client("cognito-idp", settings)
            clients.append(cognito)
            worker.directory = CognitoIdentityDirectory(cognito, settings.cognito_user_pool_id)
            worker.sender = make_sender(settings)
            if not all([worker.matching_client, worker.sender, settings.owner_app_base_url]):
                parser.error("Matching, sender and OWNER_APP_BASE_URL configuration is required")
            if hasattr(worker.sender, "client"):
                clients.append(worker.sender.client)
        while not stopped.is_set():
            worked = worker.poll(queue, settings.event_queue_url) if queue else worker.once()
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
