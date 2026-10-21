"""Strict public contracts; import row validation happens independently."""

import json
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    field_validator,
    model_validator,
)

State = Literal[
    "awaiting_upload",
    "processing",
    "processing_failed",
    "available",
    "reserved",
    "handed_over",
    "withdrawn",
]


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class VerificationQuestion(Input):
    prompt: str = Field(min_length=1, max_length=300)
    reference_answer: str = Field(min_length=1, max_length=1000)


class NewItem(Input):
    title: str = Field(min_length=1, max_length=120)
    description: str = Field(min_length=1, max_length=2000)
    category: str = Field(min_length=1, max_length=100)
    found_at: AwareDatetime
    public_location_hint: str | None = Field(default=None, max_length=300)
    verification_questions: list[VerificationQuestion] = Field(min_length=1, max_length=3)

    @field_validator("found_at")
    @classmethod
    def valid_time(cls, value):
        if value > datetime.now(UTC) + timedelta(minutes=5):
            raise ValueError("Found time cannot be more than five minutes in the future")
        return value


class ItemChange(Input):
    title: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, min_length=1, max_length=2000)
    category: str | None = Field(default=None, min_length=1, max_length=100)
    found_at: AwareDatetime | None = None
    public_location_hint: str | None = Field(default=None, max_length=300)
    verification_questions: list[VerificationQuestion] | None = Field(
        default=None, min_length=1, max_length=3
    )

    @model_validator(mode="after")
    def changes(self):
        if not self.model_fields_set or any(
            getattr(self, key) is None for key in self.model_fields_set - {"public_location_hint"}
        ):
            raise ValueError("Supply at least one valid change")
        if self.found_at:
            NewItem.valid_time(self.found_at)
        return self


class Withdrawal(Input):
    reason_code: Literal["incorrect_record", "disposed", "duplicate", "other"]


class UploadRequest(Input):
    content_type: Literal["image/jpeg", "image/png", "image/webp"]
    size: int = Field(ge=1, le=10 * 1024 * 1024, strict=True)


class ReserveRequest(Input):
    claim_id: UUID
    item_version: int = Field(ge=1)


class ReleaseRequest(Input):
    reason_code: Literal["claim_cancelled", "approval_aborted", "recovery"]


class HandoverRequest(Input):
    handover_id: UUID
    mode: Literal["counter", "courier"]


class ImportRequest(Input):
    batch_reference: str = Field(min_length=1, max_length=128)
    entries: list[dict[str, JsonValue]] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def references(self):
        references = [entry.get("client_reference") for entry in self.entries]
        if any(not isinstance(ref, str) or not 1 <= len(ref) <= 128 for ref in references):
            raise ValueError("Each row requires a client_reference of 1-128 characters")
        if len(set(references)) != len(references):
            raise ValueError("Batch references must be unique")
        if len(json.dumps(self.model_dump(), ensure_ascii=False).encode()) > 1024 * 1024:
            raise ValueError("Manifest exceeds the 1 MiB limit")
        return self


class RepairEntry(Input):
    entry_id: UUID
    item: NewItem


class ImportRepair(Input):
    entries: list[RepairEntry] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def unique(self):
        if len({entry.entry_id for entry in self.entries}) != len(self.entries):
            raise ValueError("An entry can only be repaired once per request")
        return self


class ItemView(BaseModel):
    id: UUID
    title: str
    description: str
    category: str
    found_at: AwareDatetime
    public_location_hint: str | None
    state: State
    item_version: int
    version: int
    photo_id: UUID | None
    active_upload_id: UUID | None


class QuestionView(BaseModel):
    id: UUID
    prompt: str
    reference_answer: str
    version: int


class ItemDetail(ItemView):
    verification_questions: list[QuestionView]


class ItemsPage(BaseModel):
    items: list[ItemView]
    limit: int
    offset: int
    has_more: bool


class ReservationView(BaseModel):
    claim_id: UUID
    item_id: UUID
    item_version: int
    state: Literal["held", "released", "committed"]
    handover_id: UUID | None
    mode: Literal["counter", "courier"] | None
    version: int


class PresignedUpload(BaseModel):
    url: str
    fields: dict[str, str]


class UploadView(BaseModel):
    upload_id: UUID
    item_id: UUID
    item_version: int
    version: int
    expires_at: AwareDatetime
    upload: PresignedUpload


class AcceptedOperation(BaseModel):
    operation_id: UUID
    item_id: UUID


class ProcessingView(ItemView):
    processing_correlation_id: UUID


class OperationView(BaseModel):
    id: UUID
    state: Literal["pending", "leased", "completed", "failed", "superseded", "needs_attention"]
    kind: Literal["photo", "import_entry", "reservation_watch"]
    attempts: int
    last_error_code: str | None


class ImportView(BaseModel):
    id: UUID
    state: Literal["pending", "completed", "completed_with_errors"]
    total: int
    accepted: int
    failed: int
    batch_reference: str


class ImportEntryView(BaseModel):
    id: UUID
    client_reference: str
    state: Literal["pending", "succeeded", "failed"]
    item_id: UUID | None
    error_code: str | None


class ImportStatus(BaseModel):
    job: ImportView
    items: list[ImportEntryView]
    limit: int
    offset: int
    has_more: bool


class PublicQuestion(BaseModel):
    id: UUID
    prompt: str
    version: int


class ClaimContext(BaseModel):
    item_id: UUID
    tenant_id: UUID
    item_version: int
    state: State
    title: str
    version: int
    questions: list[PublicQuestion]


class IndexContext(ItemView):
    item_id: UUID
    tenant_id: UUID
    revision: int
    processing_correlation_id: UUID | None
    photo_bucket: str | None
    photo_key: str | None
    photo_url: str | None
