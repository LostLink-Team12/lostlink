"""Leased tasks, outbox delivery and transactional embedding-result consumption."""

import argparse
import logging
import random
import signal
import threading
from datetime import timedelta
from uuid import UUID, uuid4

from lostlink_common.events import (
    DomainEvent,
    SnsPublisher,
    acknowledge_message,
    aws_client,
    decode_message,
    receive_messages,
)
from lostlink_common.observability import configure_logging
from lostlink_common.worker_health import heartbeat
from pydantic import ValidationError
from sqlalchemy import and_, func, or_, select, text

from item_app.config import ItemSettings
from item_app.database import ItemDatabase
from item_app.models import (
    DATABASE_REVISION,
    DATABASE_SCHEMA,
    AuditEntry,
    ImportEntry,
    ImportJob,
    InboxReceipt,
    Item,
    ItemLocator,
    OutboxEvent,
    Photo,
    Reservation,
    WorkJob,
)
from item_app.schemas import NewItem
from item_app.service import ItemService
from item_app.storage import PhotoStorage, StorageError, s3_client
from item_app.transactions import digest

logger = logging.getLogger("lostlink.item.worker")


class ProtocolError(Exception):
    """Malformed or conflicting messages must not be acknowledged."""


class ItemWorker:
    def __init__(self, database, *, storage=None, publisher=None):
        self.database, self.storage, self.publisher = database, storage, publisher

    def claim(self, model):
        with self.database.context(subject="item-worker") as session:
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
            if row.attempts >= 10:
                row.state, row.last_error_code = "failed", "retry_exhausted"
                row.lease_until, row.lease_token = None, None
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

    def finish(self, row, state="completed", error=None):
        row.state, row.last_error_code = state, error
        row.lease_until, row.lease_token = None, None

    def failure(self, model, claimed, code, retryable):
        with self.database.context(subject="item-worker", tenant_id=claimed.tenant_id) as session:
            row = self.current(session, model, claimed)
            if row is None:
                return
            terminal = not retryable or row.attempts >= 10
            self.finish(row, "failed" if terminal else "pending", code)
            row.available_at = session.scalar(select(func.now())) + timedelta(
                seconds=random.uniform(1, min(300, 2**row.attempts))
            )
            if terminal and isinstance(row, WorkJob) and row.kind == "photo":
                photo = session.get(Photo, row.resource_id)
                if photo and photo.status not in {"confirmed", "superseded", "purging", "purged"}:
                    photo.status = "failed"

    def photo(self, claimed):
        with self.database.context(subject="item-worker", tenant_id=claimed.tenant_id) as session:
            job = self.current(session, WorkJob, claimed)
            if job is None:
                return
            photo = session.get(Photo, claimed.resource_id)
            item = session.scalar(select(Item).where(Item.id == photo.item_id).with_for_update())
            if photo.status == "confirmed":
                self.finish(job)
                return
            if (
                item.active_upload_id != photo.id
                or item.item_version != photo.item_version
                or item.state != "awaiting_upload"
                or photo.status not in {"pending", "sealing"}
            ):
                self.finish(job, "superseded")
                return
            photo.status = "sealing"
        etag = self.storage.seal(photo)  # Bounded network I/O, no SQL transaction or lock held.
        with self.database.context(subject="item-worker", tenant_id=claimed.tenant_id) as session:
            job = self.current(session, WorkJob, claimed)
            if job is None:
                return
            item = session.scalar(select(Item).where(Item.id == photo.item_id).with_for_update())
            current = session.get(Photo, photo.id)
            if (
                item.active_upload_id != current.id
                or item.item_version != current.item_version
                or item.state != "awaiting_upload"
                or current.status != "sealing"
            ):
                self.finish(job, "superseded")
                return
            current.status, current.etag = "confirmed", etag
            item.photo_id, item.active_upload_id = current.id, None
            item.version += 1
            service = ItemService(session, "item-worker")
            service.start_processing(item)
            service.catalog(item)
            service.audit(item, "photo.confirmed")
            self.finish(job)

    def import_entry(self, claimed):
        with self.database.context(subject="item-worker", tenant_id=claimed.tenant_id) as session:
            task = self.current(session, WorkJob, claimed)
            if task is None:
                return
            entry = session.get(ImportEntry, claimed.resource_id)
            job = session.scalar(
                select(ImportJob).where(ImportJob.id == entry.job_id).with_for_update()
            )
            session.refresh(entry)
            if entry.state != "pending" or entry.generation != claimed.generation:
                self.finish(task, "superseded")
                return
            try:
                body = NewItem.model_validate(entry.payload)
            except ValidationError:
                entry.state, entry.error_code = "failed", "validation_error"
                job.failed += 1
            else:
                value, _, _ = ItemService(session, "item-worker").create(
                    job.tenant_id, body, created_by=job.created_by
                )
                entry.item_id, entry.state = UUID(value["id"]), "succeeded"
                job.accepted += 1
            if job.accepted + job.failed == job.total:
                job.state = "completed" if not job.failed else "completed_with_errors"
            self.finish(task)

    def watch(self, claimed):
        with self.database.context(subject="item-worker", tenant_id=claimed.tenant_id) as session:
            task = self.current(session, WorkJob, claimed)
            if task is None:
                return
            reservation = session.get(Reservation, task.resource_id)
            if reservation and reservation.state == "held":
                self.finish(task, "needs_attention", "reservation_recovery_required")
                logger.warning("reservation_recovery_required")
            else:
                self.finish(task)

    def once(self, mode):
        model = OutboxEvent if mode == "outbox" else WorkJob
        claimed = self.claim(model)
        if claimed is None:
            return False
        try:
            if mode == "outbox":
                self.publisher.publish(DomainEvent.model_validate(claimed.envelope))
                with self.database.context(subject="item-worker") as session:
                    row = self.current(session, model, claimed)
                    if row:
                        self.finish(row, "sent")
            elif claimed.kind == "photo":
                self.photo(claimed)
            elif claimed.kind == "import_entry":
                self.import_entry(claimed)
            elif claimed.kind == "reservation_watch":
                self.watch(claimed)
            else:
                raise ProtocolError("Unsupported task")
        except StorageError as exc:
            self.failure(model, claimed, exc.code, exc.retryable)
        except Exception as exc:
            logger.warning("item_task_failed", extra={"error_type": type(exc).__name__})
            self.failure(model, claimed, "dependency_unavailable", True)
        return True

    def consume(self, event):
        if event.event_type not in {"item.embedded", "item.embedding_failed"}:
            raise ProtocolError("Event type is not accepted by the Item consumer")
        item_id = UUID(event.data["item_id"])
        with self.database.context(resource_id=item_id) as session:
            locator = session.get(ItemLocator, item_id)
        if locator and locator.tenant_id != event.tenant_id:
            raise ProtocolError("Event tenant does not match authoritative item")
        value_hash = digest(event.model_dump(mode="json"))
        with self.database.context(
            subject="embedding-consumer", tenant_id=event.tenant_id
        ) as session:
            session.execute(
                text("SELECT pg_advisory_xact_lock(:key)"),
                {"key": int.from_bytes(event.event_id.bytes[:8], signed=True)},
            )
            receipt = session.get(InboxReceipt, event.event_id)
            if receipt:
                if receipt.payload_hash != value_hash:
                    raise ProtocolError("Event ID was reused with different data")
                return "duplicate"
            service = ItemService(session, "embedding-consumer")
            item = session.scalar(select(Item).where(Item.id == item_id).with_for_update())
            code = "ignored"
            if (
                item
                and item.state == "processing"
                and item.item_version == event.data["item_version"]
                and item.processing_correlation_id == event.correlation_id
            ):
                item.state = (
                    "available" if event.event_type == "item.embedded" else "processing_failed"
                )
                item.version += 1
                service.catalog(item)
                service.audit(item, "item.embedding_result")
                code = "applied"
            session.add(
                InboxReceipt(event_id=event.event_id, payload_hash=value_hash, result_code=code)
            )
            return code

    def poll(self, client, queue_url, *, wait_seconds=10):
        messages = receive_messages(client, queue_url, wait_seconds=wait_seconds)
        for message in messages:
            try:
                self.consume(decode_message(message["Body"]))
            except Exception as exc:
                logger.warning("item_message_rejected", extra={"error_type": type(exc).__name__})
                continue
            acknowledge_message(client, queue_url, message["ReceiptHandle"])
        return bool(messages)

    def retry_failed(self, mode, job_id, operator, reason):
        model = OutboxEvent if mode == "outbox" else WorkJob
        with self.database.context(subject=operator) as session:
            row = session.scalar(select(model).where(model.id == job_id).with_for_update())
            if row is None or row.state != "failed":
                raise ValueError("Only failed jobs can be replayed")
            # Photo terminal errors require a fresh upload; retry only dependency failures.
            if (
                isinstance(row, WorkJob)
                and row.kind == "photo"
                and row.last_error_code
                not in {"storage_unavailable", "dependency_unavailable", "retry_exhausted"}
            ):
                raise ValueError("Create a fresh upload for terminal photo errors")
            tenant, resource = row.tenant_id, row.id
            row.state, row.attempts = "pending", 0
            row.available_at = session.scalar(select(func.now()))
            row.last_error_code = None
            session.add(
                AuditEntry(
                    actor_sub=operator,
                    tenant_id=tenant,
                    action=f"{mode}.retry.{reason}",
                    resource_id=resource,
                )
            )
            if isinstance(row, WorkJob) and row.kind == "photo":
                session.execute(
                    text("SELECT set_config('app.tenant_id', :tenant, true)"),
                    {"tenant": str(tenant)},
                )
                photo = session.get(Photo, row.resource_id)
                if photo:
                    if photo.status == "failed":
                        photo.status = "pending"


def main():
    parser = argparse.ArgumentParser(description="Run Item durable workers")
    parser.add_argument("mode", choices=["tasks", "outbox", "consume", "cleanup"])
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--retry-id", type=UUID)
    parser.add_argument("--operator")
    parser.add_argument("--reason", choices=["dependency_restored", "manual_review"])
    args = parser.parse_args()
    settings = ItemSettings(process_mode="worker")
    configure_logging(settings.service_name, settings.log_level)
    if not settings.worker_database_url:
        parser.error("WORKER_DATABASE_URL is required")
    if args.retry_id and (not args.operator or not args.reason or len(args.operator) > 128):
        parser.error("Replay requires an operator and reason")
    db = ItemDatabase(settings.model_copy(update={"database_url": settings.worker_database_url}))
    clients = []
    stopped = threading.Event()
    for sig in [signal.SIGINT, signal.SIGTERM]:
        signal.signal(sig, lambda *_: stopped.set())
    try:
        db.check_ready(DATABASE_SCHEMA, DATABASE_REVISION)
        worker = ItemWorker(db)
        if args.retry_id:
            if args.mode not in {"tasks", "outbox"}:
                parser.error("Only tasks and outbox support replay")
            worker.retry_failed(args.mode, args.retry_id, args.operator, args.reason)
            return
        queue = None
        if args.mode in {"tasks", "cleanup"}:
            if not settings.photo_bucket:
                parser.error("PHOTO_BUCKET is required")
            client = s3_client(settings)
            clients.append(client)
            worker.storage = PhotoStorage(client)
        elif args.mode == "outbox":
            if not settings.event_topic_arn:
                parser.error("EVENT_TOPIC_ARN is required")
            client = aws_client("sns", settings)
            clients.append(client)
            worker.publisher = SnsPublisher(client, settings.event_topic_arn)
        else:
            if not settings.event_queue_url:
                parser.error("EVENT_QUEUE_URL is required")
            queue = aws_client("sqs", settings)
            clients.append(queue)
        if args.mode == "cleanup":
            from item_app.cleanup import cleanup

            while not stopped.is_set():
                cleanup(
                    db,
                    worker.storage,
                    settings.photo_bucket,
                    stopped=stopped,
                    prefixes=settings.cleanup_prefixes,
                )
                heartbeat()
                if args.once:
                    break
                for _ in range(120):
                    if stopped.wait(30):
                        break
                    heartbeat()
            return
        while not stopped.is_set():
            worked = (
                worker.poll(queue, settings.event_queue_url) if queue else worker.once(args.mode)
            )
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
