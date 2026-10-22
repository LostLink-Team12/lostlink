"""API/worker credentials, trusted service endpoints and secret-backed keys."""

import base64
import binascii
from typing import Literal
from urllib.parse import urlsplit

from lostlink_common.config import Settings
from pydantic import Field, SecretStr, field_validator, model_validator
from sqlalchemy.engine import make_url


class ClaimSettings(Settings):
    service_name: str = "claim-service"
    process_mode: Literal["api", "worker"] = "api"
    worker_mode: Literal["tasks", "outbox"] = "tasks"
    worker_database_url: SecretStr | None = None
    user_client_ids: list[str] = Field(default_factory=list)
    venue_base_url: str | None = None
    item_base_url: str | None = None
    machine_token_url: str | None = None
    machine_client_id: str | None = None
    machine_client_secret: SecretStr | None = None
    cognito_user_pool_id: str | None = None
    field_encryption_keys: SecretStr | None = None
    field_encryption_active_key_id: str | None = None
    handover_token_key: SecretStr | None = None
    handover_token_hours: int = Field(default=24, ge=1, le=72)
    operation_deadline_minutes: int = Field(default=15, ge=1, le=120)

    @field_validator("worker_database_url")
    @classmethod
    def database_driver(cls, value):
        return Settings.validate_database_url(value)

    @field_validator("venue_base_url", "item_base_url", "machine_token_url")
    @classmethod
    def trusted_url(cls, value):
        if value is not None:
            parsed = urlsplit(value)
            if (
                parsed.scheme != "https"
                or not parsed.hostname
                or parsed.username
                or parsed.password
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError("Internal service endpoints must be fixed HTTPS URLs")
        return value.rstrip("/") if value else value

    @field_validator("handover_token_key")
    @classmethod
    def token_key(cls, value):
        if value is not None:
            try:
                raw = base64.b64decode(value.get_secret_value(), validate=True)
            except (binascii.Error, ValueError) as exc:
                raise ValueError("HANDOVER_TOKEN_KEY must be base64") from exc
            if len(raw) < 32:
                raise ValueError("HANDOVER_TOKEN_KEY must decode to at least 32 bytes")
        return value

    def token_key_bytes(self) -> bytes | None:
        if self.handover_token_key is None:
            return None
        return base64.b64decode(self.handover_token_key.get_secret_value())

    @model_validator(mode="after")
    def validate_configuration(self):
        if self.process_mode == "api":
            Settings.validate_configuration(self)
        elif self.environment in {"dev", "prod"}:
            if not self.worker_database_url or self.aws_endpoint_url:
                raise ValueError("Worker requires its own database credential and AWS endpoints")
        if any(not client.strip() for client in self.user_client_ids):
            raise ValueError("User client IDs must not be empty")
        if self.cognito_client_ids and not set(self.user_client_ids) <= set(
            self.cognito_client_ids
        ):
            raise ValueError("Endpoint clients must be accepted by the token verifier")
        urls = [self.database_url, self.worker_database_url]
        users = [make_url(url.get_secret_value()).username for url in urls if url]
        if len(set(users)) != len(users):
            raise ValueError("Claim access modes require distinct login roles")
        if bool(self.field_encryption_keys) != bool(self.field_encryption_active_key_id):
            raise ValueError("Configure encryption keys and the active key ID together")
        if self.environment in {"dev", "prod"} and (
            self.process_mode == "api" or self.worker_mode == "tasks"
        ):
            required = [
                self.item_base_url,
                self.machine_token_url,
                self.machine_client_id,
                self.machine_client_secret,
                self.field_encryption_keys,
                self.handover_token_key,
            ]
            if self.process_mode == "api":
                required += [self.user_client_ids, self.venue_base_url, self.cognito_user_pool_id]
            if not all(required):
                raise ValueError("Claim requires Item, Venue, identity and key configuration")
        return self
