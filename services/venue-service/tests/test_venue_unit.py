from copy import deepcopy
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from lostlink_common.auth import Principal
from lostlink_common.events import DomainEvent
from pydantic import ValidationError
from venue_app.config import VenueSettings
from venue_app.main import create_app
from venue_app.token_trigger import customize_token
from venue_app.workers import CognitoSessionRevoker, IdentityAccountMissing


def trigger_event():
    return {
        "version": "2",
        "userPoolId": "pool",
        "triggerSource": "TokenGeneration_Authentication",
        "callerContext": {"clientId": "web"},
        "request": {
            "userAttributes": {"sub": "user", "custom:tenant_id": str(uuid4())},
            "groupConfiguration": {
                "groupsToOverride": ["venue_admin", "unrelated", "platform_admin"]
            },
        },
        "response": {},
    }


@pytest.mark.parametrize("role", ["venue_staff", "venue_admin", None])
def test_trigger_uses_authoritative_membership_and_preserves_unrelated_groups(role):
    tenant = str(uuid4()) if role else None
    event = customize_token(
        trigger_event(),
        lambda _: {"tenant_id": tenant, "role": role},
        user_pool_id="pool",
        user_client_ids=["web"],
    )
    value = event["response"]["claimsAndScopeOverrideDetails"]
    groups = value["groupOverrideDetails"]["groupsToOverride"]
    assert "owner" in groups and "unrelated" in groups and "platform_admin" in groups
    assert ({"venue_admin", "venue_staff"} & set(groups)) == ({role} if role else set())
    access = value["accessTokenGeneration"]
    assert access["claimsToAddOrOverride"] == ({"custom:tenant_id": tenant} if role else {})
    assert access["claimsToSuppress"] == ([] if role else ["custom:tenant_id"])


def test_trigger_failures_do_not_issue_stale_permissions():
    def unavailable(_):
        raise OSError("Database unavailable")

    with pytest.raises(OSError):
        customize_token(trigger_event(), unavailable, user_pool_id="pool", user_client_ids=["web"])
    event = trigger_event()
    event["version"] = "1"
    with pytest.raises(ValueError):
        customize_token(event, unavailable, user_pool_id="pool", user_client_ids=["web"])
    event["version"] = "2"
    with pytest.raises(ValueError):
        customize_token(event, unavailable, user_pool_id="other", user_client_ids=["web"])


def test_machine_trigger_does_not_resolve_a_human_or_recurse():
    event = {"version": "3", "triggerSource": "TokenGeneration_ClientCredentials"}
    original = deepcopy(event)

    def resolver(_):
        pytest.fail("A machine trigger must not query memberships")

    assert customize_token(event, resolver, user_pool_id="pool", user_client_ids=[]) == original


def test_cognito_revoker_resolves_exact_sub_and_never_changes_groups():
    class Cognito:
        def list_users(self, **kwargs):
            assert kwargs["Filter"] == 'sub = "user"'
            return {
                "Users": [
                    {"Username": "login-name", "Attributes": [{"Name": "sub", "Value": "user"}]}
                ]
            }

        def admin_user_global_sign_out(self, **kwargs):
            assert kwargs == {"UserPoolId": "pool", "Username": "login-name"}
            self.called = True

    cognito = Cognito()
    CognitoSessionRevoker(cognito, "pool").revoke("user")
    assert cognito.called


def test_missing_identity_is_terminal_and_never_signs_out_another_user():
    class Cognito:
        def list_users(self, **_):
            return {
                "Users": [
                    {"Username": "another", "Attributes": [{"Name": "sub", "Value": "another"}]}
                ]
            }

    with pytest.raises(IdentityAccountMissing):
        CognitoSessionRevoker(Cognito(), "pool").revoke("user")


@pytest.mark.parametrize(
    "changes",
    [
        {"user_client_ids": ["same"], "member_check_client_ids": ["same"]},
        {
            "database_url": "postgresql+psycopg://same:pass@localhost/db",
            "platform_database_url": "postgresql+psycopg://same:other@localhost/db",
        },
        {"platform_database_url": "sqlite:///:memory:"},
    ],
)
def test_access_modes_cannot_share_credentials_or_clients(changes):
    with pytest.raises(ValueError):
        VenueSettings(_env_file=None, **changes)


def test_probes_and_no_auth_bypass_when_unconfigured():
    with TestClient(create_app(VenueSettings(_env_file=None))) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/ready").status_code == 503
        assert client.get("/v1/venues/current").status_code == 401
        assert (
            client.get("/v1/venues/current", headers={"Authorization": "Bearer forged"}).status_code
            == 503
        )
        assert "tenant_id" not in client.get("/metrics").text


def test_wrong_clients_are_rejected_even_with_correct_roles_and_scopes():
    class Verifier:
        def verify(self, _):
            return Principal(
                subject="user",
                client_id="public-app",
                roles={"platform_admin"},
                scopes={"lostlink/members.check", "lostlink/identity.resolve"},
            )

    app = create_app(
        VenueSettings(
            _env_file=None, platform_client_ids=["ops"], member_check_client_ids=["item"]
        ),
        token_verifier=Verifier(),
    )
    with TestClient(app) as client:
        headers = {"Authorization": "Bearer token"}
        assert (
            client.post(
                "/v1/admin/venues",
                headers=headers,
                json={"name": "Venue", "slug": "venue", "initial_admin_sub": "admin"},
            ).status_code
            == 403
        )
        assert (
            client.post(
                "/internal/v1/members/check",
                headers=headers,
                json={
                    "subject": "admin",
                    "tenant_id": str(uuid4()),
                    "allowed_roles": ["venue_admin"],
                },
            ).status_code
            == 403
        )


def test_membership_event_is_strict_and_preserves_legacy_registry():
    event = DomainEvent(
        event_type="venue.membership_changed",
        source="venue-service",
        tenant_id=uuid4(),
        data={"membership_id": str(uuid4()), "user_sub": "user", "version": 1, "status": "active"},
    )
    assert DomainEvent.model_validate_json(event.to_message()) == event
    for changes in [
        {"tenant_id": None},
        {"source": "item-service"},
        {"data": {**event.data, "status": "admin"}},
        {"data": {**event.data, "email": "private@example.invalid"}},
    ]:
        with pytest.raises(ValidationError):
            DomainEvent.model_validate({**event.model_dump(), **changes})


@pytest.mark.parametrize(
    "mode,field", [("worker", "worker_database_url"), ("trigger", "identity_database_url")]
)
def test_deployed_workers_and_triggers_need_only_their_own_database_credential(mode, field):
    settings = VenueSettings(
        _env_file=None,
        environment="prod",
        process_mode=mode,
        **{field: "postgresql+psycopg://restricted:pass@localhost/db"},
    )
    assert settings.database_url is None and settings.platform_database_url is None
    with pytest.raises(ValueError):
        VenueSettings(_env_file=None, environment="prod", process_mode=mode)


def test_identity_lookup_pagination_is_bounded():
    class Cognito:
        def list_users(self, **_):
            return {"Users": [], "PaginationToken": "more"}

    with pytest.raises(TimeoutError):
        CognitoSessionRevoker(Cognito(), "pool").revoke("user")
