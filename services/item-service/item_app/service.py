"""Atomic item state, content versions, immutable upload slots and reservations."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from lostlink_common.events import DomainEvent
from sqlalchemy import delete, select, text

from item_app.errors import fail
from item_app.models import (
    AuditEntry,
    Item,
    ItemLocator,
    OutboxEvent,
    Photo,
    Question,
    Reservation,
    WorkJob,
)
from item_app.transactions import expected_version


def item_view(item):
    return {
        name: (
            str(value)
            if name in {"id", "photo_id", "active_upload_id"} and value
            else value.isoformat()
            if name in {"found_at", "created_at"}
            else value
        )
        for name in [
            "id",
            "title",
            "description",
            "category",
            "found_at",
            "created_at",
            "public_location_hint",
            "state",
            "item_version",
            "version",
            "photo_id",
            "active_upload_id",
        ]
        for value in [getattr(item, name)]
    }


class ItemService:
    def __init__(self, session, subject, request_id=None):
        self.session, self.subject, self.request_id = session, subject, request_id

    def get(self, item_id, *, lock=False):
        query = select(Item).where(Item.id == item_id).execution_options(populate_existing=True)
        if lock:
            query = query.with_for_update()
        item = self.session.scalar(query)
        if item is None:
            fail(404, "item_not_found", "Item is not visible")
        return item

    def mutable(self, item):
        if item.state in {"reserved", "handed_over", "withdrawn"}:
            fail(409, "item_immutable", "This item cannot be changed")

    def audit(self, item, action):
        self.session.add(
            AuditEntry(
                tenant_id=item.tenant_id,
                actor_sub=self.subject,
                action=action,
                resource_id=item.id,
                request_id=self.request_id,
            )
        )

    def event(self, item, event_type, data, correlation_id=None):
        options = {"correlation_id": correlation_id} if correlation_id else {}
        event = DomainEvent(
            event_type=event_type,
            source="item-service",
            tenant_id=item.tenant_id,
            data=data,
            **options,
        )
        self.session.add(
            OutboxEvent(
                id=event.event_id, tenant_id=item.tenant_id, envelope=event.model_dump(mode="json")
            )
        )

    def catalog(self, item):
        photo = self.session.get(Photo, item.photo_id) if item.photo_id else None
        self.event(
            item,
            "item.catalog_changed",
            {
                "item_id": str(item.id),
                "item_version": item.item_version,
                "revision": item.version,
                "state": item.state,
                "title": item.title,
                "description": item.description,
                "category": item.category,
                "found_at": item.found_at.isoformat(),
                "venue_id": str(item.tenant_id),
                "photo_bucket": photo.bucket if photo else None,
                "photo_key": photo.sealed_key if photo else None,
            },
        )

    def start_processing(self, item):
        photo = self.session.get(Photo, item.photo_id)
        if photo is None or photo.status != "confirmed":
            fail(409, "photo_not_confirmed", "A sealed photo is required")
        item.state, item.processing_correlation_id = "processing", uuid4()
        self.event(
            item,
            "item.registered",
            {
                "item_id": str(item.id),
                "item_version": item.item_version,
                "photo_bucket": photo.bucket,
                "photo_key": photo.sealed_key,
                "description": item.description,
                "category": item.category,
                "found_at": item.found_at.isoformat(),
            },
            item.processing_correlation_id,
        )

    def questions(self, item, questions):
        for position, question in enumerate(questions):
            self.session.add(
                Question(
                    tenant_id=item.tenant_id,
                    item_id=item.id,
                    prompt=question.prompt,
                    reference_answer=question.reference_answer,
                    position=position,
                    version=item.item_version,
                )
            )

    def create(self, tenant, body, *, created_by=None):
        data = body.model_dump(exclude={"verification_questions"})
        item = Item(tenant_id=tenant, created_by=created_by or self.subject, **data)
        self.session.add(item)
        self.session.flush()
        self.session.add(ItemLocator(item_id=item.id, tenant_id=tenant))
        self.questions(item, body.verification_questions)
        self.catalog(item)
        self.audit(item, "item.created")
        return item_view(item), 201, f'"{item.version}"'

    def detail(self, item):
        rows = self.session.scalars(
            select(Question).where(Question.item_id == item.id).order_by(Question.position)
        )
        return {
            **item_view(item),
            "verification_questions": [
                {
                    "id": str(row.id),
                    "prompt": row.prompt,
                    "reference_answer": row.reference_answer,
                    "version": row.version,
                }
                for row in rows
            ],
        }

    def change(self, item_id, body, version):
        item = self.get(item_id, lock=True)
        expected_version(version, item.version)
        self.mutable(item)
        data = body.model_dump(exclude_unset=True, exclude={"verification_questions"})
        for key, value in data.items():
            setattr(item, key, value)
        item.item_version += 1
        item.version += 1
        item.processing_correlation_id = None
        if item.active_upload_id:
            self.session.get(Photo, item.active_upload_id).status = "superseded"
            item.active_upload_id = None
        if body.verification_questions is not None:
            self.session.execute(delete(Question).where(Question.item_id == item.id))
            self.questions(item, body.verification_questions)
        else:
            for question in self.session.scalars(
                select(Question).where(Question.item_id == item.id)
            ):
                question.version = item.item_version
        if item.photo_id:
            self.start_processing(item)
        else:
            item.state = "awaiting_upload"
        self.catalog(item)
        self.audit(item, "item.changed")
        return item_view(item), 200, f'"{item.version}"'

    def withdraw(self, item_id, version):
        item = self.get(item_id, lock=True)
        expected_version(version, item.version)
        self.mutable(item)
        item.state, item.processing_correlation_id = "withdrawn", None
        item.version += 1
        self.catalog(item)
        self.audit(item, "item.withdrawn")
        return item_view(item), 200, f'"{item.version}"'

    def upload(self, item_id, body, version, bucket):
        item = self.get(item_id, lock=True)
        expected_version(version, item.version)
        self.mutable(item)
        if item.photo_id or item.active_upload_id:
            item.item_version += 1
        if item.active_upload_id:
            self.session.get(Photo, item.active_upload_id).status = "superseded"
        upload_id = uuid4()
        photo = Photo(
            id=upload_id,
            tenant_id=item.tenant_id,
            item_id=item.id,
            item_version=item.item_version,
            bucket=bucket,
            content_type=body.content_type,
            size=body.size,
            staging_key=f"staging/{item.tenant_id}/{item.id}/{upload_id}",
            sealed_key=f"sealed/{item.tenant_id}/{item.id}/{item.item_version}/{upload_id}",
            expires_at=datetime.now(UTC) + timedelta(minutes=10),
        )
        self.session.add(photo)
        item.active_upload_id, item.photo_id = upload_id, None
        item.state, item.processing_correlation_id = "awaiting_upload", None
        item.version += 1
        self.session.flush()
        self.catalog(item)
        self.audit(item, "photo.upload_requested")
        return (
            {
                "upload_id": str(upload_id),
                "item_id": str(item.id),
                "item_version": item.item_version,
                "version": item.version,
                "expires_at": photo.expires_at.isoformat(),
            },
            201,
            f'"{item.version}"',
        )

    def confirm(self, item_id, upload_id):
        item = self.get(item_id, lock=True)
        photo = self.session.get(Photo, upload_id)
        if photo is None or photo.item_id != item.id:
            fail(404, "upload_not_found", "Upload is not visible")
        existing = self.session.scalar(
            select(WorkJob).where(WorkJob.kind == "photo", WorkJob.resource_id == upload_id)
        )
        if existing:
            return {"operation_id": str(existing.id), "item_id": str(item.id)}, 202, None
        self.mutable(item)
        if item.active_upload_id != photo.id or photo.status != "pending":
            fail(409, "upload_superseded", "Upload is no longer current")
        if photo.expires_at <= datetime.now(UTC):
            fail(409, "upload_expired", "Create a new upload")
        job = WorkJob(tenant_id=item.tenant_id, kind="photo", resource_id=photo.id)
        self.session.add(job)
        self.session.flush()
        self.audit(item, "photo.confirm_requested")
        return {"operation_id": str(job.id), "item_id": str(item.id)}, 202, None

    def retry_embedding(self, item_id, version):
        item = self.get(item_id, lock=True)
        expected_version(version, item.version)
        if item.state != "processing_failed":
            fail(409, "item_not_failed", "Only failed processing can be retried")
        item.version += 1
        self.start_processing(item)
        self.catalog(item)
        self.audit(item, "item.embedding_retried")
        return (
            {**item_view(item), "processing_correlation_id": str(item.processing_correlation_id)},
            202,
            f'"{item.version}"',
        )

    def reservation(self, item_id, claim_id):
        row = self.session.get(Reservation, claim_id)
        if row is not None and row.item_id != item_id:
            fail(409, "claim_item_mismatch", "Claim is already bound to another item")
        return row

    def reservation_view(self, item, row):
        return {
            "claim_id": str(row.claim_id),
            "item_id": str(item.id),
            "item_version": row.item_version,
            "state": row.state,
            "handover_id": str(row.handover_id) if row.handover_id else None,
            "mode": row.mode,
            "version": item.version,
        }

    def reserve(self, item_id, body):
        # Global claim lock serializes claim reuse across different item rows.
        self.session.execute(
            text("SELECT pg_advisory_xact_lock(:key)"),
            {"key": int.from_bytes(body.claim_id.bytes[:8], signed=True)},
        )
        item = self.get(item_id, lock=True)
        row = self.reservation(item_id, body.claim_id)
        if row:
            if row.state == "held" and row.item_version == body.item_version:
                return self.reservation_view(item, row), 200, f'"{item.version}"'
            fail(409, "reservation_closed", "This claim cannot reserve the item")
        if item.state != "available" or item.item_version != body.item_version:
            fail(
                409,
                "item_not_available",
                "Item state or content version does not allow reservation",
            )
        row = Reservation(
            claim_id=body.claim_id,
            tenant_id=item.tenant_id,
            item_id=item.id,
            item_version=item.item_version,
            state="held",
        )
        self.session.add(row)
        item.state, item.reserved_claim_id = "reserved", body.claim_id
        item.version += 1
        self.session.add(
            WorkJob(
                tenant_id=item.tenant_id,
                kind="reservation_watch",
                resource_id=body.claim_id,
                available_at=datetime.now(UTC) + timedelta(minutes=5),
            )
        )
        self.catalog(item)
        self.audit(item, "item.reserved")
        return self.reservation_view(item, row), 200, f'"{item.version}"'

    def release(self, item_id, claim_id):
        self.session.execute(
            text("SELECT pg_advisory_xact_lock(:key)"),
            {"key": int.from_bytes(claim_id.bytes[:8], signed=True)},
        )
        item = self.get(item_id, lock=True)
        row = self.reservation(item_id, claim_id)
        if row and row.state == "committed":
            fail(409, "handover_committed", "A committed handover cannot be released")
        if row is None:
            row = Reservation(
                claim_id=claim_id,
                tenant_id=item.tenant_id,
                item_id=item.id,
                item_version=item.item_version,
                state="released",
            )
            self.session.add(row)
        elif row.state == "held":
            if item.reserved_claim_id != claim_id or item.state != "reserved":
                fail(409, "reservation_inconsistent", "Reservation requires manual recovery")
            row.state = "released"
            item.state, item.reserved_claim_id = "available", None
            item.version += 1
            self.catalog(item)
        self.audit(item, "reservation.released")
        return self.reservation_view(item, row), 200, f'"{item.version}"'

    def handover(self, item_id, claim_id, body):
        self.session.execute(
            text("SELECT pg_advisory_xact_lock(:key)"),
            {"key": int.from_bytes(claim_id.bytes[:8], signed=True)},
        )
        item = self.get(item_id, lock=True)
        row = self.reservation(item_id, claim_id)
        if (
            row
            and row.state == "committed"
            and row.handover_id == body.handover_id
            and row.mode == body.mode
        ):
            return self.reservation_view(item, row), 200, f'"{item.version}"'
        if row is None or row.state != "held" or item.reserved_claim_id != claim_id:
            fail(409, "reservation_not_held", "An active reservation is required")
        row.state, row.handover_id, row.mode = "committed", body.handover_id, body.mode
        item.state = "handed_over"
        item.version += 1
        self.catalog(item)
        self.audit(item, "item.handed_over")
        return self.reservation_view(item, row), 200, f'"{item.version}"'
