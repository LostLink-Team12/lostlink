"""Stable business errors; responses never contain answers, contacts or token values."""

from fastapi import Request
from lostlink_common.errors import error_response


class ClaimError(Exception):
    def __init__(self, status, code, message):
        self.status, self.code, self.message = status, code, message


def fail(status, code, message):
    raise ClaimError(status, code, message)


def install_claim_errors(app):
    @app.exception_handler(ClaimError)
    async def handle(request: Request, exc: ClaimError):
        headers = {"Retry-After": "1"} if exc.code == "operation_in_progress" else None
        return error_response(request, exc.status, exc.code, exc.message, headers=headers)
