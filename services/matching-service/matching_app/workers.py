"""Durable inbox, CPU embedding jobs, paged rematching and leased SNS outbox delivery."""

import argparse
import logging
import random
import signal
import threading
from datetime import timedelta
from uuid import UUID, uuid4

from lostlink_common.events import DomainEvent, SnsPublisher, aws_client, decode_message
from lostlink_common.observability import configure_logging
from lostlink_common.worker_health import heartbeat
from sqlalchemy import Float, and_, func, or_, select, text
from sqlalchemy.dialects.postgresql import insert

from matching_app.clients import make_item_client
from matching_app.config import MatchingSettings
from matching_app.database import MatchingDatabase
from matching_app.embedding import ClipEncoder, EmbeddingError, normalize
from matching_app.models import (
    DATABASE_REVISION,
    DATABASE_SCHEMA,
    AuditEntry,
    Catalog,
    InboxReceipt,
    ItemVector,
    OutboxEvent,
    Report,
    WorkJob,
)
from matching_app.service import candidates, catalog_upsert, emit, filters_match, save_match
from matching_app.transactions import digest

logger = logging.getLogger("lostlink.matching.worker")


class ProtocolError(Exception):
    """Rejected deliveries remain unacknowledged and eventually reach the SQS DLQ."""


class MatchingWorker:
    def __init__(
        self,
        database,
        *,
        encoder=None,
        item_client=None,
        storage=None,
        publisher=None,
        settings=None,
    ):
        self.database, self.encoder, self.item_client = database, encoder, item_client
        self.storage, self.publisher = storage, publisher
        self.settings = settings or MatchingSettings(_env_file=None)

    def consume(self, event):
        if event.event_type not in {"item.registered", "item.catalog_changed"}:
            raise ProtocolError("Unsupported event")
        value_hash = digest(event.model_dump(mode="json"))
        with self.database.context(subject="matching-worker") as session:
            session.execute(
                text("SELECT pg_advisory_xact_lock(:key)"),
                {"key": int.from_bytes(event.event_id.bytes[:8], signed=True)},
            )
            receipt = session.get(InboxReceipt, event.event_id)
            if receipt:
                if receipt.payload_hash != value_hash:
                    raise ProtocolError("Event ID conflicts with its recorded payload")
                return "duplicate"
            item_id = UUID(event.data["item_id"])
            # An Item ID cannot move between tenants, including under delayed events.
            known = session.get(Catalog, item_id) or session.get(ItemVector, item_id)
            if known and known.tenant_id != event.tenant_id:
                raise ProtocolError("Item tenant cannot change")
            if event.event_type == "item.registered":
                session.add(
                    WorkJob(
                        kind="embed",
                        trigger_id=event.event_id,
                        resource_id=item_id,
                        envelope=event.model_dump(mode="json"),
                    )
                )
            else:
                current = catalog_upsert(session, event)
                if current and current.state == "available":
                    session.add(
                        WorkJob(kind="item_match", trigger_id=event.event_id, resource_id=item_id)
                    )
            session.add(InboxReceipt(event_id=event.event_id, payload_hash=value_hash))
        return "accepted"

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
                logger.warning("index_message_rejected", extra={"error_type": type(exc).__name__})
                continue
            # The inbox and durable job are committed before acknowledging SQS.
            client.delete_message(QueueUrl=queue_url, ReceiptHandle=message["ReceiptHandle"])
        return bool(messages)

    def claim(self, model):
        with self.database.context(subject="matching-worker") as session:
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
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            if row is None:
                return None
            # Terminal transition belongs to failure(), including embedding failure events.
            row.state, row.attempts, row.lease_token = "leased", row.attempts + 1, uuid4()
            row.lease_until = now + timedelta(seconds=300)
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

    @staticmethod
    def finish(row, state="completed", code=None):
        row.state, row.last_error_code = state, code
        row.lease_token, row.lease_until = None, None

    def failure(self, model, claimed, code, retryable):
        with self.database.context(subject="matching-worker") as session:
            row = self.current(session, model, claimed)
            if row is None:
                return
            terminal = not retryable or row.attempts >= 10
            self.finish(row, "failed" if terminal else "pending", code)
            row.available_at = session.scalar(select(func.now())) + timedelta(
                seconds=random.uniform(1, min(300, 2 ** min(row.attempts, 10)))
            )
            if terminal and isinstance(row, WorkJob) and row.kind == "embed":
                original = DomainEvent.model_validate(row.envelope)
                emit(
                    session,
                    DomainEvent(
                        event_type="item.embedding_failed",
                        source="embedding-worker",
                        tenant_id=original.tenant_id,
                        correlation_id=original.correlation_id,
                        data={
                            "item_id": str(row.resource_id),
                            "item_version": original.data["item_version"],
                            "error_code": code if code == "invalid_image" else "retry_exhausted",
                        },
                    ),
                )

    def image_bytes(self, context):
        if context.photo_bucket not in self.settings.item_photo_buckets:
            raise ProtocolError("Photo bucket is not allowlisted")
        response = self.storage.get_object(Bucket=context.photo_bucket, Key=context.photo_key)
        stream = response["Body"]
        try:
            if response.get("ContentLength", 0) > 10 * 1024 * 1024:
                raise EmbeddingError("invalid_image", False)
            value = stream.read(10 * 1024 * 1024 + 1)
            if not value or len(value) > 10 * 1024 * 1024:
                raise EmbeddingError("invalid_image", False)
            return value
        finally:
            stream.close()

    @staticmethod
    def authoritative(context, event):
        return (
            context is not None
            and context.state in {"processing", "available"}
            and context.item_version == event.data["item_version"]
            and context.tenant_id == event.tenant_id
            and context.processing_correlation_id == event.correlation_id
            and context.photo_bucket == event.data["photo_bucket"]
            and context.photo_key == event.data["photo_key"]
            and context.description == event.data["description"]
            and context.category == event.data["category"]
        )

    def embed(self, claimed):
        event = DomainEvent.model_validate(claimed.envelope)
        # Both network reads and inference are outside the SQL transaction.
        current = self.item_client.context(claimed.resource_id)
        if not self.authoritative(current, event):
            with self.database.context(subject="matching-worker") as session:
                row = self.current(session, WorkJob, claimed)
                if row:
                    self.finish(row, "superseded")
            return
        vector = normalize(self.encoder.encode(current.description, self.image_bytes(current)))
        latest = self.item_client.context(claimed.resource_id)
        with self.database.context(subject="matching-worker") as session:
            row = self.current(session, WorkJob, claimed)
            if row is None:
                return
            if not self.authoritative(latest, event):
                self.finish(row, "superseded")
                return
            values = {
                "item_id": row.resource_id,
                "tenant_id": event.tenant_id,
                "item_version": latest.item_version,
                "source_revision": latest.revision,
                "correlation_id": event.correlation_id,
                "model_version": self.encoder.model_version,
                "embedding": vector,
            }
            statement = insert(ItemVector).values(**values)
            session.execute(
                statement.on_conflict_do_update(
                    index_elements=[ItemVector.item_id],
                    set_={k: getattr(statement.excluded, k) for k in values if k != "item_id"},
                    where=(statement.excluded.source_revision >= ItemVector.source_revision)
                    & (statement.excluded.tenant_id == ItemVector.tenant_id),
                )
            )
            completed = DomainEvent(
                event_type="item.embedded",
                source="embedding-worker",
                tenant_id=event.tenant_id,
                correlation_id=event.correlation_id,
                data={
                    "item_id": str(row.resource_id),
                    "item_version": latest.item_version,
                    "model_version": self.encoder.model_version,
                    "dimensions": 512,
                },
            )
            emit(session, completed)
            session.add(
                WorkJob(
                    kind="item_match", resource_id=row.resource_id, trigger_id=completed.event_id
                )
            )
            self.finish(row)

    def report_match(self, claimed):
        with self.database.context(subject="matching-worker") as session:
            job = self.current(session, WorkJob, claimed)
            if job is None:
                return
            row = session.scalar(
                select(Report).where(Report.id == claimed.resource_id).with_for_update()
            )
            if row and row.state == "open":
                for item, embedding, score in candidates(
                    session,
                    row.embedding,
                    row.model_version,
                    row,
                    self.settings.candidate_limit,
                    self.settings.match_threshold,
                ):
                    save_match(session, row, item, embedding, score)
            self.finish(job)

    def item_match(self, claimed):
        with self.database.context(subject="matching-worker") as session:
            job = self.current(session, WorkJob, claimed)
            if job is None:
                return
            item, embedding = (
                session.get(Catalog, job.resource_id),
                session.get(ItemVector, job.resource_id),
            )
            if (
                item is None
                or embedding is None
                or item.state != "available"
                or item.item_version != embedding.item_version
                or item.tenant_id != embedding.tenant_id
            ):
                self.finish(job)
                return
            query = select(Report).where(Report.state == "open").order_by(Report.id).limit(50)
            if job.cursor:
                query = query.where(Report.id > UUID(job.cursor))
            rows = list(session.scalars(query.with_for_update()))
            for row in rows:
                if row.model_version != embedding.model_version or not filters_match(item, row):
                    continue
                distance = Report.embedding.op("<=>", return_type=Float())(ItemVector.embedding)
                score = session.scalar(
                    select(1 - distance)
                    .select_from(Report)
                    .join(ItemVector, ItemVector.item_id == item.item_id)
                    .where(Report.id == row.id)
                )
                score = max(-1.0, min(1.0, float(score)))
                if score >= self.settings.match_threshold:
                    save_match(session, row, item, embedding, score)
            if len(rows) == 50:
                job.cursor = str(rows[-1].id)
                self.finish(job, "pending")
                job.attempts = 0  # Each bounded page gets its own failure budget.
            else:
                self.finish(job)

    def once(self, mode="tasks"):
        model = OutboxEvent if mode == "outbox" else WorkJob
        claimed = self.claim(model)
        if claimed is None:
            return False
        try:
            if claimed.attempts > 10:
                self.failure(model, claimed, "retry_exhausted", False)
            elif mode == "outbox":
                self.publisher.publish(DomainEvent.model_validate(claimed.envelope))
                with self.database.context(subject="matching-worker") as session:
                    row = self.current(session, model, claimed)
                    if row:
                        self.finish(row, "sent")
            elif claimed.kind == "embed":
                self.embed(claimed)
            elif claimed.kind == "item_match":
                self.item_match(claimed)
            elif claimed.kind == "report_match":
                self.report_match(claimed)
            else:
                raise ProtocolError("Unsupported durable job")
        except EmbeddingError as exc:
            self.failure(model, claimed, exc.code, exc.retryable)
        except Exception as exc:
            logger.warning("matching_task_failed", extra={"error_type": type(exc).__name__})
            self.failure(model, claimed, "dependency_unavailable", True)
        return True

    def retry_failed(self, model, job_id, operator):
        with self.database.context(subject=operator) as session:
            row = session.scalar(select(model).where(model.id == job_id).with_for_update())
            if row is None or row.state != "failed":
                raise ValueError("Only failed jobs can be replayed")
            if isinstance(row, WorkJob) and row.kind == "embed":
                raise ValueError("Retry embedding through Item to obtain a fresh correlation ID")
            self.finish(row, "pending")
            row.attempts = 0
            row.available_at = session.scalar(select(func.now()))
            session.add(
                AuditEntry(actor_sub=operator, resource_id=row.id, action="operation.retry")
            )


def main():
    parser = argparse.ArgumentParser(description="Run Matching and Embedding durable workers")
    parser.add_argument("mode", choices=["consume", "tasks", "outbox"])
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--retry-id", type=UUID)
    parser.add_argument("--operator")
    args = parser.parse_args()
    settings = MatchingSettings(process_mode="worker", worker_mode=args.mode)
    configure_logging(settings.service_name, settings.log_level)
    if not settings.worker_database_url:
        parser.error("WORKER_DATABASE_URL is required")
    if args.retry_id and (not args.operator or len(args.operator) > 128 or args.mode == "consume"):
        parser.error("Replay requires an operator and tasks/outbox mode")
    db = MatchingDatabase(
        settings.model_copy(update={"database_url": settings.worker_database_url})
    )
    clients, queue = [], None
    stop = threading.Event()
    for sig in [signal.SIGINT, signal.SIGTERM]:
        signal.signal(sig, lambda *_: stop.set())
    try:
        db.check_ready(DATABASE_SCHEMA, DATABASE_REVISION)
        worker = MatchingWorker(db, settings=settings)
        if args.retry_id:
            worker.retry_failed(
                OutboxEvent if args.mode == "outbox" else WorkJob, args.retry_id, args.operator
            )
            return
        if args.mode == "consume":
            if not settings.event_queue_url:
                parser.error("EVENT_QUEUE_URL is required")
            queue = aws_client("sqs", settings)
            clients.append(queue)
        elif args.mode == "outbox":
            if not settings.event_topic_arn:
                parser.error("EVENT_TOPIC_ARN is required")
            client = aws_client("sns", settings)
            clients.append(client)
            worker.publisher = SnsPublisher(client, settings.event_topic_arn)
        else:
            if not settings.item_photo_buckets:
                parser.error("ITEM_PHOTO_BUCKETS is required")
            from matching_app.storage import s3_client

            client = s3_client(settings)
            clients.append(client)
            worker.storage, worker.item_client = client, make_item_client(settings)
            if worker.item_client is None:
                parser.error("Authenticated Item access is required")
            worker.encoder = ClipEncoder(
                settings.clip_checkpoint, settings.clip_sha256, settings.clip_device
            )
            worker.encoder.ready()
        while not stop.is_set():
            worked = (
                worker.poll(queue, settings.event_queue_url) if queue else worker.once(args.mode)
            )
            heartbeat()
            if args.once:
                break
            if not worked:
                stop.wait(1)
    finally:
        for client in clients:
            client.close()
        db.close()


if __name__ == "__main__":
    main()
