from app.main import create_app
from fastapi.testclient import TestClient
from lostlink_common.auth import IdentityUnavailable, InvalidToken, Principal
from lostlink_common.config import Settings


class StubVerifier:
    def verify(self, token):
        if token == "unavailable":
            raise IdentityUnavailable()
        if token != "valid":
            raise InvalidToken()
        return Principal(subject="owner-1", client_id="owner-client", roles={"owner"})


class ReadyDatabase:
    def check_ready(self, schema, revision):
        assert schema == "service_template"
        assert revision == "0001_template"


class UnavailableDatabase:
    def check_ready(self, schema, revision):
        raise RuntimeError("password=not-public")


def test_health():
    with TestClient(create_app(Settings(_env_file=None))) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_unconfigured_service_is_live_but_not_ready():
    with TestClient(create_app(Settings(_env_file=None))) as client:
        assert client.get("/health").status_code == 200
        response = client.get("/ready")
        assert response.status_code == 503
        assert response.json()["checks"]["database"] == "not_configured"
        assert client.get("/v1/me").status_code == 401
        assert client.get("/v1/me", headers={"Authorization": "Bearer token"}).status_code == 503


def test_ready_checks_database_and_identity_configuration():
    app = create_app(
        Settings(_env_file=None), token_verifier=StubVerifier(), database=ReadyDatabase()
    )
    with TestClient(app) as client:
        assert client.get("/ready").status_code == 200
        app.state.database = UnavailableDatabase()
        response = client.get("/ready")
        assert response.status_code == 503
        assert "not-public" not in response.text


def test_protected_identity_endpoint_and_uniform_errors():
    app = create_app(Settings(_env_file=None), token_verifier=StubVerifier())
    with TestClient(app) as client:
        response = client.get("/v1/me", headers={"Authorization": "Bearer valid"})
        assert response.status_code == 200
        assert response.json()["subject"] == "owner-1"
        for token, status in [("bad", 401), ("unavailable", 503)]:
            response = client.get("/v1/me", headers={"Authorization": f"Bearer {token}"})
            assert response.status_code == status
            assert response.json()["request_id"] == response.headers["X-Request-ID"]
        response = client.get("/v1/examples", headers={"Authorization": "Bearer valid"})
        assert response.status_code == 403


def test_docs_and_metrics_are_available():
    with TestClient(create_app(Settings(_env_file=None))) as client:
        assert client.get("/docs").status_code == 200
        paths = client.get("/openapi.json").json()["paths"]
        assert "/v1/examples" in paths
        response = client.get("/metrics")
        assert response.status_code == 200
        assert "lostlink_http_requests_total" in response.text
