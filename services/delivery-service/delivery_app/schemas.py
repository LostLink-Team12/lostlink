"""Request and response models; unknown request fields are rejected."""

from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

ShipmentStateName = Literal[
    "awaiting_pickup",
    "picked_up",
    "in_transit",
    "out_for_delivery",
    "delivery_failed",
    "delivered",
    "returned_to_venue",
    "cancelled",
]


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class DeliveryAddress(Input):
    recipient_name: str = Field(min_length=1, max_length=100)
    address_line: str = Field(min_length=5, max_length=300)
    postal_code: str = Field(pattern=r"^[0-9]{6}$")
    phone: str = Field(pattern=r"^\+?[0-9 ]{8,16}$")


class DeliveryAuthorization(Input):
    claim_id: UUID
    partner_id: str = Field(min_length=1, max_length=128)
    delivery_address: DeliveryAddress


class PartnerUpdate(Input):
    # Pickup comes only from the venue's QR scan (claim.handed_over), never from a partner.
    state: Literal[
        "in_transit", "out_for_delivery", "delivered", "delivery_failed", "returned_to_venue"
    ]
    note: str | None = Field(default=None, max_length=200)


class StatusEntry(BaseModel):
    from_state: ShipmentStateName | None
    to_state: ShipmentStateName
    actor: Literal["system", "partner", "owner", "venue"]
    version: int
    note: str | None
    occurred_at: AwareDatetime


class AddressView(BaseModel):
    recipient_name: str
    address_line: str
    postal_code: str
    phone: str


class ShipmentView(BaseModel):
    id: UUID
    claim_id: UUID
    item_id: UUID
    partner_id: str
    state: ShipmentStateName
    version: int
    delivery_attempts: int
    terminal: bool
    created_at: AwareDatetime
    updated_at: AwareDatetime
    history: list[StatusEntry] | None = None
    delivery_address: AddressView | None = None


class ShipmentsPage(BaseModel):
    items: list[ShipmentView]
    limit: int
    offset: int
    has_more: bool
