"""Stable business error codes."""

from fastapi import FastAPI, Request
from lostlink_common.errors import error_response


class VenueError(Exception):
    def __init__(self, status: int, code: str, message: str):
        self.status, self.code, self.message = status, code, message


def install_venue_errors(app: FastAPI):
    @app.exception_handler(VenueError)
    async def handle(request: Request, exc: VenueError):
        headers = {"Retry-After": "1"} if exc.code == "operation_in_progress" else None
        return error_response(request, exc.status, exc.code, exc.message, headers=headers)
