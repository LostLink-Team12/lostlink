"""Public item timestamps distinguish registration from discovery."""

from datetime import UTC, datetime
from uuid import uuid4

from item_app.models import Item
from item_app.schemas import ItemView
from item_app.service import item_view


def test_item_view_exposes_real_registration_and_found_times():
    registered = datetime(2026, 10, 24, 10, tzinfo=UTC)
    found = datetime(2026, 10, 23, 9, tzinfo=UTC)
    item = Item(
        id=uuid4(),
        title="Mouse",
        description="Black mouse",
        category="Mouse",
        found_at=found,
        created_at=registered,
        public_location_hint=None,
        state="available",
        item_version=1,
        version=1,
        photo_id=None,
        active_upload_id=None,
    )
    payload = item_view(item)
    view = ItemView.model_validate(payload)
    assert view.created_at == registered and view.found_at == found
    assert payload["created_at"] == registered.isoformat()
