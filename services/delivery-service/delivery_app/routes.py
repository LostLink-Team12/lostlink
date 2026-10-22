"""Owner authorization and partner status APIs (ADR-0009).

Owners authorize one partner for one of their approved claims and may cancel before
pickup. Partners (machine clients) see only shipments assigned to them and move them
through transport states; pickup itself comes only from the venue's QR scan.
"""

from typing import Annotated
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import JSONResponse
from lostlink_common.auth import Principal, get_principal
from lostlink_common.crypto import DecryptionFailed
from sqlalchemy import func, select

from delivery_app import schemas
from delivery_app.config import PARTNER_SCOPE
from delivery_app.domain import TERMINAL, Actor, State, TransitionError
from delivery_app.errors import fail
from delivery_app.models import ClaimApproval, ShipmentRecord
from delivery_app.service import (
    apply,
    etag,
    history,
    open_address,
    record,
    seal_address,
    to_domain,
    view,
)
from delivery_app.transactions import expected_version, idempotent

router = APIRouter()
Actor_ = Annotated[Principal, Depends(get_principal)]
Key = Annotated[str | None, Header(alias="Idempotency-Key")]
Version = Annotated[str | None, Header(alias="If-Match")]
Limit = Annotated[int, Query(ge=1, le=100)]
Offset = Annotated[int, Query(ge=0, le=10000)]


def owner(request: Request, principal: Actor_):
    if principal.client_id not in request.app.state.settings.user_client_ids:
        fail(403, "forbidden", "A LostLink owner application is required")
    if "owner" not in principal.roles:
        fail(403, "forbidden", "The owner role is required")
    return principal


def partner(request: Request, principal: Actor_):
    settings = request.app.state.settings
    if (
        principal.client_id not in settings.partner_client_ids
        or principal.subject != principal.client_id
        or PARTNER_SCOPE not in principal.scopes
    ):
        fail(403, "forbidden", "An authorized delivery partner is required")
    return principal


Owner = Annotated[Principal, Depends(owner)]
Partner = Annotated[Principal, Depends(partner)]


def database(request):
    if request.app.state.database is None:
        fail(503, "unavailable", "Delivery database is not configured")
    return request.app.state.database


def cipher(request):
    if request.app.state.cipher is None:
        fail(503, "encryption_unavailable", "Delivery addresses cannot be protected")
    return request.app.state.cipher


def owner_session(request, principal):
    return database(request).actor_session(principal, venue_scope=False)


def partner_session(request, principal):
    return database(request).scoped_session(subject=principal.subject)


def result(value):
    body, status, tag = value
    return JSONResponse(body, status_code=status, headers={"ETag": tag} if tag else {})


def now(session):
    return session.scalar(select(func.now()))


def shipment(session, shipment_id, *, owner_sub=None, partner_id=None, lock=False):
    query = select(ShipmentRecord).where(ShipmentRecord.id == shipment_id)
    if owner_sub is not None:
        query = query.where(ShipmentRecord.owner_sub == owner_sub)
    if partner_id is not None:
        query = query.where(ShipmentRecord.partner_id == partner_id)
    row = session.scalar(query.with_for_update() if lock else query)
    if row is None:
        fail(404, "shipment_not_found", "Shipment is not visible")
    return row


def readable_address(request, row):
    try:
        return open_address(cipher(request), row)
    except DecryptionFailed:
        fail(503, "address_unavailable", "Delivery address cannot be read")


def transition(session, row, to_state, actor, version, note=None):
    expected_version(version, row.version)
    try:
        updated, change = to_domain(row).transition(
            to_state, actor, now(session), expected_version=row.version, note=note
        )
    except TransitionError as exc:
        fail(409, exc.code, str(exc))
    apply(session, row, updated, change)


def page(rows, limit, offset):
    return {
        "items": [view(row) for row in rows[:limit]],
        "limit": limit,
        "offset": offset,
        "has_more": len(rows) > limit,
    }


@router.post(
    "/v1/delivery-authorizations",
    status_code=201,
    response_model=schemas.ShipmentView,
    tags=["Owner"],
)
def authorize(
    body: schemas.DeliveryAuthorization, request: Request, principal: Owner, key: Key = None
):
    settings = request.app.state.settings
    sealer = cipher(request)

    def create(session):
        approval = session.get(ClaimApproval, body.claim_id)
        if approval is None or approval.owner_sub != principal.subject:
            fail(404, "claim_not_found", "No approved claim of yours has this ID")
        if approval.state != "approved":
            fail(409, "claim_not_approved", "Delivery can be authorized only for approved claims")
        if body.partner_id not in settings.partner_client_ids:
            fail(422, "unknown_partner", "Choose one of the delivery partners")
        existing = session.scalar(
            select(ShipmentRecord.id).where(ShipmentRecord.claim_id == body.claim_id)
        )
        if existing is not None:
            fail(409, "shipment_exists", "Delivery is already authorized for this claim")
        address = body.delivery_address.model_dump()
        shipment_id = uuid4()
        sealed = seal_address(sealer, shipment_id, address)
        row = ShipmentRecord(
            id=shipment_id,
            tenant_id=approval.tenant_id,
            claim_id=approval.claim_id,
            item_id=approval.item_id,
            owner_sub=principal.subject,
            partner_id=body.partner_id,
            state=State.AWAITING_PICKUP.value,
            version=1,
            delivery_attempts=0,
            address_key_id=sealed.key_id,
            address_ciphertext=sealed.ciphertext,
        )
        session.add(row)
        session.flush()
        record(
            session,
            row,
            from_state=None,
            to_state=row.state,
            actor=Actor.OWNER.value,
            version=1,
            at=now(session),
        )
        session.flush()
        # The replay snapshot is stored in plaintext, so it never contains the address.
        return view(row, events=history(session, row.id)), 201, etag(row)

    with owner_session(request, principal) as session:
        value = idempotent(
            session,
            principal,
            "POST /v1/delivery-authorizations",
            key,
            body.model_dump(mode="json"),
            None,
            lambda: create(session),
        )
    return result(value)


@router.get("/v1/shipments", response_model=schemas.ShipmentsPage, tags=["Owner"])
def owner_shipments(request: Request, principal: Owner, limit: Limit = 20, offset: Offset = 0):
    with owner_session(request, principal) as session:
        rows = list(
            session.scalars(
                select(ShipmentRecord)
                .where(ShipmentRecord.owner_sub == principal.subject)
                .order_by(ShipmentRecord.created_at.desc(), ShipmentRecord.id)
                .limit(limit + 1)
                .offset(offset)
            )
        )
    return page(rows, limit, offset)


@router.get("/v1/shipments/{shipment_id}", response_model=schemas.ShipmentView, tags=["Owner"])
def owner_shipment(shipment_id: UUID, request: Request, principal: Owner):
    with owner_session(request, principal) as session:
        row = shipment(session, shipment_id, owner_sub=principal.subject)
        value = view(row, events=history(session, row.id), address=readable_address(request, row))
        return JSONResponse(value, headers={"ETag": etag(row)})


@router.post(
    "/v1/shipments/{shipment_id}/cancel", response_model=schemas.ShipmentView, tags=["Owner"]
)
def cancel(
    shipment_id: UUID, request: Request, principal: Owner, key: Key = None, version: Version = None
):
    def run(session):
        row = shipment(session, shipment_id, owner_sub=principal.subject, lock=True)
        transition(session, row, State.CANCELLED, Actor.OWNER, version)
        return view(row, events=history(session, row.id)), 200, etag(row)

    with owner_session(request, principal) as session:
        value = idempotent(
            session,
            principal,
            f"POST /v1/shipments/{shipment_id}/cancel",
            key,
            {},
            version,
            lambda: run(session),
        )
    return result(value)


@router.get("/v1/partner/shipments", response_model=schemas.ShipmentsPage, tags=["Partner"])
def partner_shipments(
    request: Request,
    principal: Partner,
    limit: Limit = 20,
    offset: Offset = 0,
    active: bool = True,
):
    with partner_session(request, principal) as session:
        query = select(ShipmentRecord).where(ShipmentRecord.partner_id == principal.subject)
        if active:
            query = query.where(ShipmentRecord.state.not_in([state.value for state in TERMINAL]))
        rows = list(
            session.scalars(
                query.order_by(ShipmentRecord.created_at, ShipmentRecord.id)
                .limit(limit + 1)
                .offset(offset)
            )
        )
    return page(rows, limit, offset)


@router.get(
    "/v1/partner/shipments/{shipment_id}", response_model=schemas.ShipmentView, tags=["Partner"]
)
def partner_shipment(shipment_id: UUID, request: Request, principal: Partner):
    with partner_session(request, principal) as session:
        row = shipment(session, shipment_id, partner_id=principal.subject)
        # The address is released only while the partner still has to deliver the item.
        address = None if State(row.state) in TERMINAL else readable_address(request, row)
        value = view(row, events=history(session, row.id), address=address)
        return JSONResponse(value, headers={"ETag": etag(row)})


@router.post(
    "/v1/partner/shipments/{shipment_id}/status",
    response_model=schemas.ShipmentView,
    tags=["Partner"],
)
def partner_update(
    shipment_id: UUID,
    body: schemas.PartnerUpdate,
    request: Request,
    principal: Partner,
    key: Key = None,
    version: Version = None,
):
    def run(session):
        row = shipment(session, shipment_id, partner_id=principal.subject, lock=True)
        transition(session, row, State(body.state), Actor.PARTNER, version, body.note)
        return view(row, events=history(session, row.id)), 200, etag(row)

    with partner_session(request, principal) as session:
        value = idempotent(
            session,
            principal,
            f"POST /v1/partner/shipments/{shipment_id}/status",
            key,
            body.model_dump(mode="json"),
            version,
            lambda: run(session),
        )
    return result(value)
