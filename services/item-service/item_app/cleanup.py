"""Grace-period collection in bounded pages; confirmed sealed objects are retained."""

from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select

from item_app.models import AuditEntry, Item, Photo, WorkJob


def cleanup(database, storage, bucket, *, now=None, grace_hours=24, page_size=100, stopped=None):
    if grace_hours < 24:
        raise ValueError("Photo cleanup requires at least 24 hours of grace")
    if not 1 <= page_size <= 1000:
        raise ValueError("Cleanup pages must contain 1-1000 objects")
    cutoff, deleted = (now or datetime.now(UTC)) - timedelta(hours=grace_hours), 0
    client = storage.client
    for prefix in ["staging/", "sealed/"]:
        pages = client.get_paginator("list_objects_v2").paginate(
            Bucket=bucket,
            Prefix=prefix,
            PaginationConfig={"PageSize": page_size},
        )
        for page in pages:
            for obj in page.get("Contents", []):
                if stopped is not None and stopped.is_set():
                    return deleted
                if obj["LastModified"] >= cutoff:
                    continue
                parts = obj["Key"].split("/")
                try:
                    if len(parts) != (4 if prefix == "staging/" else 5):
                        continue
                    tenant, item_id, photo_id = UUID(parts[1]), UUID(parts[2]), UUID(parts[-1])
                except ValueError:
                    continue
                allowed = False
                with database.context(subject="photo-cleanup", tenant_id=tenant) as session:
                    item = session.scalar(select(Item).where(Item.id == item_id).with_for_update())
                    photo = session.get(Photo, photo_id)
                    expected = (
                        photo.staging_key
                        if photo and prefix == "staging/"
                        else photo.sealed_key
                        if photo
                        else None
                    )
                    if photo and (
                        photo.item_id != item_id or photo.bucket != bucket or expected != obj["Key"]
                    ):
                        continue
                    if photo is None:
                        allowed = True
                    elif prefix == "staging/" and photo.status == "confirmed":
                        allowed = True
                    elif photo.expires_at < cutoff and not (item and item.photo_id == photo.id):
                        live = session.scalar(
                            select(WorkJob.id)
                            .where(
                                WorkJob.kind == "photo",
                                WorkJob.resource_id == photo.id,
                                WorkJob.state.in_(["pending", "leased"]),
                            )
                            .limit(1)
                        )
                        if live is None and photo.status != "confirmed":
                            photo.status = "purging"
                            allowed = True
                    if allowed:
                        session.add(
                            AuditEntry(
                                actor_sub="photo-cleanup",
                                tenant_id=tenant,
                                action="photo.cleanup_scheduled",
                                resource_id=photo_id,
                            )
                        )
                if allowed:
                    # Purging prevents a concurrent manual retry from confirming this object.
                    client.delete_object(Bucket=bucket, Key=obj["Key"])
                    deleted += 1
    return deleted
