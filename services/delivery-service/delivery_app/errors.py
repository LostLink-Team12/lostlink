"""Stable business errors; responses never contain the delivery address."""

from fastapi import Request
from lostlink_common.errors import error_response


class DeliveryError(Exception):
    def __init__(self, status, code, message):
        self.status, self.code, self.message = status, code, message


def fail(status, code, message):
    raise DeliveryError(status, code, message)


def install_delivery_errors(app):
    @app.exception_handler(DeliveryError)
    async def handle(request: Request, exc: DeliveryError):
        headers = {"Retry-After": "1"} if exc.code == "operation_in_progress" else None
        return error_response(request, exc.status, exc.code, exc.message, headers=headers)
