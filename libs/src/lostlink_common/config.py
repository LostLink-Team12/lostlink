"""Environment-based configuration with no authentication bypass."""

from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    service_name: str = Field(default="service-template", pattern=r"^[a-z][a-z0-9-]{0,62}$")
    environment: Literal["local", "test", "dev", "prod"] = "local"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    database_url: SecretStr | None = None
    db_pool_size: int = Field(default=5, ge=1, le=50)
    db_max_overflow: int = Field(default=5, ge=0, le=50)
    db_statement_timeout_ms: int = Field(default=5000, ge=100, le=60000)
    cognito_issuer: str | None = None
    cognito_client_ids: list[str] = Field(default_factory=list)
    cognito_audience: str | None = None
    cognito_tenant_claim: str = "custom:tenant_id"
    jwt_leeway_seconds: int = Field(default=0, ge=0, le=60)
    aws_region: str = "ap-southeast-1"
    aws_endpoint_url: str | None = None
    event_topic_arn: str | None = None
    event_queue_url: str | None = None

    @field_validator("cognito_issuer")
    @classmethod
    def validate_issuer(cls, value: str | None) -> str | None:
        if value is None:
            return value
        parsed = urlsplit(value)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("COGNITO_ISSUER must be an HTTPS issuer URL")
        return value.rstrip("/")

    @field_validator("cognito_client_ids")
    @classmethod
    def validate_client_ids(cls, value: list[str]) -> list[str]:
        if any(not client_id.strip() for client_id in value):
            raise ValueError("Cognito client IDs must not be empty")
        return value

    @field_validator("database_url")
    @classmethod
    def validate_database_url(cls, value: SecretStr | None) -> SecretStr | None:
        if value and make_url(value.get_secret_value()).drivername != "postgresql+psycopg":
            raise ValueError("DATABASE_URL must use postgresql+psycopg")
        return value

    @model_validator(mode="after")
    def validate_configuration(self) -> "Settings":
        if bool(self.cognito_issuer) != bool(self.cognito_client_ids):
            raise ValueError("Configure COGNITO_ISSUER and COGNITO_CLIENT_IDS together")
        if self.environment in {"dev", "prod"}:
            if not self.database_url or not self.cognito_issuer:
                raise ValueError("Deployed services require database and Cognito configuration")
            if self.aws_endpoint_url:
                raise ValueError("AWS endpoint overrides are restricted to local/test environments")
        return self
