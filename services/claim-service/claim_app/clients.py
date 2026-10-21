"""Fixed-origin Item calls with explicit outcomes; no redirects or caller-supplied URLs."""

import http.client
import json
from collections.abc import Callable
from urllib.parse import urlsplit
from uuid import UUID

from lostlink_common.membership import ClientCredentials, MembershipUnavailable
from pydantic import BaseModel, ConfigDict, Field, ValidationError

SCOPES = ["lostlink/members.check", "lostlink/items.context", "lostlink/items.reserve"]


class ItemUnavailable(Exception):
    """Transient: the outcome of a write may be unknown; query before retrying."""


class ItemRejected(Exception):
    def __init__(self, status: int, code: str):
        super().__init__(code)
        self.status, self.code = status, code


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


def https_json(method, url, headers, body=None):
    """Return (status, JSON object or None) with bounded I/O."""
    parsed = validate_url(url)
    connection = http.client.HTTPSConnection(parsed.hostname, parsed.port or 443, timeout=2)
    try:
        connection.connect()
        connection.sock.settimeout(5)
        payload = None if body is None else json.dumps(body).encode()
        connection.request(method, parsed.path or "/", body=payload, headers=headers)
        response = connection.getresponse()
        content = response.read(65537)
        if len(content) > 65536:
            raise ItemUnavailable("Item response is too large")
        value = json.loads(content) if content else None
        return response.status, value if isinstance(value, dict) else None
    except (OSError, http.client.HTTPException, ValueError) as exc:
        raise ItemUnavailable("Item is unavailable") from exc
    finally:
        connection.close()


class Question(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: UUID
    prompt: str = Field(min_length=1, max_length=300)
    version: int = Field(ge=1)


class ClaimContext(BaseModel):
    model_config = ConfigDict(extra="ignore")
    item_id: UUID
    tenant_id: UUID
    item_version: int = Field(ge=1)
    state: str
    title: str = Field(min_length=1, max_length=120)
    questions: list[Question]


class Reservation(BaseModel):
    model_config = ConfigDict(extra="ignore")
    claim_id: UUID
    item_id: UUID
    item_version: int
    state: str
    handover_id: UUID | None
    mode: str | None


class ItemClient:
    def __init__(self, base_url, token_provider: Callable[[], str], *, transport=https_json):
        validate_url(base_url)
        self.base_url, self.token_provider, self.transport = (
            base_url.rstrip("/"),
            token_provider,
            transport,
        )

    def call(self, method, path, *, key=None, body=None, model):
        try:
            headers = {
                "Authorization": f"Bearer {self.token_provider()}",
                "Content-Type": "application/json",
            }
        except MembershipUnavailable as exc:
            raise ItemUnavailable("Machine credentials are unavailable") from exc
        if key:
            headers["Idempotency-Key"] = key
        try:
            status, value = self.transport(method, f"{self.base_url}{path}", headers, body)
        except OSError as exc:
            raise ItemUnavailable("Item is unavailable") from exc
        if status == 200:
            try:
                return model.model_validate(value)
            except ValidationError as exc:
                raise ItemUnavailable("Invalid Item response") from exc
        code = ((value or {}).get("error") or {}).get("code") or "http_error"
        if status == 409 and code == "operation_in_progress":
            raise ItemUnavailable(code)
        if status in {404, 409, 412, 422}:
            raise ItemRejected(status, code)
        raise ItemUnavailable(code)

    def claim_context(self, item_id: UUID) -> ClaimContext:
        value = self.call("GET", f"/internal/v1/items/{item_id}/claim-context", model=ClaimContext)
        if value.item_id != item_id:
            raise ItemUnavailable("Mismatched Item response")
        return value

    def reserve(self, item_id: UUID, claim_id: UUID, item_version: int) -> Reservation:
        return self.checked(
            self.call(
                "POST",
                f"/internal/v1/items/{item_id}/reservations",
                key=f"claim:{claim_id}:reserve",
                body={"claim_id": str(claim_id), "item_version": item_version},
                model=Reservation,
            ),
            item_id,
            claim_id,
        )

    def reservation(self, item_id: UUID, claim_id: UUID) -> Reservation:
        return self.checked(
            self.call(
                "GET",
                f"/internal/v1/items/{item_id}/reservations/{claim_id}",
                model=Reservation,
            ),
            item_id,
            claim_id,
        )

    def release(self, item_id: UUID, claim_id: UUID, reason: str) -> Reservation:
        return self.checked(
            self.call(
                "POST",
                f"/internal/v1/items/{item_id}/reservations/{claim_id}/release",
                key=f"claim:{claim_id}:release",
                body={"reason_code": reason},
                model=Reservation,
            ),
            item_id,
            claim_id,
        )

    def commit_handover(
        self, item_id: UUID, claim_id: UUID, handover_id: UUID, mode: str
    ) -> Reservation:
        return self.checked(
            self.call(
                "POST",
                f"/internal/v1/items/{item_id}/reservations/{claim_id}/commit-handover",
                key=f"claim:{claim_id}:handover:{handover_id}",
                body={"handover_id": str(handover_id), "mode": mode},
                model=Reservation,
            ),
            item_id,
            claim_id,
        )

    @staticmethod
    def checked(value, item_id, claim_id):
        if value.item_id != item_id or value.claim_id != claim_id:
            raise ItemUnavailable("Mismatched Item response")
        return value


def make_token_provider(settings):
    if not all(
        [settings.machine_token_url, settings.machine_client_id, settings.machine_client_secret]
    ):
        return None
    return ClientCredentials(
        settings.machine_token_url,
        settings.machine_client_id,
        settings.machine_client_secret.get_secret_value(),
        SCOPES,
    )


def make_item_client(settings, token_provider):
    if not settings.item_base_url or token_provider is None:
        return None
    return ItemClient(settings.item_base_url, token_provider)
