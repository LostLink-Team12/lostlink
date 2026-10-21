"""Owner and operator views: no addresses, message bodies or provider error text."""

from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict

State = Literal[
    "pending", "sending", "sent", "retry_wait", "failed", "delivery_unknown", "suppressed"
]


class NotificationView(BaseModel):
    id: UUID
    match_id: UUID
    report_id: UUID
    item_id: UUID
    channel: Literal["email"]
    state: State
    reason_code: str | None
    created_at: AwareDatetime
    sent_at: AwareDatetime | None


class NotificationsPage(BaseModel):
    items: list[NotificationView]
    limit: int
    offset: int
    has_more: bool


class AdminNotificationView(NotificationView):
    tenant_id: UUID
    template_version: str
    attempts: int
    job_state: Literal["pending", "leased", "done", "stopped"]
    last_error_code: str | None


class AdminNotificationsPage(BaseModel):
    items: list[AdminNotificationView]
    limit: int
    offset: int
    has_more: bool


class NotificationStats(BaseModel):
    counts: dict[str, int]
    oldest_pending_seconds: float | None


class RetryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason_code: Literal["dependency_restored", "provider_confirmed_not_sent", "owner_requested"]
    acknowledge_duplicate_risk: bool = False
