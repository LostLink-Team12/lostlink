"""Consistent, non-sensitive API errors."""

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from starlette.exceptions import HTTPException

logger = logging.getLogger("lostlink.errors")


class ValidationIssue(BaseModel):
    """Sanitized request validation detail; input values are never returned."""

    model_config = ConfigDict(extra="forbid")

    location: list[str | int]
    type: str


class ErrorDetail(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(description="Stable application error code used by clients.")
    message: str = Field(description="Human-readable error message without sensitive values.")
    details: list[ValidationIssue] | None = Field(
        default=None,
        description="Included for request validation errors; omitted for other errors.",
    )


class ErrorResponse(BaseModel):
    """The existing error_response wire format, shared by service OpenAPI contracts."""

    model_config = ConfigDict(extra="forbid")

    error: ErrorDetail
    request_id: str | None = Field(description="Request correlation ID, also sent as X-Request-ID.")


_ERROR_DESCRIPTIONS = {
    401: "A valid Cognito access token is required.",
    403: "The client, role, scope or current membership does not permit this operation.",
    404: "The resource does not exist or is not visible to the caller.",
    409: "The operation conflicts with resource state, an in-progress operation or idempotency.",
    412: "If-Match does not match the current resource version; reload before retrying.",
    413: "The request body exceeds the service size limit.",
    422: "Request validation failed or application input is invalid.",
    428: "A required Idempotency-Key or If-Match header is missing.",
    500: "An unexpected internal error occurred; details are not disclosed.",
    503: "Required configuration, database access or an upstream dependency is unavailable.",
}


def error_responses(*status_codes: int) -> dict[int, dict[str, Any]]:
    """Document existing JSON errors without changing handlers or success responses."""
    responses = {}
    for status in status_codes:
        headers = {
            "X-Request-ID": {
                "description": "Request correlation ID, also returned in the response body.",
                "schema": {"type": "string"},
            }
        }
        if status == 401:
            headers["WWW-Authenticate"] = {"schema": {"type": "string", "example": "Bearer"}}
        if status == 409:
            headers["Retry-After"] = {
                "description": "Present for operation_in_progress; retry delay in seconds.",
                "schema": {"type": "string", "example": "1"},
            }
        responses[status] = {
            "model": ErrorResponse,
            "description": _ERROR_DESCRIPTIONS[status],
            "headers": headers,
        }
    return responses


def error_response(
    request: Request,
    status: int,
    code: str,
    message: str,
    *,
    details: list | None = None,
    headers: dict | None = None,
) -> JSONResponse:
    request_id = getattr(request.state, "request_id", None)
    body = {"error": {"code": code, "message": message}, "request_id": request_id}
    if details is not None:
        body["error"]["details"] = details
    response_headers = dict(headers or {})
    if request_id:
        response_headers["X-Request-ID"] = request_id
    return JSONResponse(body, status_code=status, headers=response_headers)


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        codes = {
            401: "unauthenticated",
            403: "forbidden",
            404: "not_found",
            405: "method_not_allowed",
            409: "conflict",
            503: "unavailable",
        }
        return error_response(
            request,
            exc.status_code,
            codes.get(exc.status_code, "http_error"),
            str(exc.detail),
            headers=exc.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        return error_response(
            request,
            422,
            "validation_error",
            "Request validation failed",
            details=[{"location": list(e["loc"]), "type": e["type"]} for e in exc.errors()],
        )

    @app.exception_handler(IntegrityError)
    async def integrity_error(request: Request, exc: IntegrityError):
        logger.warning("database_constraint_failed", extra={"error_type": type(exc).__name__})
        return error_response(request, 409, "conflict", "The operation conflicts with stored data")

    @app.exception_handler(SQLAlchemyError)
    async def database_error(request: Request, exc: SQLAlchemyError):
        logger.error("database_operation_failed", extra={"error_type": type(exc).__name__})
        return error_response(request, 503, "unavailable", "Database operation is unavailable")

    @app.exception_handler(Exception)
    async def unexpected_error(request: Request, exc: Exception):
        logger.error(
            "unhandled_error",
            extra={
                "error_type": type(exc).__name__,
                "request_id": getattr(request.state, "request_id", None),
            },
        )
        return error_response(request, 500, "internal_error", "An unexpected error occurred")
