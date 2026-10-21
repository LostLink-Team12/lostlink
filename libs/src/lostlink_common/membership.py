"""Fail-closed Venue membership checks for downstream services."""

import base64
import http.client
import json
import threading
import time
from collections.abc import Callable
from urllib.parse import urlencode, urlsplit

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict

from lostlink_common.auth import Principal


class MembershipUnavailable(Exception):
    """Membership or machine-token authority could not be reached safely."""


def post_json_or_form(url: str, headers: dict, body: bytes) -> dict:
    """Fixed HTTPS endpoints, bounded I/O, no redirects or credential logging."""
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.fragment
    ):
        raise MembershipUnavailable("An HTTPS service URL is required")
    connection = http.client.HTTPSConnection(parsed.hostname, parsed.port or 443, timeout=2)
    deadline = time.monotonic() + 15
    try:
        connection.connect()
        connection.sock.settimeout(min(5, max(0.1, deadline - time.monotonic())))
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query
        connection.request("POST", path, body=body, headers=headers)
        response = connection.getresponse()
        content = response.read(65537)
        if not 200 <= response.status < 300 or len(content) > 65536:
            raise MembershipUnavailable("Service rejected the request")
        payload = json.loads(content)
        if not isinstance(payload, dict):
            raise MembershipUnavailable("Service returned an invalid response")
        return payload
    except (OSError, http.client.HTTPException, ValueError) as exc:
        raise MembershipUnavailable("Service is unavailable") from exc
    finally:
        connection.close()


class ClientCredentials:
    def __init__(
        self,
        token_url: str,
        client_id: str,
        client_secret: str,
        scopes: list[str],
        *,
        transport: Callable = post_json_or_form,
    ):
        self.url, self.client_id, self.secret = token_url, client_id, client_secret
        self.scopes, self.transport = scopes, transport
        self.lock = threading.Lock()
        self.token, self.expires = None, 0.0

    def __call__(self) -> str:
        with self.lock:
            if self.token and time.monotonic() < self.expires:
                return self.token
            credentials = base64.b64encode(f"{self.client_id}:{self.secret}".encode()).decode()
            body = urlencode({"grant_type": "client_credentials", "scope": " ".join(self.scopes)})
            try:
                result = self.transport(
                    self.url,
                    {
                        "Authorization": f"Basic {credentials}",
                        "Content-Type": "application/x-www-form-urlencoded",
                    },
                    body.encode(),
                )
                token, expiry = result["access_token"], result["expires_in"]
                if (
                    not isinstance(token, str)
                    or not token
                    or type(expiry) is not int
                    or expiry <= 0
                    or str(result.get("token_type", "")).lower() != "bearer"
                ):
                    raise ValueError("Invalid token response")
                self.token, self.expires = token, time.monotonic() + max(0, expiry - 60)
                return token
            except (KeyError, ValueError, OSError) as exc:
                raise MembershipUnavailable("Machine credentials are unavailable") from exc


class MembershipResult(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    active: bool
    effective_role: str | None
    membership_version: int | None
    venue_status: str | None


class MembershipClient:
    def __init__(
        self,
        base_url: str,
        token_provider: Callable[[], str],
        *,
        transport: Callable = post_json_or_form,
    ):
        self.url = base_url.rstrip("/") + "/internal/v1/members/check"
        self.token_provider, self.transport = token_provider, transport

    def require_current(self, principal: Principal, *allowed_roles: str) -> MembershipResult:
        if not allowed_roles or not set(allowed_roles) <= {"venue_admin", "venue_staff"}:
            raise ValueError("Use explicit Venue roles")
        if principal.tenant_id is None or not principal.roles.intersection(allowed_roles):
            raise HTTPException(403, "Venue membership required")
        try:
            body = json.dumps(
                {
                    "subject": principal.subject,
                    "tenant_id": str(principal.tenant_id),
                    "allowed_roles": list(allowed_roles),
                }
            ).encode()
            value = self.transport(
                self.url,
                {
                    "Authorization": f"Bearer {self.token_provider()}",
                    "Content-Type": "application/json",
                },
                body,
            )
            result = MembershipResult.model_validate(value)
        except (MembershipUnavailable, ValueError, OSError) as exc:
            raise HTTPException(503, "Current membership is unavailable") from exc
        if (
            not result.active
            or result.venue_status != "active"
            or result.effective_role not in allowed_roles
            or result.effective_role not in principal.roles
        ):
            raise HTTPException(403, "Current membership does not allow this operation")
        return result
