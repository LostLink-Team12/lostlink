"""Reject oversized JSON before parsing or inference, including chunked requests."""

from fastapi import HTTPException, Request
from lostlink_common.errors import error_response

MAX_REQUEST_BYTES = 8 * 1024 * 1024


class BodyLimitMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers", []))
        try:
            length = int(headers.get(b"content-length", b"0"))
        except ValueError:
            length = MAX_REQUEST_BYTES + 1
        if length > MAX_REQUEST_BYTES or length < 0:
            await error_response(
                Request(scope),
                413,
                "request_too_large",
                "Request exceeds 8 MiB",
            )(scope, receive, send)
            return
        consumed = 0

        async def bounded_receive():
            nonlocal consumed
            message = await receive()
            consumed += len(message.get("body", b""))
            if consumed > MAX_REQUEST_BYTES:
                raise HTTPException(413, "Request exceeds 8 MiB")
            return message

        await self.app(scope, bounded_receive, send)
