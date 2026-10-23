"""Strict API contracts with no caller-selected tenant or system fields."""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

Role = Literal["venue_admin", "venue_staff"]
Status = Literal["active", "suspended"]


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class NewVenue(Input):
    name: str = Field(min_length=1, max_length=120)
    slug: str = Field(min_length=1, max_length=80, pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    initial_admin_sub: str = Field(min_length=1, max_length=128)


class NewMember(Input):
    user_sub: str = Field(min_length=1, max_length=128)
    role: Role


class MemberChange(Input):
    role: Role | None = None
    status: Status | None = None

    @model_validator(mode="after")
    def nonempty(self):
        if not self.model_fields_set or any(
            getattr(self, key) is None for key in self.model_fields_set
        ):
            raise ValueError("Supply at least one non-null change")
        return self


class VenueChange(Input):
    status: Status


class MemberCheck(Input):
    subject: str = Field(min_length=1, max_length=128)
    tenant_id: UUID
    allowed_roles: list[Role] = Field(min_length=1, max_length=2)


class IdentityResolve(Input):
    subject: str = Field(min_length=1, max_length=128)


class VenueView(BaseModel):
    id: UUID
    name: str
    slug: str
    status: Status
    version: int


class MemberView(BaseModel):
    id: UUID
    user_sub: str
    role: Role
    status: Status
    version: int
    identity_sync_status: Literal["pending"] | None = None
    identity_sync_operation_id: UUID | None = None


class OnboardingView(BaseModel):
    venue: VenueView
    initial_admin: MemberView
    identity_sync_status: Literal["pending"]


class MembersPage(BaseModel):
    items: list[MemberView]
    limit: int
    offset: int
    has_more: bool


class VenuesPage(BaseModel):
    items: list[VenueView]
    limit: int
    offset: int
    has_more: bool


class OperationView(BaseModel):
    id: UUID
    state: Literal["pending", "leased", "completed", "superseded", "failed"]
    attempts: int
    membership_version: int
    last_error_code: str | None


class MembershipCheckView(BaseModel):
    active: bool
    effective_role: Role | None
    membership_version: int | None
    venue_status: Status | None


class IdentityView(BaseModel):
    tenant_id: UUID | None
    role: Role | None
    membership_version: int | None


class PortalAccess(BaseModel):
    venue: VenueView | None
    can_manage_venue: bool
    can_manage_members: bool
    can_onboard_venues: bool
