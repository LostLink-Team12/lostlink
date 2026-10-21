"""Stable business errors with no hidden fields in responses."""

from fastapi import Request
from lostlink_common.errors import error_response


class ItemError(Exception):
    def __init__(self, status, code, message):
        self.status, self.code, self.message = status, code, message


def fail(status, code, message):
    raise ItemError(status, code, message)


def install_item_errors(app):
    @app.exception_handler(ItemError)
    async def handle(request: Request, exc: ItemError):
        headers = {"Retry-After": "1"} if exc.code == "operation_in_progress" else None
        return error_response(request, exc.status, exc.code, exc.message, headers=headers)
