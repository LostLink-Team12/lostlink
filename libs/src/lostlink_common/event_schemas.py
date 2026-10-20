"""Version-one event payloads shared by producers and consumers."""

from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field


class Payload(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ItemRegistered(Payload):
    item_id: UUID
    item_version: int = Field(ge=1)
    photo_bucket: str = Field(min_length=1, max_length=63)
    photo_key: str = Field(min_length=1, max_length=1024)
    description: str = Field(min_length=1, max_length=2000)
    category: str = Field(min_length=1, max_length=100)
    found_at: AwareDatetime


class ItemEmbedded(Payload):
    item_id: UUID
    item_version: int = Field(ge=1)
    model_version: str = Field(min_length=1, max_length=100)
    dimensions: Literal[512] = 512


class LostReportCreated(Payload):
    report_id: UUID
    owner_id: str = Field(min_length=1, max_length=128)


class MatchFound(Payload):
    match_id: UUID
    report_id: UUID
    item_id: UUID
    owner_id: str = Field(min_length=1, max_length=128)
    score: float = Field(ge=-1, le=1, allow_inf_nan=False)


class ClaimApproved(Payload):
    claim_id: UUID
    item_id: UUID
    owner_id: str = Field(min_length=1, max_length=128)


class VenueMembershipChanged(Payload):
    membership_id: UUID
    user_sub: str = Field(min_length=1, max_length=128)
    version: int = Field(ge=1)
    status: Literal["active", "suspended"]


EVENT_SCHEMAS: dict[str, tuple[type[Payload], str, bool]] = {
    "item.registered": (ItemRegistered, "item-service", True),
    "item.embedded": (ItemEmbedded, "embedding-worker", True),
    "lost_report.created": (LostReportCreated, "matching-service", False),
    "match.found": (MatchFound, "matching-service", True),
    "claim.approved": (ClaimApproved, "claim-service", True),
    "venue.membership_changed": (VenueMembershipChanged, "venue-service", True),
}
