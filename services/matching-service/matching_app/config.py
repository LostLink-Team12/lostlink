"""Separate workload credentials and an explicitly pinned offline CLIP checkpoint."""

from typing import Literal

from lostlink_common.config import Settings
from lostlink_common.membership import MembershipUnavailable
from pydantic import Field, SecretStr, field_validator, model_validator
from sqlalchemy.engine import make_url

from matching_app.clients import validate_url


class MatchingSettings(Settings):
    service_name: str = "matching-service"
    process_mode: Literal["api", "worker"] = "api"
    worker_mode: Literal["consume", "tasks", "outbox"] = "tasks"
    internal_database_url: SecretStr | None = None
    worker_database_url: SecretStr | None = None
    user_client_ids: list[str] = Field(default_factory=list)
    notification_client_ids: list[str] = Field(default_factory=list)
    item_base_url: str | None = None
    machine_token_url: str | None = None
    machine_client_id: str | None = None
    machine_client_secret: SecretStr | None = None
    item_photo_buckets: list[str] = Field(default_factory=list)
    clip_checkpoint: str | None = None
    clip_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    clip_device: Literal["cpu", "cuda"] = "cpu"
    # Calibrated on the 102 labelled D1 pairs (calibration workflow, issue #9). Reports with a
    # photo score much higher against fused item vectors than text-only reports, so each gets
    # its own cut-off at precision >= 0.8: text+photo 0.78 (P 0.85, R 1.00), text 0.63
    # (P 0.80, R 0.24). Re-run the workflow when the model or the labelled data changes.
    match_threshold: float = Field(default=0.78, ge=-1, le=1)
    match_threshold_text: float | None = Field(default=0.63, ge=-1, le=1)
    candidate_limit: int = Field(default=100, ge=20, le=100)

    def automatic_match_threshold(self, has_photo: bool) -> float:
        if not has_photo and self.match_threshold_text is not None:
            return self.match_threshold_text
        return self.match_threshold

    @field_validator("internal_database_url", "worker_database_url")
    @classmethod
    def database_driver(cls, value):
        return Settings.validate_database_url(value)

    @field_validator("item_base_url", "machine_token_url")
    @classmethod
    def endpoint(cls, value):
        if value:
            try:
                validate_url(value)
            except MembershipUnavailable as exc:
                raise ValueError("Configure a fixed HTTPS endpoint") from exc
        return value.rstrip("/") if value else value

    @model_validator(mode="after")
    def validate_configuration(self):
        if self.process_mode == "api":
            Settings.validate_configuration(self)
        elif self.environment in {"dev", "prod"}:
            if not self.worker_database_url or self.aws_endpoint_url:
                raise ValueError("Worker requires its own database credential and AWS endpoints")
        human, machine = set(self.user_client_ids), set(self.notification_client_ids)
        if human & machine or any(not x.strip() for x in human | machine):
            raise ValueError("Human and machine clients must be distinct nonempty identifiers")
        if self.cognito_client_ids and not (human | machine) <= set(self.cognito_client_ids):
            raise ValueError("Endpoint clients must be accepted by the verifier")
        urls = [self.database_url, self.internal_database_url, self.worker_database_url]
        users = [make_url(url.get_secret_value()).username for url in urls if url]
        if len(users) != len(set(users)):
            raise ValueError("Database access modes require distinct login roles")
        if bool(self.clip_checkpoint) != bool(self.clip_sha256):
            raise ValueError("Configure CLIP_CHECKPOINT and CLIP_SHA256 together")
        if self.environment in {"dev", "prod"} and (
            self.process_mode == "api" or self.worker_mode == "tasks"
        ):
            if not all(
                [
                    self.clip_checkpoint,
                    self.item_base_url,
                    self.machine_token_url,
                    self.machine_client_id,
                    self.machine_client_secret,
                ]
            ):
                raise ValueError("Configure CLIP and authenticated Item access")
            if self.process_mode == "api" and not (
                self.internal_database_url and self.user_client_ids
            ):
                raise ValueError("API requires internal database and human clients")
        return self
