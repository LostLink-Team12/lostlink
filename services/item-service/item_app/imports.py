"""Durable per-row import state; hidden answers never enter operational payloads."""

from sqlalchemy import select, text

from item_app.errors import fail
from item_app.models import ImportEntry, ImportJob, WorkJob
from item_app.transactions import digest


def job_view(row):
    return {
        key: str(row.id) if key == "id" else getattr(row, key)
        for key in ["id", "state", "total", "accepted", "failed", "batch_reference"]
    }


def submit(session, principal, body):
    manifest_hash = digest(body.model_dump(mode="json"))
    lock = int.from_bytes(
        bytes.fromhex(digest([str(principal.tenant_id), body.batch_reference])[:16]), signed=True
    )
    session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock})
    existing = session.scalar(
        select(ImportJob).where(
            ImportJob.tenant_id == principal.tenant_id,
            ImportJob.batch_reference == body.batch_reference,
        )
    )
    if existing:
        if existing.manifest_hash != manifest_hash:
            fail(409, "batch_reference_conflict", "Batch reference belongs to another manifest")
        return job_view(existing), 202, None
    job = ImportJob(
        tenant_id=principal.tenant_id,
        created_by=principal.subject,
        batch_reference=body.batch_reference,
        manifest_hash=manifest_hash,
        total=len(body.entries),
    )
    session.add(job)
    session.flush()
    for number, entry in enumerate(body.entries):
        row = ImportEntry(
            tenant_id=principal.tenant_id,
            job_id=job.id,
            line_number=number,
            client_reference=entry["client_reference"],
            payload={key: value for key, value in entry.items() if key != "client_reference"},
        )
        session.add(row)
        session.flush()
        session.add(WorkJob(tenant_id=principal.tenant_id, kind="import_entry", resource_id=row.id))
    return job_view(job), 202, None


def repair(session, job_id, body):
    job = session.scalar(select(ImportJob).where(ImportJob.id == job_id).with_for_update())
    if job is None:
        fail(404, "import_not_found", "Import is not visible")
    for change in body.entries:
        row = session.get(ImportEntry, change.entry_id)
        if row is None or row.job_id != job.id:
            fail(404, "import_entry_not_found", "Entry is not visible")
        if row.state != "failed":
            fail(409, "import_entry_not_failed", "Only failed rows can be repaired")
        row.payload = change.item.model_dump(mode="json")
        row.state, row.error_code, row.generation = "pending", None, row.generation + 1
        job.failed -= 1
        session.add(
            WorkJob(
                tenant_id=job.tenant_id,
                kind="import_entry",
                resource_id=row.id,
                generation=row.generation,
            )
        )
    job.state = "pending"
    return job_view(job), 202, None


def status(session, job_id, limit, offset):
    job = session.get(ImportJob, job_id)
    if job is None:
        fail(404, "import_not_found", "Import is not visible")
    rows = list(
        session.scalars(
            select(ImportEntry)
            .where(ImportEntry.job_id == job.id)
            .order_by(ImportEntry.line_number)
            .limit(limit + 1)
            .offset(offset)
        )
    )
    return {
        "job": job_view(job),
        "items": [
            {
                "id": str(row.id),
                "client_reference": row.client_reference,
                "state": row.state,
                "item_id": str(row.item_id) if row.item_id else None,
                "error_code": row.error_code,
            }
            for row in rows[:limit]
        ],
        "limit": limit,
        "offset": offset,
        "has_more": len(rows) > limit,
    }
