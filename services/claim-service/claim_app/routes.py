"""Owner and venue-staff APIs; each request uses one explicit access mode."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import JSONResponse
from lostlink_common.auth import Principal, get_principal
from lostlink_common.errors import error_responses
from lostlink_common.identity import (
    EmailNotVerified,
    IdentityDirectoryUnavailable,
    IdentityNotFound,
)
from sqlalchemy import select

from claim_app import schemas
from claim_app.clients import ItemRejected, ItemUnavailable
from claim_app.errors import fail
from claim_app.models import Claim, ClaimOperation
from claim_app.service import ClaimService, claim_view
from claim_app.transactions import idempotent, identity, stored

router = APIRouter(responses=error_responses(401, 403, 422, 500, 503))
Actor = Annotated[Principal, Depends(get_principal)]
Key = Annotated[str | None, Header(alias="Idempotency-Key")]
Version = Annotated[str | None, Header(alias="If-Match")]
Limit = Annotated[int, Query(ge=1, le=100)]
Offset = Annotated[int, Query(ge=0, le=10000)]
STAFF_ROLES = ("venue_admin", "venue_staff")


def user_client(request, principal):
    if principal.client_id not in request.app.state.settings.user_client_ids:
        fail(403, "forbidden", "A LostLink user application is required")


def owner(request: Request, principal: Actor):
    user_client(request, principal)
    if "owner" not in principal.roles:
        fail(403, "forbidden", "The owner role is required")
    return principal


def staff(request: Request, principal: Actor):
    user_client(request, principal)
    if principal.tenant_id is None or not principal.roles.intersection(STAFF_ROLES):
        fail(403, "forbidden", "Venue staff membership is required")
    client = request.app.state.membership_client
    if client is None:
        fail(503, "membership_unavailable", "Current membership is not configured")
    client.require_current(principal, *STAFF_ROLES)
    return principal


Owner = Annotated[Principal, Depends(owner)]
Staff = Annotated[Principal, Depends(staff)]


def dependency(request, name, code):
    value = getattr(request.app.state, name)
    if value is None:
        fail(503, code, "A required dependency is not configured")
    return value


def database(request):
    return dependency(request, "database", "unavailable")


def service(session, principal, request):
    state = request.app.state
    return ClaimService(
        session,
        principal.subject,
        getattr(request.state, "request_id", None),
        settings=state.settings,
        cipher=state.cipher,
    )


def owner_session(request, principal):
    return database(request).actor_session(principal, venue_scope=False)


def staff_session(request, principal):
    return database(request).actor_session(principal, venue_scope=True)


def result(value):
    body, status, etag = value
    return JSONResponse(body, status_code=status, headers={"ETag": etag} if etag else {})


def write(request, principal, opener, operation, key, body, version, callback):
    with opener(request, principal) as session:
        value = idempotent(
            session,
            principal,
            operation,
            key,
            body,
            version,
            lambda: callback(service(session, principal, request)),
        )
    return result(value)


def page(query, limit, offset):
    return query.order_by(Claim.created_at.desc(), Claim.id).limit(limit + 1).offset(offset)


def item_context(request, item_id):
    client = dependency(request, "item_client", "item_unavailable")
    try:
        return client.claim_context(item_id)
    except ItemRejected as exc:
        if exc.status == 404:
            fail(404, "item_not_found", "Item is not available for claims")
        fail(503, "item_unavailable", "Item could not be checked")
    except ItemUnavailable:
        fail(503, "item_unavailable", "Item could not be checked")


@router.get(
    "/v1/claim-questions",
    response_model=schemas.ClaimQuestions,
    tags=["Claims"],
    responses=error_responses(404, 409),
)
def questions(item_id: UUID, request: Request, principal: Owner):
    context = item_context(request, item_id)
    if context.state != "available":
        fail(409, "item_not_available", "This item cannot be claimed now")
    return {
        "item_id": str(context.item_id),
        "title": context.title,
        "state": context.state,
        "item_version": context.item_version,
        "questions": [question.model_dump(mode="json") for question in context.questions],
    }


@router.post(
    "/v1/claims",
    status_code=201,
    response_model=schemas.ClaimDetail,
    tags=["Claims"],
    responses=error_responses(404, 409, 428),
)
def create(body: schemas.NewClaim, request: Request, principal: Owner, key: Key = None):
    operation, payload = "POST /v1/claims", body.model_dump(mode="json")
    scope, request_hash = identity(principal, operation, key, payload, None)
    # Replay before external calls: a later Item state must not change a committed answer.
    with owner_session(request, principal) as session:
        previous = stored(session, scope, request_hash)
    if previous:
        return result(previous)
    dependency(request, "cipher", "encryption_unavailable")
    directory = dependency(request, "identity_directory", "identity_unavailable")
    context = item_context(request, body.item_id)
    try:
        email = directory.resolve_verified_email(principal.subject).email
    except (IdentityNotFound, EmailNotVerified):
        fail(409, "email_not_verified", "Verify your account email before claiming")
    except IdentityDirectoryUnavailable:
        fail(503, "identity_unavailable", "Account email could not be checked")
    return write(
        request,
        principal,
        owner_session,
        operation,
        key,
        payload,
        None,
        lambda svc: svc.create(principal.subject, body, context, email),
    )


@router.get("/v1/claims", response_model=schemas.ClaimsPage, tags=["Claims"])
def list_claims(request: Request, principal: Owner, limit: Limit = 20, offset: Offset = 0):
    with owner_session(request, principal) as session:
        rows = list(
            session.scalars(
                page(select(Claim).where(Claim.owner_sub == principal.subject), limit, offset)
            )
        )
        svc = service(session, principal, request)
        items = []
        for claim in rows[:limit]:
            operation = svc.active_operation(claim.id)
            items.append(claim_view(claim, operation.id if operation else None))
    return {"items": items, "limit": limit, "offset": offset, "has_more": len(rows) > limit}


@router.get(
    "/v1/claims/{claim_id}",
    response_model=schemas.ClaimDetail,
    tags=["Claims"],
    responses=error_responses(404),
)
def owner_detail(claim_id: UUID, request: Request, principal: Owner):
    with owner_session(request, principal) as session:
        svc = service(session, principal, request)
        claim = svc.get(claim_id, owner_sub=principal.subject)
        return JSONResponse(svc.detail(claim), headers={"ETag": f'"{claim.version}"'})


@router.get(
    "/v1/claims/{claim_id}/contact",
    response_model=schemas.ContactView,
    tags=["Claims"],
    responses=error_responses(404),
)
def owner_contact(claim_id: UUID, request: Request, principal: Owner):
    dependency(request, "cipher", "encryption_unavailable")
    with owner_session(request, principal) as session:
        svc = service(session, principal, request)
        return svc.contact(svc.get(claim_id, owner_sub=principal.subject), "contact.owner_viewed")


@router.post(
    "/v1/claims/{claim_id}/cancel",
    response_model=schemas.ClaimDetail,
    responses={**error_responses(404, 409, 412, 428), 202: {"model": schemas.AcceptedOperation}},
    tags=["Claims"],
)
def cancel(
    claim_id: UUID,
    body: schemas.Cancellation,
    request: Request,
    principal: Owner,
    key: Key = None,
    version: Version = None,
):
    return write(
        request,
        principal,
        owner_session,
        f"POST /v1/claims/{claim_id}/cancel",
        key,
        body.model_dump(),
        version,
        lambda svc: svc.cancel(principal.subject, claim_id, version, body.reason_code),
    )


@router.post(
    "/v1/claims/{claim_id}/handover-token",
    status_code=201,
    response_model=schemas.HandoverTokenView,
    tags=["Handover"],
    responses=error_responses(404, 409, 428),
)
def handover_token(claim_id: UUID, request: Request, principal: Owner, key: Key = None):
    dependency(request, "cipher", "encryption_unavailable")
    token_key = dependency(request, "token_key", "handover_unavailable")
    operation = f"POST /v1/claims/{claim_id}/handover-token"
    with owner_session(request, principal) as session:
        svc = service(session, principal, request)
        body, status, _ = idempotent(
            session,
            principal,
            operation,
            key,
            {},
            None,
            lambda: svc.issue_token(principal.subject, claim_id, token_key),
        )
        return JSONResponse(svc.token_payload(body), status_code=status)


@router.get("/v1/venue/claims", response_model=schemas.VenueClaimsPage, tags=["Venue"])
def venue_claims(
    request: Request,
    principal: Staff,
    state: schemas.ClaimState | None = None,
    limit: Limit = 20,
    offset: Offset = 0,
):
    with staff_session(request, principal) as session:
        query = select(Claim).where(Claim.tenant_id == principal.tenant_id)
        if state:
            query = query.where(Claim.state == state)
        rows = list(session.scalars(page(query, limit, offset)))
        svc = service(session, principal, request)
        items = []
        for claim in rows[:limit]:
            operation = svc.active_operation(claim.id)
            items.append(claim_view(claim, operation.id if operation else None, venue=True))
    return {"items": items, "limit": limit, "offset": offset, "has_more": len(rows) > limit}


@router.get(
    "/v1/venue/claims/{claim_id}",
    response_model=schemas.VenueClaimDetail,
    tags=["Venue"],
    responses=error_responses(404),
)
def venue_detail(claim_id: UUID, request: Request, principal: Staff):
    with staff_session(request, principal) as session:
        svc = service(session, principal, request)
        claim = svc.get(claim_id, tenant_id=principal.tenant_id)
        return JSONResponse(svc.detail(claim, venue=True), headers={"ETag": f'"{claim.version}"'})


@router.post(
    "/v1/venue/claims/{claim_id}/approve",
    status_code=202,
    response_model=schemas.AcceptedOperation,
    tags=["Venue"],
    responses=error_responses(404, 409, 412, 428),
)
def approve(
    claim_id: UUID, request: Request, principal: Staff, key: Key = None, version: Version = None
):
    return write(
        request,
        principal,
        staff_session,
        f"POST /v1/venue/claims/{claim_id}/approve",
        key,
        {},
        version,
        lambda svc: svc.approve(principal, claim_id, version),
    )


@router.post(
    "/v1/venue/claims/{claim_id}/reject",
    response_model=schemas.VenueClaimDetail,
    tags=["Venue"],
    responses=error_responses(404, 409, 412, 428),
)
def reject(
    claim_id: UUID,
    body: schemas.Rejection,
    request: Request,
    principal: Staff,
    key: Key = None,
    version: Version = None,
):
    return write(
        request,
        principal,
        staff_session,
        f"POST /v1/venue/claims/{claim_id}/reject",
        key,
        body.model_dump(),
        version,
        lambda svc: svc.reject(principal, claim_id, version, body.reason_code),
    )


@router.post(
    "/v1/venue/claims/{claim_id}/handover",
    status_code=202,
    response_model=schemas.AcceptedOperation,
    tags=["Handover"],
    responses=error_responses(404, 409, 412, 428),
)
def handover(
    claim_id: UUID,
    body: schemas.HandoverScan,
    request: Request,
    principal: Staff,
    key: Key = None,
    version: Version = None,
):
    token_key = dependency(request, "token_key", "handover_unavailable")
    return write(
        request,
        principal,
        staff_session,
        f"POST /v1/venue/claims/{claim_id}/handover",
        key,
        body.model_dump(),
        version,
        lambda svc: svc.scan(principal, claim_id, version, body.token, token_key),
    )


@router.get(
    "/v1/venue/claims/{claim_id}/contact",
    response_model=schemas.ContactView,
    tags=["Venue"],
    responses=error_responses(404),
)
def venue_contact(claim_id: UUID, request: Request, principal: Staff):
    dependency(request, "cipher", "encryption_unavailable")
    with staff_session(request, principal) as session:
        svc = service(session, principal, request)
        claim = svc.get(claim_id, tenant_id=principal.tenant_id)
        if claim.state not in {"approved", "handover_pending"}:
            fail(403, "contact_not_available", "Contact is shared only for a pending handover")
        return svc.contact(claim, "contact.venue_viewed")


def operation_view(row, claim):
    return {
        "id": str(row.id),
        "claim_id": str(row.claim_id),
        "kind": row.kind,
        "state": row.state,
        "claim_state": claim.state,
        "attempts": row.attempts,
        "result_code": row.result_code,
        "last_error_code": row.last_error_code,
        "created_at": row.created_at.isoformat(),
        "completed_at": row.completed_at.isoformat() if row.completed_at else None,
    }


def find_operation(request, principal, opener, operation_id, condition):
    with opener(request, principal) as session:
        row = session.scalar(
            select(ClaimOperation).where(ClaimOperation.id == operation_id, condition)
        )
        return operation_view(row, session.get(Claim, row.claim_id)) if row else None


@router.get(
    "/v1/claim-operations/{operation_id}",
    response_model=schemas.OperationView,
    tags=["Claims"],
    responses=error_responses(404),
)
def operation_status(operation_id: UUID, request: Request, principal: Actor):
    user_client(request, principal)
    value = None
    # A venue user may also be an owner elsewhere: their own operations are checked first.
    if "owner" in principal.roles:
        value = find_operation(
            request,
            principal,
            owner_session,
            operation_id,
            ClaimOperation.owner_sub == principal.subject,
        )
    if value is None and principal.tenant_id and principal.roles.intersection(STAFF_ROLES):
        staff(request, principal)
        value = find_operation(
            request,
            principal,
            staff_session,
            operation_id,
            ClaimOperation.tenant_id == principal.tenant_id,
        )
    if value is None:
        fail(404, "operation_not_found", "Operation is not visible")
    return value
