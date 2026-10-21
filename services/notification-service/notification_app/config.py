"""API/admin/worker credentials, trusted endpoints and the email sender configuration."""

from typing import Literal
from urllib.parse import urlsplit

from lostlink_common.config import Settings
from pydantic import Field, SecretStr, field_validator, model_validator
from sqlalchemy.engine import make_url


def https_url(value, *, allow_path=False):
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or (parsed.path not in {"", "/"} and not allow_path)
    ):
        raise ValueError("Use a fixed HTTPS origin")
    return value.rstrip("/")


class NotificationSettings(Settings):
    service_name: str = "notification-service"
    process_mode: Literal["api", "worker"] = "api"
    admin_database_url: SecretStr | None = None
    worker_database_url: SecretStr | None = None
    user_client_ids: list[str] = Field(default_factory=list)
    operator_client_ids: list[str] = Field(default_factory=list)
    matching_base_url: str | None = None
    machine_token_url: str | None = None
    machine_client_id: str | None = None
    machine_client_secret: SecretStr | None = None
    cognito_user_pool_id: str | None = None
    owner_app_base_url: str | None = None
    email_sender: Literal["ses", "capture"] = "ses"
    ses_from_address: str | None = Field(default=None, max_length=254)
    ses_configuration_set: str | None = Field(default=None, max_length=64)

    @field_validator("admin_database_url", "worker_database_url")
    @classmethod
    def database_driver(cls, value):
        return Settings.validate_database_url(value)

    @field_validator("matching_base_url", "machine_token_url")
    @classmethod
    def trusted_url(cls, value):
        return https_url(value, allow_path=True) if value else value

    @field_validator("owner_app_base_url")
    @classmethod
    def owner_app(cls, value):
        return https_url(value, allow_path=True) if value else value

    @field_validator("ses_from_address")
    @classmethod
    def sender_address(cls, value):
        if value is not None and (
            value.count("@") != 1 or any(c.isspace() or ord(c) < 32 for c in value)
        ):
            raise ValueError("SES_FROM_ADDRESS must be a single verified email address")
        return value

    @model_validator(mode="after")
    def validate_configuration(self):
        if self.process_mode == "api":
            Settings.validate_configuration(self)
        elif self.environment in {"dev", "prod"}:
            if not self.worker_database_url or self.aws_endpoint_url:
                raise ValueError("Worker requires its own database credential and AWS endpoints")
        human, operators = set(self.user_client_ids), set(self.operator_client_ids)
        if human & operators or any(not x.strip() for x in human | operators):
            raise ValueError("Owner and operator clients must be distinct nonempty identifiers")
        if self.cognito_client_ids and not (human | operators) <= set(self.cognito_client_ids):
            raise ValueError("Endpoint clients must be accepted by the token verifier")
        urls = [self.database_url, self.admin_database_url, self.worker_database_url]
        users = [make_url(url.get_secret_value()).username for url in urls if url]
        if len(set(users)) != len(users):
            raise ValueError("Notification access modes require distinct login roles")
        if self.environment in {"dev", "prod"}:
            if self.email_sender != "ses":
                raise ValueError("The capture sender is restricted to local/test environments")
            if self.process_mode == "worker" and not all(
                [
                    self.matching_base_url,
                    self.machine_token_url,
                    self.machine_client_id,
                    self.machine_client_secret,
                    self.cognito_user_pool_id,
                    self.owner_app_base_url,
                    self.ses_from_address,
                ]
            ):
                raise ValueError("Sender requires Matching, identity, Owner App and SES settings")
            if self.process_mode == "api" and not self.user_client_ids:
                raise ValueError("API requires owner client IDs")
        return self
