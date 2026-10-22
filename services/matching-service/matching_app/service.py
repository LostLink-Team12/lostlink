"""Cosine retrieval and owner operations. Network calls run outside SQL transactions."""

from uuid import uuid4

from lostlink_common.events import DomainEvent
from sqlalchemy import Float, select, text, update
from sqlalchemy.orm import defer

from matching_app.embedding import vector_literal
from matching_app.errors import fail
from matching_app.models import AuditEntry, Catalog, ItemVector, Match, OutboxEvent, Report, WorkJob
from matching_app.schemas import ReportView
from matching_app.transactions import expected_version


def report_view(row):
    return ReportView.model_validate(row, from_attributes=True).model_dump(mode="json")


def emit(session, event, owner_id=None):
    session.add(
        OutboxEvent(id=event.event_id, owner_id=owner_id, envelope=event.model_dump(mode="json"))
    )


def report(session, report_id, *, lock=False):
    query = select(Report).where(Report.id == report_id)
    row = session.scalar(query.with_for_update() if lock else query)
    if row is None:
        fail(404, "report_not_found", "Report is not visible")
    return row


def create_report(session, owner, body, vector, model_version):
    row = Report(
        id=uuid4(),
        owner_id=owner,
        description=body.description,
        category=body.category,
        venue_id=body.venue_id,
        found_from=body.found_from,
        found_until=body.found_until,
        has_photo=body.photo_base64 is not None,
        embedding=vector,
        model_version=model_version,
    )
    session.add(row)
    session.flush()
    event = DomainEvent(
        event_type="lost_report.created",
        source="matching-service",
        data={"report_id": str(row.id), "owner_id": owner},
    )
    emit(session, event, owner)
    session.add(
        WorkJob(owner_id=owner, kind="report_match", resource_id=row.id, trigger_id=event.event_id)
    )
    session.add(AuditEntry(actor_sub=owner, resource_id=row.id, action="report.created"))
    return report_view(row), 201, f'"{row.version}"'


def close_report(session, owner, row, version):
    expected_version(version, row.version)
    if row.state != "closed":
        row.state, row.version = "closed", row.version + 1
        session.execute(update(Match).where(Match.report_id == row.id).values(active=False))
        session.add(AuditEntry(actor_sub=owner, resource_id=row.id, action="report.closed"))
    session.flush()
    return report_view(row), 200, f'"{row.version}"'


def filters_match(item, filters):
    return (
        (filters.category is None or item.category == filters.category)
        and (filters.venue_id is None or item.tenant_id == filters.venue_id)
        and (filters.found_from is None or item.found_at >= filters.found_from)
        and (filters.found_until is None or item.found_at <= filters.found_until)
    )


def candidates(session, vector, model_version, filters, limit, min_score):
    # Ordering by the raw distance operator makes the HNSW cosine index usable.
    distance = ItemVector.embedding.op("<=>", return_type=Float())(
        # Explicit cast avoids psycopg array adaptation and preserves vector dimensions.
        text("CAST(:query_vector AS vector(512))")
    )
    query = (
        select(Catalog, ItemVector, (1 - distance).label("score"))
        # Scoring stays in PostgreSQL. Callers use freshness/version metadata,
        # not the 100 x 512 dense values previously sent for every search.
        .options(defer(ItemVector.embedding, raiseload=True))
        .join(ItemVector, ItemVector.item_id == Catalog.item_id)
        .where(
            Catalog.state == "available",
            Catalog.item_version == ItemVector.item_version,
            Catalog.tenant_id == ItemVector.tenant_id,
            ItemVector.model_version == model_version,
        )
    )
    if filters.category:
        query = query.where(Catalog.category == filters.category)
    if filters.venue_id:
        query = query.where(Catalog.tenant_id == filters.venue_id)
    if filters.found_from:
        query = query.where(Catalog.found_at >= filters.found_from)
    if filters.found_until:
        query = query.where(Catalog.found_at <= filters.found_until)
    # Higher ef_search improves filtered recall on the pgvector 0.7 local image too.
    session.execute(text("SET LOCAL hnsw.ef_search = 200"))
    rows = session.execute(
        query.order_by(distance, Catalog.item_id).limit(limit),
        {"query_vector": vector_literal(vector)},
    ).all()
    return [
        (item, embedding, max(-1.0, min(1.0, float(score))))
        for item, embedding, score in rows
        if float(score) >= min_score
    ]


def is_current(context, item, embedding, filters=None):
    return (
        context is not None
        and context.state == "available"
        and context.tenant_id == item.tenant_id
        and context.item_version == embedding.item_version
        and context.processing_correlation_id == embedding.correlation_id
        and (filters is None or filters_match(context, filters))
    )


def hit(context, score):
    return {
        "item_id": str(context.item_id),
        "tenant_id": str(context.tenant_id),
        "item_version": context.item_version,
        "title": context.title,
        "description": context.description,
        "category": context.category,
        "found_at": context.found_at.isoformat(),
        "public_location_hint": context.public_location_hint,
        "photo_url": context.photo_url,
        "score": score,
    }


def save_match(session, row, item, embedding, score):
    # All callers lock the report first. A unique constraint is a second line of defense.
    existing = session.scalar(
        select(Match).where(Match.report_id == row.id, Match.item_id == item.item_id)
    )
    if existing:
        existing.item_version, existing.correlation_id = (
            embedding.item_version,
            embedding.correlation_id,
        )
        existing.score, existing.active = score, True
        return
    match = Match(
        id=uuid4(),
        report_id=row.id,
        owner_id=row.owner_id,
        item_id=item.item_id,
        tenant_id=item.tenant_id,
        item_version=embedding.item_version,
        correlation_id=embedding.correlation_id,
        score=score,
    )
    session.add(match)
    event = DomainEvent(
        event_type="match.found",
        source="matching-service",
        tenant_id=item.tenant_id,
        data={
            "match_id": str(match.id),
            "report_id": str(row.id),
            "owner_id": row.owner_id,
            "item_id": str(item.item_id),
            "score": score,
        },
    )
    emit(session, event, row.owner_id)


def catalog_upsert(session, event):
    from sqlalchemy.dialects.postgresql import insert

    data = event.data
    values = {
        k: data[k]
        for k in [
            "item_id",
            "item_version",
            "revision",
            "state",
            "title",
            "description",
            "category",
            "found_at",
        ]
    }
    values["tenant_id"] = event.tenant_id
    if str(event.tenant_id) != str(data["venue_id"]):
        raise ValueError("Catalog venue must agree with the event tenant")
    statement = insert(Catalog).values(**values)
    session.execute(
        statement.on_conflict_do_update(
            index_elements=[Catalog.item_id],
            set_={k: getattr(statement.excluded, k) for k in values if k != "item_id"},
            where=(statement.excluded.revision > Catalog.revision)
            & (statement.excluded.tenant_id == Catalog.tenant_id),
        )
    )
    return session.scalar(
        select(Catalog)
        .where(Catalog.item_id == data["item_id"])
        .execution_options(populate_existing=True)
    )
