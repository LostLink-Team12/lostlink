"""Stable business errors; responses never contain addresses or provider messages."""

from fastapi import Request
from lostlink_common.errors import error_response


class NotificationError(Exception):
    def __init__(self, status, code, message):
        self.status, self.code, self.message = status, code, message


def fail(status, code, message):
    raise NotificationError(status, code, message)


def install_notification_errors(app):
    @app.exception_handler(NotificationError)
    async def handle(request: Request, exc: NotificationError):
        headers = {"Retry-After": "1"} if exc.code == "operation_in_progress" else None
        return error_response(request, exc.status, exc.code, exc.message, headers=headers)
