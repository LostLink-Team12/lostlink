"""Tenant employee APIs and separately authenticated service-only interfaces."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from fastapi.responses import JSONResponse
from lostlink_common.auth import Principal, get_principal
from sqlalchemy import select

from item_app import imports
from item_app import schemas as outputs
from item_app.errors import fail
from item_app.models import Item, ItemLocator, Photo, Question, WorkJob
from item_app.schemas import (
    HandoverRequest,
    ImportRepair,
    ImportRequest,
    ItemChange,
    ItemDetail,
    ItemsPage,
    ItemView,
    NewItem,
    ReleaseRequest,
    ReservationView,
    ReserveRequest,
    State,
    UploadRequest,
    Withdrawal,
)
from item_app.service import ItemService, item_view
from item_app.storage import StorageError
from item_app.transactions import idempotent

router = APIRouter()
Actor = Annotated[Principal, Depends(get_principal)]
Key = Annotated[str | None, Header(alias="Idempotency-Key")]
Version = Annotated[str | None, Header(alias="If-Match")]


def employee(request: Request, principal: Actor):
    if principal.client_id not in request.app.state.settings.user_client_ids:
        fail(403, "forbidden", "A staff application is required")
    client = request.app.state.membership_client
    if client is None:
        fail(503, "membership_unavailable", "Current membership is not configured")
    client.require_current(principal, "venue_admin", "venue_staff")
    return principal


Employee = Annotated[Principal, Depends(employee)]


def database(request, *, internal=False):
    db = request.app.state.internal_database if internal else request.app.state.database
    if db is None:
        fail(503, "unavailable", "Required database access mode is not configured")
    return db


def domain(session, principal, request):
    return ItemService(session, principal.subject, getattr(request.state, "request_id", None))


def result(value):
    body, status, etag = value
    return JSONResponse(body, status_code=status, headers={"ETag": etag} if etag else {})


def write(
    request, principal, operation, key, body, version, callback, *, tenant=None, internal=False
):
    tenant = tenant or principal.tenant_id
    with database(request, internal=internal).context(
        subject=principal.subject, tenant_id=tenant
    ) as session:
        value = idempotent(
            session,
            principal,
            tenant,
            operation,
            key,
            body,
            version,
            lambda: callback(domain(session, principal, request), session),
        )
    return result(value)


def locate(request, principal, item_id, scope):
    settings = request.app.state.settings
    clients = (
        settings.claim_client_ids
        if scope in {"items.context", "items.reserve"}
        else settings.index_client_ids
    )
    if principal.client_id not in clients or f"lostlink/{scope}" not in principal.scopes:
        fail(403, "forbidden", "Client and scope do not allow this operation")
    db = database(request, internal=True)
    with db.context(subject=principal.subject, resource_id=item_id) as session:
        row = session.get(ItemLocator, item_id)
        if row is None:
            fail(404, "item_not_found", "Item is not visible")
        return row.tenant_id


@router.post("/v1/items", status_code=201, response_model=ItemView, tags=["Items"])
def create(body: NewItem, request: Request, principal: Employee, key: Key = None):
    return write(
        request,
        principal,
        "POST /v1/items",
        key,
        body.model_dump(mode="json"),
        None,
        lambda service, _: service.create(principal.tenant_id, body),
    )


@router.get("/v1/items", response_model=ItemsPage, tags=["Items"])
def list_items(
    request: Request,
    principal: Employee,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0, le=10000)] = 0,
    state: State | None = None,
    category: Annotated[str | None, Query(max_length=100)] = None,
):
    with database(request).context(
        subject=principal.subject, tenant_id=principal.tenant_id
    ) as session:
        query = select(Item).order_by(Item.created_at, Item.id)
        if state:
            query = query.where(Item.state == state)
        if category:
            query = query.where(Item.category == category)
        rows = list(session.scalars(query.limit(limit + 1).offset(offset)))
        return {
            "items": [item_view(row) for row in rows[:limit]],
            "limit": limit,
            "offset": offset,
            "has_more": len(rows) > limit,
        }


@router.get("/v1/items/{item_id}", response_model=ItemDetail, tags=["Items"])
def detail(item_id: UUID, request: Request, response: Response, principal: Employee):
    with database(request).context(
        subject=principal.subject, tenant_id=principal.tenant_id
    ) as session:
        service = domain(session, principal, request)
        item = service.get(item_id)
        response.headers["ETag"] = f'"{item.version}"'
        return service.detail(item)


@router.patch("/v1/items/{item_id}", response_model=ItemView, tags=["Items"])
def change(
    item_id: UUID,
    body: ItemChange,
    request: Request,
    principal: Employee,
    key: Key = None,
    version: Version = None,
):
    return write(
        request,
        principal,
        f"PATCH /v1/items/{item_id}",
        key,
        body.model_dump(mode="json", exclude_unset=True),
        version,
        lambda service, _: service.change(item_id, body, version),
    )


@router.post("/v1/items/{item_id}/withdraw", response_model=ItemView, tags=["Items"])
def withdraw(
    item_id: UUID,
    body: Withdrawal,
    request: Request,
    principal: Employee,
    key: Key = None,
    version: Version = None,
):
    return write(
        request,
        principal,
        f"POST /v1/items/{item_id}/withdraw",
        key,
        body.model_dump(),
        version,
        lambda service, _: service.withdraw(item_id, version),
    )


@router.post(
    "/v1/items/{item_id}/uploads",
    status_code=201,
    response_model=outputs.UploadView,
    tags=["Photos"],
)
def upload(
    item_id: UUID,
    body: UploadRequest,
    request: Request,
    principal: Employee,
    key: Key = None,
    version: Version = None,
):
    if not request.app.state.storage or not request.app.state.settings.photo_bucket:
        fail(503, "storage_unavailable", "Photo storage is not configured")
    response = write(
        request,
        principal,
        f"POST /v1/items/{item_id}/uploads",
        key,
        body.model_dump(),
        version,
        lambda service, _: service.upload(
            item_id, body, version, request.app.state.settings.photo_bucket
        ),
    )
    import json

    value = json.loads(response.body)
    with database(request).context(
        subject=principal.subject, tenant_id=principal.tenant_id
    ) as session:
        photo = session.get(Photo, UUID(value["upload_id"]))
        item = session.get(Item, item_id)
        if photo is None or item.active_upload_id != photo.id or photo.status != "pending":
            fail(409, "upload_superseded", "Upload is no longer current")
    try:
        value["upload"] = request.app.state.storage.presign(photo)
    except StorageError:
        fail(409, "upload_expired", "Create a new upload")
    return JSONResponse(value, status_code=201, headers={"ETag": response.headers["ETag"]})


@router.post(
    "/v1/items/{item_id}/uploads/{upload_id}/confirm",
    status_code=202,
    response_model=outputs.AcceptedOperation,
    tags=["Photos"],
)
def confirm(item_id: UUID, upload_id: UUID, request: Request, principal: Employee, key: Key = None):
    return write(
        request,
        principal,
        f"POST /v1/items/{item_id}/uploads/{upload_id}/confirm",
        key,
        {},
        None,
        lambda service, _: service.confirm(item_id, upload_id),
    )


@router.post(
    "/v1/items/{item_id}/retry-embedding",
    status_code=202,
    response_model=outputs.ProcessingView,
    tags=["Items"],
)
def retry(
    item_id: UUID, request: Request, principal: Employee, key: Key = None, version: Version = None
):
    return write(
        request,
        principal,
        f"POST /v1/items/{item_id}/retry-embedding",
        key,
        {},
        version,
        lambda service, _: service.retry_embedding(item_id, version),
    )


@router.get(
    "/v1/item-operations/{operation_id}", response_model=outputs.OperationView, tags=["Operations"]
)
def operation(operation_id: UUID, request: Request, principal: Employee):
    with database(request).context(
        subject=principal.subject, tenant_id=principal.tenant_id
    ) as session:
        job = session.get(WorkJob, operation_id)
        if job is None:
            fail(404, "operation_not_found", "Operation is not visible")
        return {
            "id": str(job.id),
            "state": job.state,
            "kind": job.kind,
            "attempts": job.attempts,
            "last_error_code": job.last_error_code,
        }


@router.post(
    "/v1/item-imports", status_code=202, response_model=outputs.ImportView, tags=["Imports"]
)
def submit_import(body: ImportRequest, request: Request, principal: Employee, key: Key = None):
    return write(
        request,
        principal,
        "POST /v1/item-imports",
        key,
        body.model_dump(mode="json"),
        None,
        lambda _, session: imports.submit(session, principal, body),
    )


@router.get("/v1/item-imports/{job_id}", response_model=outputs.ImportStatus, tags=["Imports"])
def import_status(
    job_id: UUID,
    request: Request,
    principal: Employee,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0, le=10000)] = 0,
):
    with database(request).context(
        subject=principal.subject, tenant_id=principal.tenant_id
    ) as session:
        return imports.status(session, job_id, limit, offset)


@router.post(
    "/v1/item-imports/{job_id}/retry",
    status_code=202,
    response_model=outputs.ImportView,
    tags=["Imports"],
)
def repair_import(
    job_id: UUID, body: ImportRepair, request: Request, principal: Employee, key: Key = None
):
    return write(
        request,
        principal,
        f"POST /v1/item-imports/{job_id}/retry",
        key,
        body.model_dump(mode="json"),
        None,
        lambda _, session: imports.repair(session, job_id, body),
    )


@router.get(
    "/internal/v1/items/{item_id}/claim-context",
    response_model=outputs.ClaimContext,
    tags=["Internal"],
)
def claim_context(item_id: UUID, request: Request, principal: Actor):
    tenant = locate(request, principal, item_id, "items.context")
    with database(request, internal=True).context(
        subject=principal.subject, tenant_id=tenant
    ) as session:
        item = domain(session, principal, request).get(item_id)
        questions = session.execute(
            select(Question.id, Question.prompt, Question.version)
            .where(Question.item_id == item.id)
            .order_by(Question.position)
        )
        return {
            "item_id": str(item.id),
            "tenant_id": str(tenant),
            "item_version": item.item_version,
            "state": item.state,
            "title": item.title,
            "version": item.version,
            "questions": [
                {"id": str(row.id), "prompt": row.prompt, "version": row.version}
                for row in questions
            ],
        }


@router.get(
    "/internal/v1/items/{item_id}/index-context",
    response_model=outputs.IndexContext,
    tags=["Internal"],
)
def index_context(item_id: UUID, request: Request, principal: Actor):
    tenant = locate(request, principal, item_id, "items.index")
    with database(request, internal=True).context(
        subject=principal.subject, tenant_id=tenant
    ) as session:
        item = domain(session, principal, request).get(item_id)
        photo = session.get(Photo, item.photo_id) if item.photo_id else None
        value = {
            **item_view(item),
            "item_id": str(item.id),
            "tenant_id": str(tenant),
            "revision": item.version,
            "processing_correlation_id": str(item.processing_correlation_id)
            if item.processing_correlation_id
            else None,
            "photo_bucket": photo.bucket if photo else None,
            "photo_key": photo.sealed_key if photo else None,
        }
    value["photo_url"] = (
        request.app.state.storage.photo_url(photo)
        if photo and item.state == "available" and request.app.state.storage
        else None
    )
    return value


def reserve_write(request, principal, item_id, operation, key, body, callback):
    tenant = locate(request, principal, item_id, "items.reserve")
    return write(
        request, principal, operation, key, body, None, callback, tenant=tenant, internal=True
    )


@router.post(
    "/internal/v1/items/{item_id}/reservations",
    response_model=ReservationView,
    tags=["Reservations"],
)
def reserve(
    item_id: UUID, body: ReserveRequest, request: Request, principal: Actor, key: Key = None
):
    return reserve_write(
        request,
        principal,
        item_id,
        f"POST /internal/v1/items/{item_id}/reservations",
        key,
        body.model_dump(mode="json"),
        lambda service, _: service.reserve(item_id, body),
    )


@router.get(
    "/internal/v1/items/{item_id}/reservations/{claim_id}",
    response_model=ReservationView,
    tags=["Reservations"],
)
def reservation_status(item_id: UUID, claim_id: UUID, request: Request, principal: Actor):
    tenant = locate(request, principal, item_id, "items.reserve")
    with database(request, internal=True).context(
        subject=principal.subject, tenant_id=tenant
    ) as session:
        service = domain(session, principal, request)
        item, row = service.get(item_id), service.reservation(item_id, claim_id)
        if row is None:
            fail(404, "reservation_not_found", "Reservation is not visible")
        return service.reservation_view(item, row)


@router.post(
    "/internal/v1/items/{item_id}/reservations/{claim_id}/release",
    response_model=ReservationView,
    tags=["Reservations"],
)
def release(
    item_id: UUID,
    claim_id: UUID,
    body: ReleaseRequest,
    request: Request,
    principal: Actor,
    key: Key = None,
):
    return reserve_write(
        request,
        principal,
        item_id,
        f"POST /internal/v1/items/{item_id}/reservations/{claim_id}/release",
        key,
        body.model_dump(),
        lambda service, _: service.release(item_id, claim_id),
    )


@router.post(
    "/internal/v1/items/{item_id}/reservations/{claim_id}/commit-handover",
    response_model=ReservationView,
    tags=["Reservations"],
)
def handover(
    item_id: UUID,
    claim_id: UUID,
    body: HandoverRequest,
    request: Request,
    principal: Actor,
    key: Key = None,
):
    return reserve_write(
        request,
        principal,
        item_id,
        f"POST /internal/v1/items/{item_id}/reservations/{claim_id}/commit-handover",
        key,
        body.model_dump(mode="json"),
        lambda service, _: service.handover(item_id, claim_id, body),
    )
