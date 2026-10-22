"""HTTP guards, configuration and address sealing without a database."""

from uuid import uuid4

import pytest
from delivery_app.config import PARTNER_SCOPE, DeliverySettings
from delivery_app.main import create_app
from delivery_app.service import open_address, seal_address
from fastapi.testclient import TestClient
from lostlink_common.auth import InvalidToken, Principal
from lostlink_common.crypto import DecryptionFailed, FieldCipher
from pydantic import ValidationError

KEY = FieldCipher.generate_key()
CIPHER = FieldCipher.from_secret(f'{{"k1": "{KEY}"}}', "k1")
ADDRESS = {
    "recipient_name": "Alex Owner",
    "address_line": "1 Example Road #01-01",
    "postal_code": "123456",
    "phone": "+65 8000 0000",
}


class Verifier:
    tokens = {
        "owner": Principal(subject="owner-1", client_id="owner-web", roles={"owner"}),
        "staff-on-owner-app": Principal(subject="staff", client_id="owner-web", roles=set()),
        "partner": Principal(subject="courier-a", client_id="courier-a", scopes={PARTNER_SCOPE}),
        "partner-no-scope": Principal(subject="courier-a", client_id="courier-a"),
        "owner-as-partner": Principal(
            subject="owner-1", client_id="owner-web", scopes={PARTNER_SCOPE}
        ),
    }

    def verify(self, token):
        if token not in self.tokens:
            raise InvalidToken()
        return self.tokens[token]


def settings(**values):
    return DeliverySettings(_env_file=None, **values)


def client(cipher=CIPHER):
    app = create_app(
        settings(user_client_ids=["owner-web"], partner_client_ids=["courier-a"]),
        token_verifier=Verifier(),
        cipher=cipher,
    )
    return TestClient(app)


def bearer(token):
    return {"Authorization": f"Bearer {token}", "Idempotency-Key": "delivery-test-key"}


def test_health_and_readiness_fail_closed_without_database():
    with client() as http:
        assert http.get("/health").json()["status"] == "ok"
        ready = http.get("/ready")
        assert ready.status_code == 503
        assert ready.json()["checks"]["database"] is False
        assert http.get("/metrics").status_code == 200


def test_lifecycle_is_published():
    with client() as http:
        body = http.get("/v1/shipment-lifecycle").json()
    assert {"from": "awaiting_pickup", "to": "picked_up", "actors": ["system"]} in body[
        "transitions"
    ]
    assert body["terminal"] == ["cancelled", "delivered", "returned_to_venue"]


def test_owner_and_partner_routes_require_the_right_client():
    authorization = {
        "claim_id": str(uuid4()),
        "partner_id": "courier-a",
        "delivery_address": ADDRESS,
    }
    with client() as http:
        assert http.get("/v1/shipments").status_code == 401
        assert http.get("/v1/shipments", headers=bearer("bad")).status_code == 401
        assert http.get("/v1/shipments", headers=bearer("staff-on-owner-app")).status_code == 403
        assert http.get("/v1/shipments", headers=bearer("partner")).status_code == 403
        for token in ["owner", "partner-no-scope", "owner-as-partner"]:
            response = http.get("/v1/partner/shipments", headers=bearer(token))
            assert response.status_code == 403, token
        # Valid callers reach the data layer, which is not configured here.
        unavailable = http.get("/v1/shipments", headers=bearer("owner"))
        assert unavailable.status_code == 503
        assert unavailable.json()["error"]["code"] == "unavailable"
        assert http.get("/v1/partner/shipments", headers=bearer("partner")).status_code == 503
        created = http.post(
            "/v1/delivery-authorizations", headers=bearer("owner"), json=authorization
        )
        assert created.status_code == 503
        assert "Example Road" not in created.text


@pytest.mark.parametrize(
    "change",
    [
        {"partner_id": ""},
        {"delivery_address": {**ADDRESS, "postal_code": "12345"}},
        {"delivery_address": {**ADDRESS, "phone": "call me"}},
        {"owner_id": "someone-else"},
        {"tenant_id": str(uuid4())},
    ],
)
def test_authorization_body_is_validated(change):
    body = {"claim_id": str(uuid4()), "partner_id": "courier-a", "delivery_address": ADDRESS}
    with client() as http:
        response = http.post(
            "/v1/delivery-authorizations", headers=bearer("owner"), json={**body, **change}
        )
    assert response.status_code == 422


def test_partner_cannot_report_pickup_or_unknown_states():
    with client() as http:
        for state in ["picked_up", "awaiting_pickup", "cancelled", "lost"]:
            response = http.post(
                f"/v1/partner/shipments/{uuid4()}/status",
                headers={**bearer("partner"), "If-Match": '"2"'},
                json={"state": state},
            )
            assert response.status_code == 422, state


def test_address_is_bound_to_its_shipment():
    shipment_id = uuid4()
    sealed = seal_address(CIPHER, shipment_id, ADDRESS)
    assert b"Example Road" not in sealed.ciphertext

    class Row:
        id = shipment_id
        address_key_id = sealed.key_id
        address_ciphertext = sealed.ciphertext

    assert open_address(CIPHER, Row) == ADDRESS
    Row.id = uuid4()
    with pytest.raises(DecryptionFailed):
        open_address(CIPHER, Row)


def test_deployed_configuration_is_validated():
    with pytest.raises(ValidationError):
        settings(environment="dev")
    with pytest.raises(ValidationError):
        settings(user_client_ids=["same"], partner_client_ids=["same"])
    with pytest.raises(ValidationError):
        settings(field_encryption_keys=f'{{"k1": "{KEY}"}}')
    with pytest.raises(ValidationError):
        settings(
            database_url="postgresql+psycopg://same:x@db/lostlink",
            worker_database_url="postgresql+psycopg://same:y@db/lostlink",
        )
    with pytest.raises(ValidationError):
        settings(environment="dev", process_mode="worker")
    worker = settings(
        environment="dev",
        process_mode="worker",
        worker_mode="consume",
        worker_database_url="postgresql+psycopg://lostlink_delivery_worker:x@db/lostlink",
    )
    assert worker.worker_mode == "consume"
