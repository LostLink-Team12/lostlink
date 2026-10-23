"""Photo renewal rechecks authoritative visibility without inference or database reads."""

from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from lostlink_common.auth import InvalidToken, Principal
from lostlink_common.membership import MembershipUnavailable
from matching_app.config import MatchingSettings
from matching_app.main import create_app


class Verifier:
    def verify(self, token):
        if token == "invalid":
            raise InvalidToken()
        return Principal(
            subject="owner",
            client_id="other" if token == "foreign" else "web",
            roles={"venue_staff"} if token == "staff" else {"owner"},
        )


def client(item):
    return TestClient(
        create_app(
            MatchingSettings(_env_file=None, user_client_ids=["web"]),
            token_verifier=Verifier(),
            item_client=item,
            encoder=SimpleNamespace(
                encode=Mock(side_effect=AssertionError("Unexpected inference"))
            ),
        )
    )


@pytest.mark.parametrize(
    "token,status", [(None, 401), ("invalid", 401), ("staff", 403), ("foreign", 403)]
)
def test_photo_renewal_requires_an_owner_from_an_allowed_client(token, status):
    item = SimpleNamespace(context=Mock())
    with client(item) as api:
        response = api.get(
            f"/v1/items/{uuid4()}/photo",
            headers={"Authorization": f"Bearer {token}"} if token else {},
        )
    assert response.status_code == status
    item.context.assert_not_called()


@pytest.mark.parametrize("state", ["available", "reserved", "handed_over", "deleted", None])
def test_photo_renewal_returns_only_current_available_photo_and_no_private_context(state):
    item_id = uuid4()
    context = (
        SimpleNamespace(
            state=state, photo_url="https://photos.invalid/fresh", photo_key="private-key"
        )
        if state
        else None
    )
    item = SimpleNamespace(context=Mock(return_value=context))
    with client(item) as api:
        response = api.get(f"/v1/items/{item_id}/photo", headers={"Authorization": "Bearer owner"})
    item.context.assert_called_once_with(item_id)
    if state == "available":
        assert response.status_code == 200
        assert response.json() == {
            "item_id": str(item_id),
            "photo_url": "https://photos.invalid/fresh",
        }
        assert response.headers["cache-control"] == "no-store"
    else:
        assert response.status_code == 404
        assert "photo_url" not in response.text and "private-key" not in response.text


def test_photo_renewal_fails_closed_when_item_service_is_unavailable():
    item = SimpleNamespace(context=Mock(side_effect=MembershipUnavailable()))
    with client(item) as api:
        response = api.get(f"/v1/items/{uuid4()}/photo", headers={"Authorization": "Bearer owner"})
    assert response.status_code == 503
