"""Strict public contracts; owner and venue views are separate DTOs."""

from typing import Literal
from uuid import UUID

from lostlink_common.event_schemas import ClaimCancellationReason
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

ClaimState = Literal[
    "submitted",
    "approval_pending",
    "approved",
    "cancellation_pending",
    "handover_pending",
    "rejected",
    "cancelled",
    "handed_over",
]
RejectReason = Literal["answers_incorrect", "insufficient_detail", "duplicate", "other"]


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class AnswerInput(Input):
    question_id: UUID
    version: int = Field(ge=1)
    answer: str = Field(min_length=1, max_length=1000)


class ContactInput(Input):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    phone: str | None = Field(default=None, min_length=3, max_length=32)

    @field_validator("phone")
    @classmethod
    def phone_characters(cls, value):
        if value is not None and any(c not in "+0123456789 -()" for c in value):
            raise ValueError("Phone may contain digits, spaces, +, -, ( and ) only")
        return value


class NewClaim(Input):
    item_id: UUID
    answers: list[AnswerInput] = Field(min_length=1, max_length=3)
    contact: ContactInput | None = None

    @model_validator(mode="after")
    def unique_questions(self):
        if len({answer.question_id for answer in self.answers}) != len(self.answers):
            raise ValueError("Answer each question once")
        return self


class Rejection(Input):
    reason_code: RejectReason


class Cancellation(Input):
    reason_code: ClaimCancellationReason


class HandoverScan(Input):
    token: str = Field(min_length=20, max_length=200)


class PublicQuestion(BaseModel):
    id: UUID
    prompt: str
    version: int


class ClaimQuestions(BaseModel):
    item_id: UUID
    title: str
    state: str
    item_version: int
    questions: list[PublicQuestion]


class AnswerView(BaseModel):
    question_id: UUID
    question_version: int
    prompt: str
    answer: str


class ClaimView(BaseModel):
    id: UUID
    item_id: UUID
    venue_id: UUID
    item_title: str
    item_version: int
    state: ClaimState
    version: int
    decision_code: str | None
    created_at: AwareDatetime
    approved_at: AwareDatetime | None
    closed_at: AwareDatetime | None
    active_operation_id: UUID | None


class ClaimDetail(ClaimView):
    answers: list[AnswerView]


class VenueClaimView(ClaimView):
    owner_sub: str
    reviewed_by: str | None


class VenueClaimDetail(VenueClaimView):
    answers: list[AnswerView]


class ClaimsPage(BaseModel):
    items: list[ClaimView]
    limit: int
    offset: int
    has_more: bool


class VenueClaimsPage(BaseModel):
    items: list[VenueClaimView]
    limit: int
    offset: int
    has_more: bool


class ContactView(BaseModel):
    name: str | None
    phone: str | None
    email: str


class AcceptedOperation(BaseModel):
    operation_id: UUID
    claim_id: UUID
    state: ClaimState
    version: int


class OperationView(BaseModel):
    id: UUID
    claim_id: UUID
    kind: Literal["approve", "cancel", "handover"]
    state: Literal["pending", "leased", "completed", "needs_attention"]
    claim_state: ClaimState
    attempts: int
    result_code: str | None
    last_error_code: str | None
    created_at: AwareDatetime
    completed_at: AwareDatetime | None


class HandoverTokenView(BaseModel):
    token_id: UUID
    claim_id: UUID
    qr_payload: str
    expires_at: AwareDatetime
