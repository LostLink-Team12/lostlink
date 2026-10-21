"""Consistent, non-sensitive API errors."""

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from starlette.exceptions import HTTPException

logger = logging.getLogger("lostlink.errors")


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
