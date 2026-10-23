"""Keep generated error contracts aligned with the shared and service handlers."""

import importlib
from uuid import uuid4

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from lostlink_common.errors import ErrorResponse, error_response, install_error_handlers
from lostlink_common.observability import HttpMetrics, ObservabilityMiddleware
from pydantic import BaseModel

from scripts import export_contracts
from scripts.export_contracts import SERVICES, service_documents


@pytest.fixture(scope="module")
def documents():
    return service_documents()


@pytest.fixture(params=SERVICES)
def service(request):
    name = request.param
    main_module, config_module, settings_class = SERVICES[name]
    settings = getattr(importlib.import_module(config_module), settings_class)(_env_file=None)
    app = importlib.import_module(main_module).create_app(settings)
    namespace = main_module.removesuffix(".main")
    error_class = getattr(
        importlib.import_module(f"{namespace}.errors"),
        "".join(part.title() for part in name.removesuffix("-service").split("-")) + "Error",
    )
    return name, app, error_class


def operations(document):
    for path, entry in document["paths"].items():
        if path.startswith(("/v1/", "/internal/v1/")):
            for method, operation in entry.items():
                if method in {"get", "post", "patch", "put", "delete"}:
                    yield path, method, operation


def test_business_contracts_use_shared_errors_and_keep_preconditions_scoped(documents):
    for document in documents.values():
        assert "HTTPValidationError" not in document["components"]["schemas"]
        for path, method, operation in operations(document):
            responses = operation["responses"]
            assert {"401", "403", "422", "500", "503"} <= responses.keys(), path
            headers = {
                parameter["name"]
                for parameter in operation.get("parameters", [])
                if parameter["in"] == "header"
            }
            assert ("412" in responses) == ("If-Match" in headers), path
            assert ("428" in responses) == bool(headers & {"If-Match", "Idempotency-Key"}), path
            if method == "get":
                assert not {"412", "428"} & responses.keys(), path
            for status, response in responses.items():
                if int(status) < 400:
                    continue
                assert response["content"]["application/json"]["schema"] == {
                    "$ref": "#/components/schemas/ErrorResponse"
                }, (path, status)
                assert "X-Request-ID" in response["headers"]
            assert "WWW-Authenticate" in responses["401"]["headers"]


def test_lookup_conflict_size_and_async_contracts_remain_distinct(documents):
    claim = documents["claim-service.openapi.json"]["paths"]
    questions = claim["/v1/claim-questions"]["get"]["responses"]
    assert {"404", "409"} <= questions.keys()
    cancel = claim["/v1/claims/{claim_id}/cancel"]["post"]["responses"]
    assert cancel["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ClaimDetail"
    }
    assert cancel["202"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/AcceptedOperation"
    }
    for name, document in documents.items():
        for path, _, operation in operations(document):
            assert ("413" in operation["responses"]) == (name == "matching-service.openapi.json")
            if "{" in path:
                assert "404" in operation["responses"], path
        assert "401" not in document["paths"]["/health"]["get"]["responses"]
        assert "ErrorResponse" not in str(document["paths"]["/ready"])


def test_actual_unauthenticated_responses_match_the_contract(service):
    _, app, _ = service
    path, method = next(
        (path, method) for path, method, _ in operations(app.openapi()) if method == "get"
    )
    with TestClient(app) as client:
        response = client.request(method, path)
    assert response.status_code == 401
    body = ErrorResponse.model_validate(response.json())
    assert body.error.code == "unauthenticated"
    assert body.request_id == response.headers["X-Request-ID"]
    assert response.headers["WWW-Authenticate"] == "Bearer"
    assert "details" not in response.json()["error"]


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (404, "not_found"),
        (409, "operation_in_progress"),
        (412, "version_mismatch"),
        (428, "precondition_required"),
    ],
)
def test_actual_service_domain_errors_match_the_contract(service, status, code):
    _, app, error_class = service

    @app.get("/contract-error")
    def failure():
        raise error_class(status, code, "Safe error message")

    with TestClient(app) as client:
        response = client.get("/contract-error")
    assert response.status_code == status
    body = ErrorResponse.model_validate(response.json())
    assert body.error.code == code
    assert body.request_id == response.headers["X-Request-ID"]
    assert "details" not in response.json()["error"]
    if status == 409:
        assert response.headers["Retry-After"] == "1"


def test_request_validation_uses_sanitized_details_instead_of_fastapi_default():
    class Payload(BaseModel):
        quantities: list[int]

    app = FastAPI()
    app.add_middleware(ObservabilityMiddleware, metrics=HttpMetrics("test-errors"))
    install_error_handlers(app)

    @app.post("/validation")
    def validate(body: Payload):
        return body

    request_id = str(uuid4())
    with TestClient(app) as client:
        response = client.post(
            "/validation",
            json={"quantities": ["private-input"]},
            headers={"X-Request-ID": request_id},
        )
    assert response.status_code == 422
    body = ErrorResponse.model_validate(response.json())
    assert body.request_id == response.headers["X-Request-ID"] == request_id
    assert body.error.code == "validation_error"
    assert body.error.details[0].location == ["body", "quantities", 0]
    assert body.error.details[0].type == "int_parsing"
    assert "private-input" not in response.text
    assert "detail" not in response.json()


def test_shared_error_contract_allows_missing_observability_context():
    response = error_response(Request({"type": "http"}), 503, "unavailable", "Not configured")
    assert ErrorResponse.model_validate_json(response.body).request_id is None


def test_contract_check_detects_stale_files_without_rewriting(monkeypatch, tmp_path, documents):
    monkeypatch.setattr(export_contracts, "OUTPUT", tmp_path)
    monkeypatch.setattr("sys.argv", ["export_contracts.py", "--check"])
    assert export_contracts.main() == 1
    assert not list(tmp_path.iterdir())
    expected = {**documents, "events.schema.json": export_contracts.event_document()}
    for name, content in expected.items():
        (tmp_path / name).write_text(export_contracts.render(content), encoding="utf-8")
    assert export_contracts.main() == 0
    stale = tmp_path / "matching-service.openapi.json"
    stale.write_text("{}\n", encoding="utf-8")
    assert export_contracts.main() == 1
    assert stale.read_text() == "{}\n"
