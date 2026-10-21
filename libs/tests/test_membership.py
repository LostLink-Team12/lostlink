import json
from uuid import uuid4

import pytest
from fastapi import HTTPException
from lostlink_common.auth import Principal
from lostlink_common.membership import ClientCredentials, MembershipClient, MembershipUnavailable


def test_machine_tokens_are_cached_and_scope_is_explicit():
    calls = []

    def transport(url, headers, body):
        calls.append((url, headers, body))
        return {"access_token": "access", "expires_in": 3600, "token_type": "Bearer"}

    provider = ClientCredentials(
        "https://auth.example.invalid/oauth2/token",
        "item",
        "secret",
        ["lostlink/members.check"],
        transport=transport,
    )
    assert provider() == provider() == "access"
    assert len(calls) == 1
    assert b"lostlink%2Fmembers.check" in calls[0][2]
    assert calls[0][1]["Authorization"].startswith("Basic ")


@pytest.mark.parametrize(
    "response",
    [
        {},
        {"access_token": "", "expires_in": 10},
        {"access_token": "token", "expires_in": -1, "token_type": "Bearer"},
    ],
)
def test_invalid_machine_credentials_fail_closed(response):
    provider = ClientCredentials(
        "https://auth.invalid/token", "item", "secret", [], transport=lambda *_: response
    )
    with pytest.raises(MembershipUnavailable):
        provider()


def test_membership_writes_are_not_cached_and_old_tokens_cannot_keep_access():
    principal = Principal(
        subject="staff", client_id="web", tenant_id=uuid4(), roles={"venue_admin"}
    )
    calls = []

    def transport(url, headers, body):
        calls.append(json.loads(body))
        assert headers["Authorization"] == "Bearer machine-token"
        return {
            "active": len(calls) == 1,
            "effective_role": "venue_admin",
            "membership_version": len(calls),
            "venue_status": "active",
        }

    client = MembershipClient("https://venue.invalid", lambda: "machine-token", transport=transport)
    assert client.require_current(principal, "venue_admin").active
    with pytest.raises(HTTPException) as exc:
        client.require_current(principal, "venue_admin")
    assert exc.value.status_code == 403
    assert len(calls) == 2 and calls[0]["tenant_id"] == str(principal.tenant_id)


def test_membership_dependency_failure_is_not_an_authorization_success():
    principal = Principal(
        subject="staff", client_id="web", tenant_id=uuid4(), roles={"venue_staff"}
    )

    def unavailable(*_):
        raise MembershipUnavailable()

    for transport in [unavailable, lambda *_: {"active": "true"}]:
        client = MembershipClient("https://venue.invalid", lambda: "machine", transport=transport)
        with pytest.raises(HTTPException) as exc:
            client.require_current(principal, "venue_staff")
        assert exc.value.status_code == 503
