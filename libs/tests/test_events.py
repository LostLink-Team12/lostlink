import json
from datetime import UTC, datetime
from uuid import uuid4

import boto3
import pytest
from botocore.exceptions import ClientError
from botocore.stub import Stubber
from lostlink_common.events import DomainEvent, SnsPublisher, SqsPublisher, decode_message
from pydantic import ValidationError


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
