from delivery_app.main import create_app
from fastapi.testclient import TestClient

client = TestClient(create_app())


def test_health_and_ready():
    assert client.get("/health").json()["status"] == "ok"
    assert client.get("/ready").status_code == 200


def test_lifecycle_is_published():
    body = client.get("/v1/shipment-lifecycle").json()
    assert {"from": "awaiting_pickup", "to": "picked_up", "actors": ["system"]} in body[
        "transitions"
    ]
    assert body["terminal"] == ["cancelled", "delivered", "returned_to_venue"]
