"""Independent API/internal/worker credentials and trusted service endpoints."""

from typing import Literal
from urllib.parse import urlsplit

from lostlink_common.config import Settings
from pydantic import Field, SecretStr, field_validator, model_validator
from sqlalchemy.engine import make_url


class ItemSettings(Settings):
    service_name: str = "item-service"
    process_mode: Literal["api", "worker"] = "api"
    internal_database_url: SecretStr | None = None
    worker_database_url: SecretStr | None = None
    user_client_ids: list[str] = Field(default_factory=list)
    claim_client_ids: list[str] = Field(default_factory=list)
    index_client_ids: list[str] = Field(default_factory=list)
    venue_base_url: str | None = None
    machine_token_url: str | None = None
    machine_client_id: str | None = None
    machine_client_secret: SecretStr | None = None
    photo_bucket: str | None = Field(default=None, min_length=3, max_length=63)

    @field_validator("internal_database_url", "worker_database_url")
    @classmethod
    def database_driver(cls, value):
        return Settings.validate_database_url(value)

    @field_validator("venue_base_url", "machine_token_url")
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
                raise ValueError("Internal identity/service endpoints must be fixed HTTPS URLs")
        return value.rstrip("/") if value else value

    @model_validator(mode="after")
    def validate_configuration(self):
        if self.process_mode == "api":
            Settings.validate_configuration(self)
        elif self.environment in {"dev", "prod"}:
            if not self.worker_database_url or self.aws_endpoint_url:
                raise ValueError("Worker requires its own database credential and AWS endpoints")
        human, machine = (
            set(self.user_client_ids),
            set(self.claim_client_ids + self.index_client_ids),
        )
        if human & machine or any(not client.strip() for client in human | machine):
            raise ValueError("Human and machine allowlists must be disjoint and nonempty")
        if self.cognito_client_ids and not (human | machine) <= set(self.cognito_client_ids):
            raise ValueError("Endpoint clients must be accepted by the token verifier")
        urls = [self.database_url, self.internal_database_url, self.worker_database_url]
        users = [make_url(url.get_secret_value()).username for url in urls if url]
        if len(set(users)) != len(users):
            raise ValueError("Item access modes require distinct login roles")
        if self.environment in {"dev", "prod"} and not self.photo_bucket:
            raise ValueError("PHOTO_BUCKET is required")
        if self.process_mode == "api" and self.environment in {"dev", "prod"}:
            if not all(
                [
                    self.internal_database_url,
                    self.user_client_ids,
                    self.venue_base_url,
                    self.machine_token_url,
                    self.machine_client_id,
                    self.machine_client_secret,
                ]
            ):
                raise ValueError("API requires internal DB and Venue machine credentials")
        return self
