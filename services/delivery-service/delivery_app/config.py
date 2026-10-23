"""API/worker credentials, owner and partner client allowlists and address encryption keys."""

from typing import Literal

from lostlink_common.config import Settings
from pydantic import Field, SecretStr, field_validator, model_validator
from sqlalchemy.engine import make_url

PARTNER_SCOPE = "lostlink/deliveries.update"


class DeliverySettings(Settings):
    service_name: str = "delivery-service"
    process_mode: Literal["api", "worker"] = "api"
    worker_mode: Literal["consume", "outbox"] | None = None
    worker_database_url: SecretStr | None = None
    # Owner App clients (human, owner role) and courier partners (machine clients).
    user_client_ids: list[str] = Field(default_factory=list)
    partner_client_ids: list[str] = Field(default_factory=list)
    # The delivery address is readable only by the owner and the authorized partner.
    field_encryption_keys: SecretStr | None = None
    field_encryption_active_key_id: str | None = None

    @field_validator("worker_database_url")
    @classmethod
    def database_driver(cls, value):
        return Settings.validate_database_url(value)

    @model_validator(mode="after")
    def validate_configuration(self):
        if self.process_mode == "api":
            Settings.validate_configuration(self)
        elif self.environment in {"dev", "prod"}:
            if not self.worker_database_url or self.aws_endpoint_url:
                raise ValueError("Worker requires its own database credential and AWS endpoints")
        humans, partners = set(self.user_client_ids), set(self.partner_client_ids)
        if humans & partners or any(not c.strip() for c in humans | partners):
            raise ValueError("Owner and partner clients must be distinct nonempty identifiers")
        if self.cognito_client_ids and not (humans | partners) <= set(self.cognito_client_ids):
            raise ValueError("Endpoint clients must be accepted by the token verifier")
        urls = [self.database_url, self.worker_database_url]
        users = [make_url(url.get_secret_value()).username for url in urls if url]
        if len(set(users)) != len(users):
            raise ValueError("Delivery access modes require distinct login roles")
        if bool(self.field_encryption_keys) != bool(self.field_encryption_active_key_id):
            raise ValueError("Configure encryption keys and the active key ID together")
        if (
            self.environment in {"dev", "prod"}
            and self.process_mode == "api"
            and not all([humans, partners, self.field_encryption_keys])
        ):
            raise ValueError("API requires owner and partner clients and address encryption keys")
        return self
