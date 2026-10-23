"""Portal capabilities reuse verified identities and current membership checks."""

from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from lostlink_common.auth import Principal
from venue_app.config import VenueSettings
from venue_app.main import create_app
from venue_app.models import Venue

TENANT = uuid4()


def portal(
    principal,
    *,
    member_role="venue_admin",
    member_status="active",
    venue_status="active",
    database_available=True,
):
    venue = SimpleNamespace(id=TENANT, name="Campus", slug="campus", status=venue_status, version=1)
    member = SimpleNamespace(role=member_role, status=member_status)
    session = SimpleNamespace(
        scalar=lambda query: venue if query.column_descriptions[0]["entity"] is Venue else member
    )
    db = SimpleNamespace(context=Mock(return_value=nullcontext(session)))
    verifier = SimpleNamespace(verify=lambda _: principal)
    app = create_app(
        VenueSettings(_env_file=None, user_client_ids=["web"], platform_client_ids=["ops"]),
        token_verifier=verifier,
        database=db if database_available else None,
    )
    return TestClient(app), db


def test_owner_receives_no_capabilities_without_querying_any_venue():
    with portal(Principal(subject="owner", client_id="web", roles={"owner"}))[0] as api:
        response = api.get("/v1/session", headers={"Authorization": "Bearer token"})
        assert response.json() == {
            "venue": None,
            "can_manage_venue": False,
            "can_manage_members": False,
            "can_onboard_venues": False,
        }
        assert response.headers["cache-control"] == "no-store"
        api.app.state.database.context.assert_not_called()
        assert api.get("/v1/session").status_code == 401


@pytest.mark.parametrize(
    "token_roles,member_role,status,venue_status,staff,admin",
    [
        ({"venue_admin"}, "venue_admin", "active", "active", True, True),
        ({"venue_staff"}, "venue_staff", "active", "active", True, False),
        ({"venue_admin"}, "venue_admin", "suspended", "active", False, False),
        ({"venue_admin"}, "venue_admin", "active", "suspended", False, False),
        ({"venue_admin"}, "venue_staff", "active", "active", False, False),
        ({"venue_admin", "venue_staff"}, "venue_staff", "active", "active", True, False),
    ],
)
def test_portal_uses_current_membership_instead_of_stale_role_claims(
    token_roles, member_role, status, venue_status, staff, admin
):
    principal = Principal(subject="staff", client_id="web", roles=token_roles, tenant_id=TENANT)
    with portal(
        principal, member_role=member_role, member_status=status, venue_status=venue_status
    )[0] as api:
        response = api.get("/v1/session", headers={"Authorization": "Bearer token"})
    assert response.status_code == 200
    assert response.json()["can_manage_venue"] is staff
    assert response.json()["can_manage_members"] is admin
    assert response.json()["can_onboard_venues"] is False


@pytest.mark.parametrize(
    "client,expected,status", [("ops", True, 200), ("web", False, 200), ("machine", None, 403)]
)
def test_platform_capability_requires_the_platform_client_and_role(client, expected, status):
    principal = Principal(subject="platform", client_id=client, roles={"platform_admin"})
    with portal(principal)[0] as api:
        response = api.get("/v1/session", headers={"Authorization": "Bearer token"})
        api.app.state.database.context.assert_not_called()
    assert response.status_code == status
    if status == 200:
        assert response.json()["can_onboard_venues"] is expected
        assert response.json()["can_manage_venue"] is False


def test_portal_does_not_grant_permissions_when_membership_database_is_unavailable():
    principal = Principal(subject="staff", client_id="web", roles={"venue_admin"}, tenant_id=TENANT)
    with portal(principal, database_available=False)[0] as api:
        response = api.get("/v1/session", headers={"Authorization": "Bearer token"})
    assert response.status_code == 503
