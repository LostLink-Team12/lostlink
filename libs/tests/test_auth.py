import time
from types import SimpleNamespace
from typing import Annotated
from uuid import UUID, uuid4

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from jwt.exceptions import PyJWKClientConnectionError
from lostlink_common.auth import (
    CognitoVerifier,
    IdentityUnavailable,
    InvalidToken,
    get_principal,
    require_roles,
    require_scopes,
    require_tenant,
)
from lostlink_common.config import Settings

ISSUER = "https://cognito-idp.ap-southeast-1.amazonaws.com/test-pool"


@pytest.fixture(scope="module")
def private_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def token_factory(private_key):
    def make(**overrides):
        now = int(time.time())
        claims = {
            "sub": "user-123",
            "iss": ISSUER,
            "iat": now,
            "exp": now + 300,
            "token_use": "access",
            "client_id": "venue-client",
            "cognito:groups": ["venue_staff"],
            "custom:tenant_id": str(uuid4()),
            "scope": "lostlink/items.read lostlink/claims.read",
        }
        claims.update(overrides)
        claims = {key: value for key, value in claims.items() if value is not None}
        return jwt.encode(claims, private_key, algorithm="RS256", headers={"kid": "key-1"})

    return make


@pytest.fixture
def verifier(private_key, monkeypatch):
    verifier = CognitoVerifier(
        Settings(_env_file=None, cognito_issuer=ISSUER, cognito_client_ids=["venue-client"])
    )
    key = jwt.algorithms.RSAAlgorithm.to_jwk(private_key.public_key(), as_dict=True)
    key.update(kid="key-1", use="sig", alg="RS256")
    monkeypatch.setattr(verifier.jwks_client, "fetch_data", lambda: {"keys": [key]})
    return verifier


def test_verifies_signature_and_extracts_identity(verifier, token_factory):
    principal = verifier.verify(token_factory())
    assert principal.subject == "user-123"
    assert principal.tenant_id is not None
    assert principal.roles == {"venue_staff"}
    assert principal.scopes == {"lostlink/items.read", "lostlink/claims.read"}


@pytest.mark.parametrize(
    "overrides",
    [
        {"exp": int(time.time()) - 120},
        {"iat": int(time.time()) + 3600},
        {"iss": "https://wrong.example.com"},
        {"client_id": "another-client"},
        {"token_use": "id"},
        {"exp": None},
        {"iat": None},
        {"sub": ""},
        {"sub": None},
        {"cognito:groups": "venue_admin"},
        {"cognito:groups": [1]},
        {"scope": ["admin"]},
        {"custom:tenant_id": "not-a-uuid"},
        {"custom:tenant_id": 123},
    ],
)
def test_invalid_claims_are_rejected(verifier, token_factory, overrides):
    with pytest.raises(InvalidToken):
        verifier.verify(token_factory(**overrides))


def test_wrong_signature_is_rejected(verifier):
    wrong_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    token = jwt.encode({"sub": "user"}, wrong_key, algorithm="RS256", headers={"kid": "key-1"})
    with pytest.raises(InvalidToken):
        verifier.verify(token)


@pytest.mark.parametrize(
    "token", ["not-a-jwt", "x" * 16385, jwt.encode({}, "x" * 32, algorithm="HS256")]
)
def test_invalid_headers_are_rejected(verifier, token):
    with pytest.raises(InvalidToken):
        verifier.verify(token)


def test_unknown_key_is_rejected(verifier, private_key):
    token = jwt.encode({}, private_key, algorithm="RS256", headers={"kid": "unknown"})
    with pytest.raises(InvalidToken):
        verifier.verify(token)


def test_new_signing_key_is_resolved(verifier, token_factory, private_key, monkeypatch):
    replacement = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = jwt.algorithms.RSAAlgorithm.to_jwk(replacement.public_key(), as_dict=True)
    public.update(kid="key-2", use="sig", alg="RS256")
    monkeypatch.setattr(verifier.jwks_client, "fetch_data", lambda: {"keys": [public]})
    claims = jwt.decode(token_factory(), private_key.public_key(), algorithms=["RS256"])
    token = jwt.encode(claims, replacement, algorithm="RS256", headers={"kid": "key-2"})
    assert verifier.verify(token).subject == "user-123"


def test_jwks_outage_is_not_reported_as_bad_credentials(verifier, token_factory, monkeypatch):
    def unavailable(_token):
        raise PyJWKClientConnectionError("Connection failed")

    monkeypatch.setattr(verifier.jwks_client, "get_signing_key_from_jwt", unavailable)
    with pytest.raises(IdentityUnavailable):
        verifier.verify(token_factory())


def test_optional_resource_audience_is_checked(private_key, token_factory):
    settings = Settings(
        _env_file=None,
        cognito_issuer=ISSUER,
        cognito_client_ids=["venue-client"],
        cognito_audience="lostlink-api",
    )
    jwks = SimpleNamespace(
        get_signing_key_from_jwt=lambda _token: SimpleNamespace(key=private_key.public_key())
    )
    verifier = CognitoVerifier(settings, jwks)
    with pytest.raises(InvalidToken):
        verifier.verify(token_factory(aud="another-api"))
    assert verifier.verify(token_factory(aud="lostlink-api")).subject == "user-123"


def test_machine_token_without_venue_membership(verifier, token_factory):
    token = token_factory(sub="venue-client", **{"custom:tenant_id": None, "cognito:groups": None})
    principal = verifier.verify(token)
    assert principal.tenant_id is None
    assert principal.roles == frozenset()


def test_authorization_dependencies(verifier, token_factory):
    app = FastAPI()
    app.state.token_verifier = verifier

    @app.get("/identity", dependencies=[Depends(get_principal)])
    def identity():
        return {"ok": True}

    @app.get("/staff", dependencies=[Depends(require_roles("venue_admin", "venue_staff"))])
    def staff():
        return {"ok": True}

    @app.get("/tenant")
    def tenant(tenant_id: Annotated[UUID, Depends(require_tenant)]):
        return {"tenant_id": str(tenant_id)}

    @app.get("/partner", dependencies=[Depends(require_scopes("lostlink/items.read", "missing"))])
    def partner():
        return {"ok": True}

    with TestClient(app) as client:
        assert client.get("/identity").status_code == 401
        headers = {"Authorization": f"Bearer {token_factory()}", "X-Tenant-ID": str(uuid4())}
        assert client.get("/staff", headers=headers).status_code == 200
        tenant = client.get("/tenant", headers=headers)
        assert tenant.status_code == 200
        assert tenant.json()["tenant_id"] != headers["X-Tenant-ID"]
        assert client.get("/partner", headers=headers).status_code == 403
        headers["Authorization"] = f"Bearer {token_factory(**{'cognito:groups': ['owner']})}"
        assert client.get("/staff", headers=headers).status_code == 403
        headers["Authorization"] = f"Bearer {token_factory(**{'custom:tenant_id': None})}"
        assert client.get("/tenant", headers=headers).status_code == 403
