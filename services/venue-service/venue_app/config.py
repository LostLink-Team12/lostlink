"""Separate credentials for tenant, platform, worker and token-trigger processes."""

from typing import Literal

from lostlink_common.config import Settings
from pydantic import Field, SecretStr, field_validator, model_validator
from sqlalchemy.engine import make_url


class VenueSettings(Settings):
    service_name: str = "venue-service"
    process_mode: Literal["api", "worker", "trigger"] = "api"
    platform_database_url: SecretStr | None = None
    worker_database_url: SecretStr | None = None
    identity_database_url: SecretStr | None = None
    user_client_ids: list[str] = Field(default_factory=list)
    platform_client_ids: list[str] = Field(default_factory=list)
    member_check_client_ids: list[str] = Field(default_factory=list)
    identity_resolve_client_ids: list[str] = Field(default_factory=list)
    cognito_user_pool_id: str | None = None

    @model_validator(mode="after")
    def validate_configuration(self):
        if self.process_mode == "api":
            return Settings.validate_configuration(self)
        if bool(self.cognito_issuer) != bool(self.cognito_client_ids):
            raise ValueError("Configure COGNITO_ISSUER and COGNITO_CLIENT_IDS together")
        if self.environment in {"dev", "prod"}:
            required = (
                self.worker_database_url
                if self.process_mode == "worker"
                else self.identity_database_url
            )
            if not required or self.aws_endpoint_url:
                raise ValueError(
                    "Deployed worker/trigger requires its credential and AWS endpoints"
                )
        return self

    @field_validator("platform_database_url", "worker_database_url", "identity_database_url")
    @classmethod
    def database_driver(cls, value):
        return Settings.validate_database_url(value)

    @model_validator(mode="after")
    def separate_clients(self):
        if self.cognito_tenant_claim != "custom:tenant_id":
            raise ValueError("Venue token trigger uses the custom:tenant_id claim")
        human = set(self.user_client_ids) | set(self.platform_client_ids)
        machine = set(self.member_check_client_ids) | set(self.identity_resolve_client_ids)
        if human & machine:
            raise ValueError("Human and machine client allowlists must be disjoint")
        lists = [human, machine]
        if any(not client.strip() for group in lists for client in group):
            raise ValueError("Client allowlists cannot contain empty IDs")
        if self.cognito_client_ids and not (human | machine) <= set(self.cognito_client_ids):
            raise ValueError("Endpoint client allowlists must belong to COGNITO_CLIENT_IDS")
        if (
            self.process_mode == "api"
            and self.environment in {"dev", "prod"}
            and (
                not self.platform_database_url
                or not self.user_client_ids
                or not self.platform_client_ids
            )
        ):
            raise ValueError("Venue API requires platform credentials and human client allowlists")
        urls = [
            self.database_url,
            self.platform_database_url,
            self.worker_database_url,
            self.identity_database_url,
        ]
        users = [make_url(url.get_secret_value()).username for url in urls if url]
        if len(users) != len(set(users)):
            raise ValueError("Database access modes require separate login roles")
        return self
