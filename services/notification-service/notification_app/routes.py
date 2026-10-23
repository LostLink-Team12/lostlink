"""Owner notification status and audited operator recovery; no arbitrary-recipient sending."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import JSONResponse
from lostlink_common.auth import Principal, get_principal
from lostlink_common.errors import error_responses
from sqlalchemy import func, select

from notification_app import schemas
from notification_app.errors import fail
from notification_app.models import (
    STATES,
    AuditEntry,
    Notification,
    NotificationJob,
)
from notification_app.transactions import idempotent

router = APIRouter(responses=error_responses(401, 403, 422, 500, 503))
Actor = Annotated[Principal, Depends(get_principal)]
Key = Annotated[str | None, Header(alias="Idempotency-Key")]
Limit = Annotated[int, Query(ge=1, le=100)]
Offset = Annotated[int, Query(ge=0, le=10000)]
RETRYABLE = {"failed", "delivery_unknown"}


def owner(request: Request, principal: Actor):
    if principal.client_id not in request.app.state.settings.user_client_ids:
        fail(403, "forbidden", "A LostLink owner application is required")
    if "owner" not in principal.roles:
        fail(403, "forbidden", "The owner role is required")
    return principal


def operator(request: Request, principal: Actor):
    if (
        principal.client_id not in request.app.state.settings.operator_client_ids
        or "platform_admin" not in principal.roles
    ):
        fail(403, "forbidden", "A platform operator is required")
    return principal


Owner = Annotated[Principal, Depends(owner)]
Operator = Annotated[Principal, Depends(operator)]


def database(request, name):
    db = getattr(request.app.state, name)
    if db is None:
        fail(503, "unavailable", "Required database access mode is not configured")
    return db


def iso(value):
    return value.isoformat() if value else None


def view(row):
    return {
        "id": str(row.id),
        "match_id": str(row.match_id),
        "report_id": str(row.report_id),
        "item_id": str(row.item_id),
        "channel": row.channel,
        "state": row.state,
        "reason_code": row.reason_code,
        "created_at": iso(row.created_at),
        "sent_at": iso(row.sent_at),
    }


def admin_view(row, job):
    return {
        **view(row),
        "tenant_id": str(row.tenant_id),
        "template_version": row.template_version,
        "attempts": job.attempts,
        "job_state": job.state,
        "last_error_code": job.last_error_code,
    }


@router.get("/v1/notifications", response_model=schemas.NotificationsPage, tags=["Notifications"])
def list_notifications(request: Request, principal: Owner, limit: Limit = 20, offset: Offset = 0):
    with database(request, "database").actor_session(principal, venue_scope=False) as session:
        rows = list(
            session.scalars(
                select(Notification)
                .where(Notification.owner_sub == principal.subject)
                .order_by(Notification.created_at.desc(), Notification.id)
                .limit(limit + 1)
                .offset(offset)
            )
        )
    return {
        "items": [view(row) for row in rows[:limit]],
        "limit": limit,
        "offset": offset,
        "has_more": len(rows) > limit,
    }


@router.get(
    "/v1/notifications/{notification_id}",
    response_model=schemas.NotificationView,
    tags=["Notifications"],
    responses=error_responses(404),
)
def notification_detail(notification_id: UUID, request: Request, principal: Owner):
    with database(request, "database").actor_session(principal, venue_scope=False) as session:
        row = session.scalar(
            select(Notification).where(
                Notification.id == notification_id, Notification.owner_sub == principal.subject
            )
        )
        if row is None:
            fail(404, "notification_not_found", "Notification is not visible")
        return view(row)


@router.get(
    "/v1/admin/notifications", response_model=schemas.AdminNotificationsPage, tags=["Operations"]
)
def admin_notifications(
    request: Request,
    principal: Operator,
    state: schemas.State | None = None,
    limit: Limit = 20,
    offset: Offset = 0,
):
    with database(request, "admin_database").scoped_session(subject=principal.subject) as session:
        query = select(Notification, NotificationJob).join(
            NotificationJob, NotificationJob.notification_id == Notification.id
        )
        if state:
            query = query.where(Notification.state == state)
        rows = list(
            session.execute(
                query.order_by(Notification.created_at.desc(), Notification.id)
                .limit(limit + 1)
                .offset(offset)
            )
        )
    return {
        "items": [admin_view(row, job) for row, job in rows[:limit]],
        "limit": limit,
        "offset": offset,
        "has_more": len(rows) > limit,
    }


@router.get(
    "/v1/admin/notification-stats", response_model=schemas.NotificationStats, tags=["Operations"]
)
def notification_stats(request: Request, principal: Operator):
    with database(request, "admin_database").scoped_session(subject=principal.subject) as session:
        counts = dict(
            session.execute(
                select(Notification.state, func.count()).group_by(Notification.state)
            ).all()
        )
        oldest = session.scalar(
            select(func.extract("epoch", func.now() - func.min(Notification.created_at))).where(
                Notification.state.in_(["pending", "sending", "retry_wait"])
            )
        )
    return {
        "counts": {state: counts.get(state, 0) for state in STATES},
        "oldest_pending_seconds": float(oldest) if oldest is not None else None,
    }


@router.post(
    "/v1/admin/notifications/{notification_id}/retry",
    response_model=schemas.AdminNotificationView,
    tags=["Operations"],
    responses=error_responses(404, 409, 428),
)
def retry_notification(
    notification_id: UUID,
    body: schemas.RetryRequest,
    request: Request,
    principal: Operator,
    key: Key = None,
):
    def run():
        row = session.scalar(
            select(Notification).where(Notification.id == notification_id).with_for_update()
        )
        if row is None:
            fail(404, "notification_not_found", "Notification is not visible")
        if row.state not in RETRYABLE:
            fail(409, "notification_not_retryable", "Only failed or unknown deliveries retry")
        if row.state == "delivery_unknown" and not body.acknowledge_duplicate_risk:
            fail(
                409,
                "duplicate_risk_not_acknowledged",
                "The previous email may have been delivered; acknowledge the duplicate risk",
            )
        job = session.scalar(
            select(NotificationJob)
            .where(NotificationJob.notification_id == row.id)
            .with_for_update()
        )
        row.state, row.reason_code = "pending", None
        job.state, job.attempts, job.last_error_code = "pending", 0, None
        job.lease_token = job.lease_until = None
        job.available_at = session.scalar(select(func.now()))
        session.add(
            AuditEntry(
                actor_sub=principal.subject,
                action="notification.retry",
                resource_id=row.id,
                reason_code=body.reason_code,
                request_id=getattr(request.state, "request_id", None),
            )
        )
        session.flush()
        return admin_view(row, job), 200

    with database(request, "admin_database").scoped_session(subject=principal.subject) as session:
        response, status = idempotent(
            session,
            principal,
            f"POST /v1/admin/notifications/{notification_id}/retry",
            key,
            body.model_dump(),
            run,
        )
    return JSONResponse(response, status_code=status)
