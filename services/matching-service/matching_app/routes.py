"""Owner-only reports and retrieval, plus a narrowly scoped Notification lookup."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from fastapi.responses import JSONResponse
from lostlink_common.auth import Principal, get_principal
from lostlink_common.errors import error_responses
from lostlink_common.membership import MembershipUnavailable
from sqlalchemy import select

from matching_app import schemas
from matching_app.embedding import EmbeddingError, decode_photo
from matching_app.errors import fail
from matching_app.models import Match, Report
from matching_app.service import (
    candidates,
    close_report,
    create_report,
    hit,
    is_current,
    report,
    report_view,
)
from matching_app.transactions import idempotent, replay

router = APIRouter(responses=error_responses(401, 403, 413, 422, 500, 503))
Actor = Annotated[Principal, Depends(get_principal)]
Key = Annotated[str | None, Header(alias="Idempotency-Key")]
Version = Annotated[str | None, Header(alias="If-Match")]


def owner(request, principal):
    if (
        principal.client_id not in request.app.state.settings.user_client_ids
        or "owner" not in principal.roles
    ):
        fail(403, "forbidden", "An owner identity from an allowed application is required")


def database(request, internal=False):
    db = request.app.state.internal_database if internal else request.app.state.database
    if db is None:
        fail(503, "unavailable", "Database access is not configured")
    return db


def item_context(request, item_id):
    if request.app.state.item_client is None:
        fail(503, "unavailable", "Item access is not configured")
    try:
        return request.app.state.item_client.context(item_id)
    except MembershipUnavailable:
        fail(503, "unavailable", "Current Item state is unavailable")


def encode(request, text, photo):
    encoder = request.app.state.encoder
    try:
        return encoder.encode(text, decode_photo(photo) if photo else None)
    except EmbeddingError as exc:
        fail(422 if not exc.retryable else 503, exc.code, "Query could not be encoded")


def result(value):
    body, status, etag = value
    return JSONResponse(body, status_code=status, headers={"ETag": etag} if etag else {})


@router.post(
    "/v1/lost-reports",
    status_code=201,
    response_model=schemas.ReportView,
    responses=error_responses(409, 428),
)
def new_report(body: schemas.NewReport, request: Request, principal: Actor, key: Key = None):
    owner(request, principal)
    db = database(request)
    payload = body.model_dump(mode="json")
    # A completed retry works even if inference is temporarily unavailable.
    with db.context(subject=principal.subject) as session:
        cached = replay(session, principal, None, "POST /v1/lost-reports", key, payload, None)
    if cached:
        return result(cached)
    vector = encode(request, body.description, body.photo_base64)
    with db.context(subject=principal.subject) as session:
        value = idempotent(
            session,
            principal,
            None,
            "POST /v1/lost-reports",
            key,
            payload,
            None,
            lambda: create_report(
                session, principal.subject, body, vector, request.app.state.encoder.model_version
            ),
        )
    return result(value)


@router.get("/v1/lost-reports", response_model=schemas.ReportsPage)
def list_reports(
    request: Request,
    principal: Actor,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0, le=10000)] = 0,
):
    owner(request, principal)
    with database(request).context(subject=principal.subject) as session:
        rows = list(
            session.scalars(
                select(Report)
                .order_by(Report.created_at, Report.id)
                .limit(limit + 1)
                .offset(offset)
            )
        )
        return {
            "items": [report_view(row) for row in rows[:limit]],
            "limit": limit,
            "offset": offset,
            "has_more": len(rows) > limit,
        }


@router.get(
    "/v1/lost-reports/{report_id}",
    response_model=schemas.ReportView,
    responses=error_responses(404),
)
def get_report(report_id: UUID, request: Request, response: Response, principal: Actor):
    owner(request, principal)
    with database(request).context(subject=principal.subject) as session:
        row = report(session, report_id)
        response.headers["ETag"] = f'"{row.version}"'
        return report_view(row)


@router.post(
    "/v1/lost-reports/{report_id}/close",
    response_model=schemas.ReportView,
    responses=error_responses(404, 409, 412, 428),
)
def close(
    report_id: UUID,
    body: schemas.CloseReport,
    request: Request,
    principal: Actor,
    key: Key = None,
    version: Version = None,
):
    owner(request, principal)
    with database(request).context(subject=principal.subject) as session:
        row = report(session, report_id, lock=True)
        value = idempotent(
            session,
            principal,
            None,
            f"POST /v1/lost-reports/{report_id}/close",
            key,
            body.model_dump(),
            version,
            lambda: close_report(session, principal.subject, row, version),
        )
    return result(value)


@router.post("/v1/search", response_model=schemas.SearchResults)
def search(body: schemas.Search, request: Request, principal: Actor):
    owner(request, principal)
    metrics = request.app.state.search_metrics
    with metrics.phase_seconds.labels("encode").time():
        vector = encode(request, body.text, body.photo_base64)
    model_version = request.app.state.encoder.model_version
    bound = request.app.state.settings.candidate_limit
    with metrics.phase_seconds.labels("candidates").time():
        with database(request).context(subject=principal.subject) as session:
            rows = candidates(session, vector, model_version, body, bound, body.min_score)
    metrics.candidates.observe(len(rows))
    values = []
    with metrics.phase_seconds.labels("item_context").time():
        for item, embedding, score in rows:
            metrics.item_context_calls.inc()
            context = item_context(request, item.item_id)
            if is_current(context, item, embedding, body):
                values.append(hit(context, score))
            if len(values) == body.limit:
                break
    metrics.results.observe(len(values))
    return {
        "items": values,
        "limit": body.limit,
        "candidate_limit": bound,
        "model_version": model_version,
        "potentially_truncated": len(rows) == bound,
    }


@router.get(
    "/v1/lost-reports/{report_id}/matches",
    response_model=schemas.MatchesPage,
    responses=error_responses(404),
)
def list_matches(
    report_id: UUID,
    request: Request,
    principal: Actor,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0, le=10000)] = 0,
):
    owner(request, principal)
    with database(request).context(subject=principal.subject) as session:
        row = report(session, report_id)
        rows = (
            list(
                session.scalars(
                    select(Match)
                    .where(Match.report_id == row.id, Match.active.is_(True))
                    .order_by(Match.created_at, Match.id)
                    .limit(limit + 1)
                    .offset(offset)
                )
            )
            if (row.state == "open")
            else []
        )
    values = []
    for match in rows[:limit]:
        current = item_context(request, match.item_id)
        if (
            current
            and current.state == "available"
            and current.tenant_id == match.tenant_id
            and current.item_version == match.item_version
            and current.processing_correlation_id == match.correlation_id
            and is_filter_current(current, row)
        ):
            values.append(
                {
                    "id": match.id,
                    "report_id": row.id,
                    "created_at": match.created_at,
                    "item": hit(current, match.score),
                }
            )
    return {"items": values, "limit": limit, "offset": offset, "has_more": len(rows) > limit}


def is_filter_current(context, row):
    from matching_app.service import filters_match

    return filters_match(context, row)


@router.get(
    "/internal/v1/matches/{match_id}/notification-context",
    response_model=schemas.NotificationContext,
    responses=error_responses(404),
)
def notification_context(match_id: UUID, request: Request, principal: Actor):
    settings = request.app.state.settings
    if (
        principal.client_id not in settings.notification_client_ids
        or "lostlink/matches.notify" not in principal.scopes
    ):
        fail(403, "forbidden", "Notification client and scope are required")
    db = database(request, internal=True)
    with db.context(subject=principal.subject, resource_id=match_id) as session:
        match = session.get(Match, match_id)
        if match is None:
            fail(404, "match_not_found", "Match is not visible")
        row = session.execute(
            select(
                Report.state,
                Report.category,
                Report.venue_id,
                Report.found_from,
                Report.found_until,
            ).where(Report.id == match.report_id)
        ).first()
    current = item_context(request, match.item_id)
    # Recheck report closure after the remote read.
    with db.context(subject=principal.subject, resource_id=match_id) as session:
        latest = session.execute(
            select(
                Report.state,
                Report.category,
                Report.venue_id,
                Report.found_from,
                Report.found_until,
            ).where(Report.id == match.report_id)
        ).first()
        active = bool(
            row
            and latest
            and latest.state == "open"
            and match.active
            and current
            and current.state == "available"
            and current.tenant_id == match.tenant_id
            and current.item_version == match.item_version
            and current.processing_correlation_id == match.correlation_id
            and is_filter_current(current, latest)
        )
    return {
        "match_id": match.id,
        "owner_id": match.owner_id,
        "report_id": match.report_id,
        "item_id": match.item_id,
        "tenant_id": match.tenant_id,
        "active": active,
        "item_title": current.title if active else None,
        # Venue does not yet expose a machine-readable public directory.
        "venue_name": None,
    }
