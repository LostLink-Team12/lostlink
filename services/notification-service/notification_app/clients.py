"""Fixed-origin Matching context lookups with explicit outcomes."""

import http.client
import json
from collections.abc import Callable
from urllib.parse import urlsplit
from uuid import UUID

from lostlink_common.membership import ClientCredentials, MembershipUnavailable
from pydantic import BaseModel, ConfigDict, Field, ValidationError


class MatchingUnavailable(Exception):
    """Transient: retry later; never treat as permission to send."""


class NotificationContext(BaseModel):
    model_config = ConfigDict(extra="ignore")
    match_id: UUID
    owner_id: str = Field(min_length=1, max_length=128)
    report_id: UUID
    item_id: UUID
    tenant_id: UUID
    active: bool
    item_title: str | None = Field(default=None, max_length=120)
    venue_name: str | None = Field(default=None, max_length=200)


def validate_url(url):
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("A fixed HTTPS service URL is required")
    return parsed


def https_get(url, headers):
    parsed = validate_url(url)
    connection = http.client.HTTPSConnection(parsed.hostname, parsed.port or 443, timeout=2)
    try:
        connection.connect()
        connection.sock.settimeout(5)
        connection.request("GET", parsed.path or "/", headers=headers)
        response = connection.getresponse()
        content = response.read(65537)
        if len(content) > 65536:
            raise MatchingUnavailable("Matching response is too large")
        value = json.loads(content) if content else None
        return response.status, value if isinstance(value, dict) else None
    except (OSError, http.client.HTTPException, ValueError) as exc:
        raise MatchingUnavailable("Matching is unavailable") from exc
    finally:
        connection.close()


class MatchingClient:
    def __init__(self, base_url, token_provider: Callable[[], str], *, transport=https_get):
        validate_url(base_url)
        self.base_url, self.token_provider, self.transport = (
            base_url.rstrip("/"),
            token_provider,
            transport,
        )

    def context(self, match_id: UUID) -> NotificationContext | None:
        """None means Matching does not know the match; other failures raise."""
        try:
            token = self.token_provider()
        except MembershipUnavailable as exc:
            raise MatchingUnavailable("Machine credentials are unavailable") from exc
        try:
            status, value = self.transport(
                f"{self.base_url}/internal/v1/matches/{match_id}/notification-context",
                {"Authorization": f"Bearer {token}"},
            )
        except OSError as exc:
            raise MatchingUnavailable("Matching is unavailable") from exc
        if status == 404:
            return None
        if status != 200:
            raise MatchingUnavailable(f"Matching returned {status}")
        try:
            result = NotificationContext.model_validate(value)
        except ValidationError as exc:
            raise MatchingUnavailable("Invalid Matching response") from exc
        if result.match_id != match_id:
            raise MatchingUnavailable("Mismatched Matching response")
        return result


def make_matching_client(settings):
    if not all(
        [
            settings.matching_base_url,
            settings.machine_token_url,
            settings.machine_client_id,
            settings.machine_client_secret,
        ]
    ):
        return None
    return MatchingClient(
        settings.matching_base_url,
        ClientCredentials(
            settings.machine_token_url,
            settings.machine_client_id,
            settings.machine_client_secret.get_secret_value(),
            ["lostlink/matches.notify"],
        ),
    )
