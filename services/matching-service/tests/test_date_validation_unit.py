"""Date range errors retain a stable, sanitized reason for every matching form."""

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from lostlink_common.auth import Principal
from matching_app.config import MatchingSettings
from matching_app.main import create_app


@pytest.mark.parametrize(
    "path,description", [("/v1/search", "text"), ("/v1/lost-reports", "description")]
)
def test_reversed_date_range_has_a_stable_public_error_without_input_values(path, description):
    verifier = SimpleNamespace(
        verify=lambda _: Principal(subject="owner", client_id="web", roles={"owner"})
    )
    with TestClient(
        create_app(
            MatchingSettings(_env_file=None, user_client_ids=["web"]), token_verifier=verifier
        )
    ) as client:
        response = client.post(
            path,
            json={
                description: "private search text",
                "found_from": "2026-10-24T10:00:00Z",
                "found_until": "2026-10-23T10:00:00Z",
            },
            headers={"Authorization": "Bearer token"},
        )
    assert response.status_code == 422
    assert response.json()["error"]["details"] == [
        {"location": ["body"], "type": "invalid_date_range"}
    ]
    assert "private search text" not in response.text
