"""Requests never accept owner IDs, tenant IDs or caller-supplied vectors."""

from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator
from pydantic_core import PydanticCustomError

MAX_BASE64 = 4 * ((5 * 1024 * 1024 + 2) // 3)


class Filters(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    category: str | None = Field(default=None, min_length=1, max_length=100)
    venue_id: UUID | None = None
    found_from: AwareDatetime | None = None
    found_until: AwareDatetime | None = None

    @model_validator(mode="after")
    def date_range(self):
        if self.found_from and self.found_until and self.found_from > self.found_until:
            raise PydanticCustomError(
                "invalid_date_range", "found_from must not exceed found_until"
            )
        return self


class Search(Filters):
    text: str | None = Field(default=None, min_length=1, max_length=2000)
    photo_base64: str | None = Field(default=None, min_length=1, max_length=MAX_BASE64)
    limit: int = Field(default=20, ge=1, le=100)
    min_score: float = Field(default=0.0, ge=-1, le=1, allow_inf_nan=False)

    @model_validator(mode="after")
    def query_required(self):
        if not self.text and not self.photo_base64:
            raise ValueError("Text or a photo is required")
        return self


class NewReport(Filters):
    description: str = Field(min_length=1, max_length=2000)
    photo_base64: str | None = Field(default=None, min_length=1, max_length=MAX_BASE64)


class CloseReport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(default="no_longer_needed", pattern=r"^(found|no_longer_needed)$")


class ReportView(BaseModel):
    id: UUID
    description: str
    category: str | None
    venue_id: UUID | None
    found_from: AwareDatetime | None
    found_until: AwareDatetime | None
    has_photo: bool
    state: str
    version: int
    created_at: AwareDatetime


class ReportsPage(BaseModel):
    items: list[ReportView]
    limit: int
    offset: int
    has_more: bool


class SearchHit(BaseModel):
    item_id: UUID
    tenant_id: UUID
    item_version: int
    title: str
    description: str
    category: str
    found_at: AwareDatetime
    public_location_hint: str | None
    photo_url: str | None
    score: float


class SearchResults(BaseModel):
    items: list[SearchHit]
    limit: int
    candidate_limit: int
    model_version: str
    potentially_truncated: bool


class MatchView(BaseModel):
    id: UUID
    report_id: UUID
    item: SearchHit
    created_at: AwareDatetime


class MatchesPage(BaseModel):
    items: list[MatchView]
    limit: int
    offset: int
    has_more: bool


class NotificationContext(BaseModel):
    match_id: UUID
    owner_id: str
    report_id: UUID
    item_id: UUID
    tenant_id: UUID
    active: bool
    item_title: str | None
    venue_name: str | None


class ItemPhoto(BaseModel):
    item_id: UUID
    photo_url: str
