"""Human clients and machine clients have separate authorization paths."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from fastapi.responses import JSONResponse
from lostlink_common.auth import Principal, get_principal
from lostlink_common.errors import error_responses
from sqlalchemy import select

from venue_app.errors import VenueError
from venue_app.models import IdentityJob, Membership, Venue
from venue_app.schemas import (
    IdentityResolve,
    IdentityView,
    MemberChange,
    MemberCheck,
    MembershipCheckView,
    MembersPage,
    MemberView,
    NewMember,
    NewVenue,
    OnboardingView,
    OperationView,
    VenueChange,
    VenuesPage,
    VenueView,
)
from venue_app.service import VenueService, idempotent, member_view, resolve_identity, venue_view

router = APIRouter(responses=error_responses(401, 403, 422, 500, 503))
Actor = Annotated[Principal, Depends(get_principal)]
Key = Annotated[str | None, Header(alias="Idempotency-Key")]
Version = Annotated[str | None, Header(alias="If-Match")]


def authorize(request, principal, mode):
    settings = request.app.state.settings
    if mode == "platform":
        allowed = (
            principal.client_id in settings.platform_client_ids
            and "platform_admin" in principal.roles
        )
    elif mode == "employee":
        allowed = (
            principal.client_id in settings.user_client_ids
            and principal.tenant_id is not None
            and bool(principal.roles & {"venue_admin", "venue_staff"})
        )
    else:
        clients = (
            settings.member_check_client_ids
            if mode == "members.check"
            else settings.identity_resolve_client_ids
        )
        allowed = principal.client_id in clients and f"lostlink/{mode}" in principal.scopes
    if not allowed:
        raise VenueError(403, "forbidden", "Client and identity do not allow this operation")


def database(request, mode="employee"):
    db = getattr(
        request.app.state,
        {"employee": "database", "platform": "platform_database", "identity": "identity_database"}[
            mode
        ],
    )
    if db is None:
        raise VenueError(503, "unavailable", "Required database access mode is not configured")
    return db


def service(session, principal, request):
    return VenueService(session, principal.subject, getattr(request.state, "request_id", None))


def result(value):
    body, status, etag = value
    return JSONResponse(body, status_code=status, headers={"ETag": etag} if etag else {})


@router.post(
    "/v1/admin/venues",
    status_code=201,
    response_model=OnboardingView,
    tags=["Onboarding"],
    responses=error_responses(409, 428),
)
def create_venue(body: NewVenue, request: Request, principal: Actor, key: Key = None):
    authorize(request, principal, "platform")
    with database(request, "platform").context(subject=principal.subject) as session:
        value = idempotent(
            session,
            principal,
            "POST /v1/admin/venues",
            key,
            body.model_dump(),
            None,
            lambda: service(session, principal, request).create_venue(body),
        )
    return result(value)


@router.get("/v1/admin/venues", response_model=VenuesPage, tags=["Onboarding"])
def list_venues(
    request: Request,
    principal: Actor,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0, le=10000)] = 0,
):
    authorize(request, principal, "platform")
    with database(request, "platform").context(subject=principal.subject) as session:
        rows = list(
            session.scalars(
                select(Venue).order_by(Venue.created_at, Venue.id).limit(limit + 1).offset(offset)
            )
        )
        return {
            "items": [venue_view(row) for row in rows[:limit]],
            "limit": limit,
            "offset": offset,
            "has_more": len(rows) > limit,
        }


@router.get(
    "/v1/admin/venues/{tenant_id}",
    response_model=VenueView,
    tags=["Onboarding"],
    responses=error_responses(404),
)
def admin_venue(tenant_id: UUID, request: Request, response: Response, principal: Actor):
    authorize(request, principal, "platform")
    with database(request, "platform").context(subject=principal.subject) as session:
        venue = service(session, principal, request).venue(tenant_id)
        response.headers["ETag"] = f'"{venue.version}"'
        return venue_view(venue)


@router.get(
    "/v1/venues/current", response_model=VenueView, tags=["Venue"], responses=error_responses(404)
)
def current(request: Request, response: Response, principal: Actor):
    authorize(request, principal, "employee")
    with database(request).context(
        subject=principal.subject, tenant_id=principal.tenant_id
    ) as session:
        venue = service(session, principal, request).employee(principal.tenant_id, principal.roles)
        response.headers["ETag"] = f'"{venue.version}"'
        return venue_view(venue)


@router.get(
    "/v1/venues/current/members",
    response_model=MembersPage,
    response_model_exclude_none=True,
    tags=["Members"],
    responses=error_responses(404),
)
def members(
    request: Request,
    principal: Actor,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0, le=10000)] = 0,
):
    authorize(request, principal, "employee")
    with database(request).context(
        subject=principal.subject, tenant_id=principal.tenant_id
    ) as session:
        service(session, principal, request).employee(
            principal.tenant_id, principal.roles, admin=True
        )
        rows = list(
            session.scalars(
                select(Membership)
                .order_by(Membership.created_at, Membership.id)
                .limit(limit + 1)
                .offset(offset)
            )
        )
        return {
            "items": [member_view(row) for row in rows[:limit]],
            "limit": limit,
            "offset": offset,
            "has_more": len(rows) > limit,
        }


@router.post(
    "/v1/venues/current/members",
    status_code=201,
    response_model=MemberView,
    tags=["Members"],
    responses=error_responses(404, 409, 428),
)
def add_member(body: NewMember, request: Request, principal: Actor, key: Key = None):
    authorize(request, principal, "employee")
    with database(request).context(
        subject=principal.subject, tenant_id=principal.tenant_id
    ) as session:
        domain = service(session, principal, request)
        domain.employee(principal.tenant_id, principal.roles, admin=True, write=True)
        value = idempotent(
            session,
            principal,
            "POST /v1/venues/current/members",
            key,
            body.model_dump(),
            None,
            lambda: domain.add_member(principal.tenant_id, principal.roles, body),
        )
    return result(value)


@router.patch(
    "/v1/venues/current/members/{member_id}",
    response_model=MemberView,
    tags=["Members"],
    responses=error_responses(404, 409, 412, 428),
)
def change_member(
    member_id: UUID,
    body: MemberChange,
    request: Request,
    principal: Actor,
    key: Key = None,
    version: Version = None,
):
    authorize(request, principal, "employee")
    with database(request).context(
        subject=principal.subject, tenant_id=principal.tenant_id
    ) as session:
        domain = service(session, principal, request)
        domain.employee(principal.tenant_id, principal.roles, admin=True, write=True)
        value = idempotent(
            session,
            principal,
            f"PATCH /v1/venues/current/members/{member_id}",
            key,
            body.model_dump(exclude_unset=True),
            version,
            lambda: domain.change_member(
                principal.tenant_id, principal.roles, member_id, body, version
            ),
        )
    return result(value)


@router.patch(
    "/v1/admin/venues/{tenant_id}",
    response_model=VenueView,
    tags=["Onboarding"],
    responses=error_responses(404, 409, 412, 428),
)
def change_venue(
    tenant_id: UUID,
    body: VenueChange,
    request: Request,
    principal: Actor,
    key: Key = None,
    version: Version = None,
):
    authorize(request, principal, "platform")
    with database(request, "platform").context(subject=principal.subject) as session:
        value = idempotent(
            session,
            principal,
            f"PATCH /v1/admin/venues/{tenant_id}",
            key,
            body.model_dump(),
            version,
            lambda: service(session, principal, request).change_venue(tenant_id, body, version),
        )
    return result(value)


@router.get(
    "/v1/admin/identity-sync/{operation_id}",
    response_model=OperationView,
    tags=["Identity"],
    responses=error_responses(404),
)
def identity_sync(operation_id: UUID, request: Request, principal: Actor):
    mode = (
        "platform"
        if principal.client_id in request.app.state.settings.platform_client_ids
        else "employee"
    )
    authorize(request, principal, mode)
    with database(request, mode).context(
        subject=principal.subject, tenant_id=principal.tenant_id
    ) as session:
        if mode == "employee":
            service(session, principal, request).employee(
                principal.tenant_id, principal.roles, admin=True
            )
        job = session.get(IdentityJob, operation_id)
        if job is None:
            raise VenueError(404, "operation_not_found", "Operation is not visible")
        return {
            "id": str(job.id),
            "state": job.state,
            "attempts": job.attempts,
            "membership_version": job.membership_version,
            "last_error_code": job.last_error_code,
        }


@router.post("/internal/v1/members/check", response_model=MembershipCheckView, tags=["Internal"])
def check_member(body: MemberCheck, request: Request, principal: Actor):
    authorize(request, principal, "members.check")
    with database(request).context(subject=principal.subject, tenant_id=body.tenant_id) as session:
        member = session.scalar(select(Membership).where(Membership.user_sub == body.subject))
        venue = session.get(Venue, body.tenant_id)
        active = bool(
            venue
            and venue.status == "active"
            and member
            and member.status == "active"
            and member.role in body.allowed_roles
        )
        return {
            "active": active,
            "effective_role": member.role if member else None,
            "membership_version": member.version if member else None,
            "venue_status": venue.status if venue else None,
        }


@router.post("/internal/v1/identity/resolve", response_model=IdentityView, tags=["Internal"])
def identity(body: IdentityResolve, request: Request, principal: Actor):
    authorize(request, principal, "identity.resolve")
    with database(request, "identity").context(subject=body.subject) as session:
        return resolve_identity(session, body.subject)
