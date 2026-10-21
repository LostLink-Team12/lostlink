"""Transactional membership rules and request idempotency."""

import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from uuid import UUID

from lostlink_common.events import DomainEvent
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from venue_app.errors import VenueError
from venue_app.models import (
    AuditEntry,
    IdempotencyRecord,
    IdentityJob,
    Membership,
    OutboxEvent,
    Venue,
)


def fail(status: int, code: str, message: str):
    raise VenueError(status, code, message)


def venue_view(row: Venue) -> dict:
    return {
        "id": str(row.id),
        "name": row.name,
        "slug": row.slug,
        "status": row.status,
        "version": row.version,
    }


def member_view(row: Membership) -> dict:
    return {
        "id": str(row.id),
        "user_sub": row.user_sub,
        "role": row.role,
        "status": row.status,
        "version": row.version,
    }


def expected_version(header: str | None, actual: int):
    if header is None:
        fail(428, "precondition_required", "If-Match is required")
    if not re.fullmatch(r'"[1-9][0-9]*"', header):
        fail(422, "invalid_etag", "Use a quoted integer ETag")
    if header != f'"{actual}"':
        fail(412, "version_mismatch", "Resource version has changed")


def idempotent(session, principal, operation, key, body, version, callback):
    if key is None:
        fail(428, "precondition_required", "Idempotency-Key is required")
    if not 8 <= len(key) <= 128 or any(not 32 <= ord(c) <= 126 for c in key):
        fail(422, "invalid_idempotency_key", "Use 8 to 128 printable ASCII characters")
    scope = hashlib.sha256(
        json.dumps(
            [principal.client_id, principal.subject, str(principal.tenant_id), operation, key],
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    digest = hashlib.sha256(
        json.dumps(
            {"body": body, "if_match": version}, sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()
    lock_id = int.from_bytes(bytes.fromhex(scope[:16]), signed=True)
    if not session.scalar(text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": lock_id}):
        fail(409, "operation_in_progress", "An identical operation is in progress")
    record = session.get(IdempotencyRecord, scope)
    now = datetime.now(UTC)
    if record and record.expires_at > now:
        if record.request_hash != digest:
            fail(409, "idempotency_conflict", "Key was used for a different request")
        return record.response, record.response_status, record.etag
    if record:
        session.delete(record)
        session.flush()
    response, status, etag = callback()
    session.add(
        IdempotencyRecord(
            scope=scope,
            actor_sub=principal.subject,
            request_hash=digest,
            response=response,
            response_status=status,
            etag=etag,
            expires_at=now + timedelta(hours=24),
        )
    )
    session.flush()
    return response, status, etag


class VenueService:
    def __init__(self, session: Session, subject: str, request_id: str | None = None):
        self.session, self.subject, self.request_id = session, subject, request_id

    def venue(self, tenant: UUID, *, lock=False) -> Venue:
        query = select(Venue).where(Venue.id == tenant)
        if lock:
            query = query.with_for_update()
        row = self.session.scalar(query.execution_options(populate_existing=True))
        if row is None:
            fail(404, "venue_not_found", "Venue is not visible")
        return row

    def employee(self, tenant, token_roles, *, admin=False, write=False, lock=False):
        venue = self.venue(tenant, lock=lock)
        member = self.session.scalar(
            select(Membership)
            .where(Membership.tenant_id == tenant, Membership.user_sub == self.subject)
            .execution_options(populate_existing=True)
        )
        roles = {"venue_admin"} if admin else {"venue_admin", "venue_staff"}
        if (
            member is None
            or member.status != "active"
            or member.role not in roles
            or member.role not in token_roles
        ):
            fail(403, "membership_inactive", "Current membership does not allow this operation")
        if write and venue.status != "active":
            fail(403, "venue_suspended", "Venue is suspended")
        return venue

    def audit(self, tenant, action, resource):
        self.session.add(
            AuditEntry(
                actor_sub=self.subject,
                tenant_id=tenant,
                action=action,
                resource_id=resource,
                request_id=self.request_id,
            )
        )

    def schedule_identity(self, venue, member) -> IdentityJob:
        job = IdentityJob(
            tenant_id=venue.id,
            membership_id=member.id,
            user_sub=member.user_sub,
            membership_version=member.version,
            venue_version=venue.version,
        )
        self.session.add(job)
        self.session.flush()
        return job

    def changed(self, venue, member):
        job = self.schedule_identity(venue, member)
        event = DomainEvent(
            event_type="venue.membership_changed",
            source="venue-service",
            tenant_id=venue.id,
            data={
                "membership_id": str(member.id),
                "user_sub": member.user_sub,
                "version": member.version,
                "status": member.status,
            },
        )
        self.session.add(
            OutboxEvent(
                id=event.event_id, tenant_id=venue.id, envelope=event.model_dump(mode="json")
            )
        )
        self.audit(venue.id, "membership.changed", member.id)
        return {
            **member_view(member),
            "identity_sync_status": "pending",
            "identity_sync_operation_id": str(job.id),
        }

    def create_venue(self, body):
        venue = Venue(name=body.name, slug=body.slug)
        self.session.add(venue)
        self.session.flush()
        member = Membership(tenant_id=venue.id, user_sub=body.initial_admin_sub, role="venue_admin")
        self.session.add(member)
        self.session.flush()
        result = self.changed(venue, member)
        self.audit(venue.id, "venue.created", venue.id)
        return (
            {
                "venue": venue_view(venue),
                "initial_admin": result,
                "identity_sync_status": "pending",
            },
            201,
            f'"{venue.version}"',
        )

    def add_member(self, tenant, roles, body):
        venue = self.employee(tenant, roles, admin=True, write=True, lock=True)
        member = Membership(tenant_id=tenant, user_sub=body.user_sub, role=body.role)
        self.session.add(member)
        self.session.flush()
        return self.changed(venue, member), 201, f'"{member.version}"'

    def change_member(self, tenant, roles, member_id, body, version):
        venue = self.employee(tenant, roles, admin=True, write=True, lock=True)
        member = self.session.get(Membership, member_id)
        if member is None or member.tenant_id != tenant:
            fail(404, "member_not_found", "Member is not visible")
        expected_version(version, member.version)
        role, status = body.role or member.role, body.status or member.status
        if (
            member.role == "venue_admin"
            and member.status == "active"
            and (role != "venue_admin" or status != "active")
        ):
            admins = self.session.scalar(
                select(func.count())
                .select_from(Membership)
                .where(
                    Membership.tenant_id == tenant,
                    Membership.role == "venue_admin",
                    Membership.status == "active",
                )
            )
            if admins <= 1:
                fail(409, "last_venue_admin", "At least one active administrator is required")
        if (member.role, member.status) == (role, status):
            return member_view(member), 200, f'"{member.version}"'
        member.role, member.status = role, status
        member.version += 1
        self.session.flush()
        return self.changed(venue, member), 200, f'"{member.version}"'

    def change_venue(self, tenant, body, version):
        venue = self.venue(tenant, lock=True)
        expected_version(version, venue.version)
        if venue.status != body.status:
            venue.status, venue.version = body.status, venue.version + 1
            venue.updated_at = datetime.now(UTC)
            for member in self.session.scalars(
                select(Membership).where(
                    Membership.tenant_id == tenant, Membership.status == "active"
                )
            ):
                self.schedule_identity(venue, member)
            self.audit(tenant, "venue.status_changed", tenant)
        return venue_view(venue), 200, f'"{venue.version}"'


def resolve_identity(session, subject):
    member = session.scalar(
        select(Membership).where(Membership.user_sub == subject, Membership.status == "active")
    )
    if member is None:
        return {"tenant_id": None, "role": None, "membership_version": None}
    venue = session.get(Venue, member.tenant_id)
    if venue is None or venue.status != "active":
        return {"tenant_id": None, "role": None, "membership_version": None}
    return {"tenant_id": str(venue.id), "role": member.role, "membership_version": member.version}
