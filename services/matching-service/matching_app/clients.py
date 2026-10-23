"""Bounded fixed-origin Item reads, without redirects or user-provided URLs."""

import http.client
import json
import ssl
from functools import partial
from urllib.parse import urlsplit
from uuid import UUID

from lostlink_common.membership import ClientCredentials, MembershipUnavailable
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field


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
        raise MembershipUnavailable("A fixed HTTPS service URL is required")
    return parsed


def get_json(url, headers, *, tls_context=None):
    parsed = validate_url(url)
    connection = http.client.HTTPSConnection(
        parsed.hostname, parsed.port or 443, timeout=2, context=tls_context
    )
    try:
        connection.connect()
        connection.sock.settimeout(5)
        connection.request("GET", parsed.path or "/", headers=headers)
        response = connection.getresponse()
        content = response.read(65537)
        if response.status == 404:
            return None
        if response.status != 200 or len(content) > 65536:
            raise MembershipUnavailable("Item context is unavailable")
        value = json.loads(content)
        if not isinstance(value, dict):
            raise MembershipUnavailable("Invalid Item context")
        return value
    except (OSError, http.client.HTTPException, ValueError) as exc:
        raise MembershipUnavailable("Item is unavailable") from exc
    finally:
        connection.close()


class ItemContext(BaseModel):
    model_config = ConfigDict(extra="ignore")
    item_id: UUID
    tenant_id: UUID
    item_version: int = Field(ge=1)
    revision: int = Field(ge=1)
    state: str
    title: str = Field(min_length=1, max_length=120)
    description: str = Field(min_length=1, max_length=2000)
    category: str = Field(min_length=1, max_length=100)
    found_at: AwareDatetime
    public_location_hint: str | None = None
    processing_correlation_id: UUID | None
    photo_bucket: str | None
    photo_key: str | None
    photo_url: str | None = None


class ItemClient:
    def __init__(self, base_url, token_provider, *, transport=None):
        validate_url(base_url)
        self.base_url, self.token_provider = (
            base_url.rstrip("/"),
            token_provider,
        )
        if transport is None:
            # Reuse verified trust material, not Item responses or authorization.
            # Like other long-lived clients, reload this client after CA rotation.
            context = ssl.create_default_context()
            context.set_alpn_protocols(["http/1.1"])
            context.post_handshake_auth = True
            transport = partial(get_json, tls_context=context)
        self.transport = transport

    def context(self, item_id):
        try:
            value = self.transport(
                f"{self.base_url}/internal/v1/items/{UUID(str(item_id))}/index-context",
                {"Authorization": f"Bearer {self.token_provider()}"},
            )
            if value is None:
                return None
            result = ItemContext.model_validate(value)
            if result.item_id != item_id:
                raise ValueError("Mismatched Item ID")
            return result
        except (ValueError, OSError) as exc:
            raise MembershipUnavailable("Invalid Item context") from exc


def make_item_client(settings):
    if not all(
        [
            settings.item_base_url,
            settings.machine_token_url,
            settings.machine_client_id,
            settings.machine_client_secret,
        ]
    ):
        return None
    return ItemClient(
        settings.item_base_url,
        ClientCredentials(
            settings.machine_token_url,
            settings.machine_client_id,
            settings.machine_client_secret.get_secret_value(),
            ["lostlink/items.index"],
        ),
    )
