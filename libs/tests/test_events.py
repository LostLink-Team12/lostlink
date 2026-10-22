import json
from datetime import UTC, datetime
from uuid import uuid4

import boto3
import pytest
from botocore.exceptions import ClientError
from botocore.stub import Stubber
from lostlink_common.events import DomainEvent, SnsPublisher, SqsPublisher, decode_message
from pydantic import ValidationError


def test_sqs_socket_timeout_allows_a_full_long_poll():
    from lostlink_common.config import Settings
    from lostlink_common.events import aws_client

    settings = Settings(_env_file=None, aws_endpoint_url="http://localhost:4566")
    sqs, sns = aws_client("sqs", settings), aws_client("sns", settings)
    try:
        assert sqs.meta.config.read_timeout > 20
        assert sns.meta.config.read_timeout == 3
    finally:
        sqs.close()
        sns.close()


@pytest.fixture
def event():
    return DomainEvent(
        event_type="item.registered",
        source="item-service",
        tenant_id=uuid4(),
        data={
            "item_id": str(uuid4()),
            "item_version": 1,
            "photo_bucket": "lostlink-photos",
            "photo_key": "items/photo.jpg",
            "description": "A blue umbrella",
            "category": "umbrella",
            "found_at": datetime.now(UTC).isoformat(),
        },
    )


def client(service):
    return boto3.client(
        service,
        region_name="ap-southeast-1",
        aws_access_key_id="test",
        aws_secret_access_key="test",
    )


def test_raw_and_wrapped_event_roundtrip(event):
    raw = event.to_message()
    assert decode_message(raw) == event
    assert decode_message(json.dumps({"Type": "Notification", "Message": raw})) == event


@pytest.mark.parametrize(
    "changes",
    [
        {"schema_version": 2},
        {"event_type": "invalid name"},
        {"source": "../../service"},
        {"occurred_at": datetime(2026, 1, 1)},
        {"tenant_id": "bad"},
        {"unexpected": True},
        {"source": "claim-service"},
        {"event_type": "item.unknown"},
        {"tenant_id": None},
        {"data": {"item_id": str(uuid4())}},
    ],
)
def test_invalid_envelopes_fail_before_publishing(event, changes):
    with pytest.raises(ValidationError):
        DomainEvent.model_validate({**event.model_dump(), **changes})


def test_large_payload_is_rejected(event):
    oversized = event.model_copy(update={"data": {"text": "x" * (240 * 1024)}})
    with pytest.raises(ValueError, match="limit"):
        oversized.to_message()


def claim_event(event_type, **data):
    base = {"claim_id": str(uuid4()), "item_id": str(uuid4()), "owner_id": "owner-sub"}
    return {
        "event_type": event_type,
        "source": "claim-service",
        "tenant_id": str(uuid4()),
        "data": {**base, **data},
    }


@pytest.mark.parametrize(
    "envelope",
    [
        claim_event(
            "claim.handed_over", mode="counter", handed_over_at=datetime.now(UTC).isoformat()
        ),
        claim_event(
            "claim.handed_over", mode="courier", handed_over_at=datetime.now(UTC).isoformat()
        ),
        claim_event("claim.cancelled", reason_code="found_elsewhere"),
        claim_event("claim.cancelled", reason_code="other"),
    ],
)
def test_claim_lifecycle_events_roundtrip(envelope):
    event = DomainEvent.model_validate(envelope)
    assert decode_message(event.to_message()) == event


@pytest.mark.parametrize(
    "changes",
    [
        {"source": "item-service"},
        {"tenant_id": None},
        {"data_update": {"mode": "drone"}},
        {"data_update": {"handed_over_at": "2026-10-20T09:00:00"}},
        {"data_update": {"contact_email": "owner@example.com"}},
        {"data_update": {"owner_id": ""}},
    ],
)
def test_claim_handover_event_rejects_invalid_or_private_fields(changes):
    envelope = claim_event(
        "claim.handed_over", mode="counter", handed_over_at=datetime.now(UTC).isoformat()
    )
    envelope["data"].update(changes.pop("data_update", {}))
    with pytest.raises(ValidationError):
        DomainEvent.model_validate({**envelope, **changes})


@pytest.mark.parametrize(
    "data", [{"reason_code": "I changed my mind"}, {}, {"reason_code": "other", "note": "free"}]
)
def test_claim_cancellation_uses_controlled_reason_codes_only(data):
    envelope = claim_event("claim.cancelled")
    envelope["data"].update(data)
    with pytest.raises(ValidationError):
        DomainEvent.model_validate(envelope)


def test_sns_preserves_event_identity_and_adds_filter_attributes(event):
    sns = client("sns")
    topic = "arn:aws:sns:ap-southeast-1:123456789012:events"
    expected = {
        "TopicArn": topic,
        "Message": event.to_message(),
        "MessageAttributes": {
            "event_type": {"DataType": "String", "StringValue": "item.registered"},
            "schema_version": {"DataType": "Number", "StringValue": "1"},
        },
    }
    with Stubber(sns) as stub:
        stub.add_response("publish", {"MessageId": "message-1"}, expected)
        stub.add_response("publish", {"MessageId": "message-2"}, expected)
        publisher = SnsPublisher(sns, topic)
        assert publisher.publish(event) == "message-1"
        assert publisher.publish(event) == "message-2"
        stub.assert_no_pending_responses()


def test_sqs_transport_and_failures(event):
    sqs = client("sqs")
    queue = "https://sqs.ap-southeast-1.amazonaws.com/123456789012/jobs"
    with Stubber(sqs) as stub:
        stub.add_response("send_message", {"MessageId": "job-1"})
        stub.add_client_error("send_message", service_error_code="AccessDenied")
        publisher = SqsPublisher(sqs, queue)
        assert publisher.publish(event) == "job-1"
        with pytest.raises(ClientError):
            publisher.publish(event)
        stub.assert_no_pending_responses()
