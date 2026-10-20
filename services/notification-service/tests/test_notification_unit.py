"""Template safety, SES outcome mapping, configuration and HTTP guards without a database."""

from uuid import uuid4

import boto3
import pytest
from botocore.exceptions import ConnectTimeoutError, EndpointConnectionError, ReadTimeoutError
from botocore.stub import Stubber
from fastapi.testclient import TestClient
from lostlink_common.auth import InvalidToken, Principal
from notification_app.clients import MatchingClient, MatchingUnavailable
from notification_app.config import NotificationSettings
from notification_app.mail import (
    SUBJECT,
    TEMPLATE_VERSION,
    SendNotAccepted,
    SendOutcomeUnknown,
    SesSender,
    render_match_found,
    ses_client,
)
from notification_app.main import create_app
from pydantic import ValidationError


def settings(**values):
    return NotificationSettings(_env_file=None, **values)


def test_template_escapes_content_and_uses_the_configured_origin_only():
    match_id = uuid4()
    message = render_match_found(
        to="owner@example.com",
        match_id=match_id,
        item_title='<img src=x onerror="alert(1)"> wallet',
        venue_name=None,
        base_url="https://app.lostlink.example/",
    )
    assert TEMPLATE_VERSION == "match-found-v1" and message.subject == SUBJECT
    link = f"https://app.lostlink.example/matches/{match_id}"
    assert link in message.text and f'href="{link}"' in message.html
    assert "<img" not in message.html and "&lt;img" in message.html
    assert "a LostLink venue" in message.text
    assert "not proof of ownership" in message.text
    with pytest.raises(ValueError):
        render_match_found(
            to="x@example.com",
            match_id="https://evil.example",
            item_title=None,
            venue_name=None,
            base_url="https://app.lostlink.example",
        )
    assert "owner@example.com" not in repr(message)


def ses():
    return boto3.client(
        "ses", region_name="ap-southeast-1", aws_access_key_id="t", aws_secret_access_key="t"
    )


def message():
    return render_match_found(
        to="owner@example.com",
        match_id=uuid4(),
        item_title="wallet",
        venue_name=None,
        base_url="https://app.lostlink.example",
    )


def test_ses_sender_maps_accepted_rejected_and_unknown_outcomes():
    client = ses()
    with Stubber(client) as stub:
        stub.add_response("send_email", {"MessageId": "ses-1"})
        stub.add_client_error("send_email", "MessageRejected", http_status_code=400)
        stub.add_client_error("send_email", "Throttling", http_status_code=400)
        stub.add_client_error("send_email", "InternalFailure", http_status_code=500)
        sender = SesSender(client, "noreply@lostlink.example", "lostlink")
        assert sender.send(message()) == "ses-1"
        with pytest.raises(SendNotAccepted) as rejected:
            sender.send(message())
        assert (rejected.value.code, rejected.value.retryable) == ("message_rejected", False)
        with pytest.raises(SendNotAccepted) as throttled:
            sender.send(message())
        assert throttled.value.retryable
        with pytest.raises(SendOutcomeUnknown):
            sender.send(message())

    class Failing:
        def __init__(self, exc):
            self.exc = exc

        def send_email(self, **_):
            raise self.exc

    with pytest.raises(SendNotAccepted):
        SesSender(Failing(EndpointConnectionError(endpoint_url="x")), "a@b.c").send(message())
    with pytest.raises(SendNotAccepted):
        SesSender(Failing(ConnectTimeoutError(endpoint_url="x")), "a@b.c").send(message())
    with pytest.raises(SendOutcomeUnknown):
        SesSender(Failing(ReadTimeoutError(endpoint_url="x")), "a@b.c").send(message())

    class NoId:
        def send_email(self, **_):
            return {}

    with pytest.raises(SendOutcomeUnknown):
        SesSender(NoId(), "a@b.c").send(message())


def test_ses_client_never_retries_inside_the_sdk():
    client = ses_client(settings())
    assert client.meta.config.retries["total_max_attempts"] == 1
    client.close()


def test_configuration_rejects_capture_in_deployments_and_untrusted_urls():
    with pytest.raises(ValidationError):
        settings(environment="dev", email_sender="capture")
    with pytest.raises(ValidationError):
        settings(owner_app_base_url="http://app.lostlink.example")
    with pytest.raises(ValidationError):
        settings(ses_from_address="a@b.c, evil@x.y")
    with pytest.raises(ValidationError):
        settings(user_client_ids=["web"], operator_client_ids=["web"])


def test_matching_client_outcomes():
    match_id = uuid4()
    value = {
        "match_id": str(match_id),
        "owner_id": "owner",
        "report_id": str(uuid4()),
        "item_id": str(uuid4()),
        "tenant_id": str(uuid4()),
        "active": True,
        "item_title": "wallet",
        "venue_name": None,
    }

    def client(status, body):
        return MatchingClient(
            "https://matching.internal", lambda: "token", transport=lambda *_: (status, body)
        )

    assert client(200, value).context(match_id).active
    assert client(404, None).context(match_id) is None
    for status, body in [
        (503, None),
        (403, None),
        (200, {"bad": 1}),
        (200, {**value, "match_id": str(uuid4())}),
    ]:
        with pytest.raises(MatchingUnavailable):
            client(status, body).context(match_id)


class Verifier:
    tokens = {
        "owner": Principal(subject="owner", client_id="web", roles={"owner"}),
        "machine": Principal(subject="m2m", client_id="notify", scopes={"lostlink/x"}),
        "operator": Principal(subject="op", client_id="ops", roles={"platform_admin"}),
        "fake-operator": Principal(subject="op", client_id="web", roles={"platform_admin"}),
    }

    def verify(self, token):
        if token not in self.tokens:
            raise InvalidToken()
        return self.tokens[token]


def test_probes_and_guards_fail_closed_without_dependencies():
    app = create_app(
        settings(user_client_ids=["web"], operator_client_ids=["ops"]), token_verifier=Verifier()
    )
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/ready").status_code == 503
        assert client.get("/v1/notifications").status_code == 401
        machine = {"Authorization": "Bearer machine"}
        assert client.get("/v1/notifications", headers=machine).status_code == 403
        owner = {"Authorization": "Bearer owner"}
        assert client.get("/v1/notifications", headers=owner).status_code == 503
        for token in ["owner", "fake-operator", "machine"]:
            response = client.get(
                "/v1/admin/notifications", headers={"Authorization": f"Bearer {token}"}
            )
            assert response.status_code == 403
        operator = {"Authorization": "Bearer operator"}
        assert client.get("/v1/admin/notifications", headers=operator).status_code == 503
