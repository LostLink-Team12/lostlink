import json
import logging
from uuid import UUID, uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
from lostlink_common.errors import install_error_handlers
from lostlink_common.observability import HttpMetrics, JsonFormatter, ObservabilityMiddleware


def test_metrics_use_route_templates_and_request_ids():
    app = FastAPI()
    metrics = HttpMetrics("test-service")
    app.add_middleware(ObservabilityMiddleware, metrics=metrics)

    @app.get("/items/{item_id}")
    def item(item_id: str):
        return {"id": item_id}

    with TestClient(app) as client:
        request_id = str(uuid4())
        response = client.get(
            "/items/private-id?token=secret", headers={"X-Request-ID": request_id}
        )
        assert response.headers["X-Request-ID"] == request_id
        assert UUID(
            client.get("/items/second", headers={"X-Request-ID": "invalid"}).headers["X-Request-ID"]
        )
        client.get("/unknown/private-value")
    samples = [sample for metric in metrics.registry.collect() for sample in metric.samples]
    routes = {sample.labels["route"] for sample in samples}
    assert routes == {"/items/{item_id}", "__unmatched__"}
    assert "private-id" not in str(samples)
    assert "secret" not in str(samples)


def test_unhandled_errors_have_request_id_and_no_exception_details():
    app = FastAPI()
    app.add_middleware(ObservabilityMiddleware, metrics=HttpMetrics("test-service"))
    install_error_handlers(app)

    @app.get("/failure")
    def failure():
        raise RuntimeError("password=do-not-expose")

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/failure")
    assert response.status_code == 500
    assert response.json()["request_id"] == response.headers["X-Request-ID"]
    assert "do-not-expose" not in response.text


def test_json_formatter_does_not_dump_exception_values():
    record = logging.LogRecord(
        "lostlink.test",
        logging.ERROR,
        __file__,
        1,
        "operation_failed",
        (),
        (RuntimeError, RuntimeError("secret-value"), None),
    )
    output = JsonFormatter("test-service").format(record)
    assert json.loads(output)["error_type"] == "RuntimeError"
    assert "secret-value" not in output
