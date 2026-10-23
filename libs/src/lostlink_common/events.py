"""Versioned event envelopes and bounded SNS/SQS transports.

Publishing is at-least-once. Reuse the same event_id when retrying; consumers must
deduplicate it. These transports do not make a database write and publish atomic.
"""

import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID, uuid4

import boto3
from botocore.config import Config
from botocore.exceptions import (
    ClientError,
    ConnectionClosedError,
    ConnectTimeoutError,
    EndpointConnectionError,
    ReadTimeoutError,
)
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, JsonValue, model_validator

from lostlink_common.config import Settings
from lostlink_common.event_schemas import EVENT_SCHEMAS

MAX_EVENT_BYTES = 240 * 1024
SQS_READ_TIMEOUT_SECONDS = 25  # Longer than SQS's maximum 20-second long poll.
logger = logging.getLogger("lostlink.events")


class DomainEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: UUID = Field(default_factory=uuid4)
    event_type: str = Field(pattern=r"^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$", max_length=100)
    schema_version: Literal[1] = 1
    source: str = Field(pattern=r"^[a-z][a-z0-9-]*$", max_length=63)
    occurred_at: AwareDatetime = Field(default_factory=lambda: datetime.now(UTC))
    tenant_id: UUID | None = None
    correlation_id: UUID = Field(default_factory=uuid4)
    data: dict[str, JsonValue]

    @model_validator(mode="after")
    def validate_payload(self) -> "DomainEvent":
        if self.event_type not in EVENT_SCHEMAS:
            raise ValueError("Unknown event type; register its schema before publishing")
        payload_type, source, tenant_required = EVENT_SCHEMAS[self.event_type]
        if self.source != source:
            raise ValueError("Event source does not match the owning service")
        if tenant_required and self.tenant_id is None:
            raise ValueError("This event requires a tenant_id")
        payload_type.model_validate(self.data)
        return self

    def to_message(self) -> str:
        message = self.model_dump_json()
        if len(message.encode("utf-8")) > MAX_EVENT_BYTES:
            raise ValueError("Event exceeds the 240 KiB transport limit")
        # Revalidate nested mutable payloads and model_copy updates before transport.
        type(self).model_validate_json(message)
        return message


def decode_message(body: str) -> DomainEvent:
    """Accept a raw SQS message or an SNS-to-SQS notification wrapper."""
    if len(body.encode("utf-8")) > 256 * 1024:
        raise ValueError("Message exceeds the transport limit")
    payload = json.loads(body)
    if isinstance(payload, dict) and payload.get("Type") == "Notification":
        payload = json.loads(payload["Message"])
    return DomainEvent.model_validate(payload)


def aws_client(service: Literal["sns", "sqs", "cognito-idp"], settings: Settings):
    options: dict[str, Any] = {}
    if settings.aws_endpoint_url:
        options.update(
            endpoint_url=settings.aws_endpoint_url,
            aws_access_key_id="test",
            aws_secret_access_key="test",
        )
    return boto3.client(
        service,
        region_name=settings.aws_region,
        config=Config(
            connect_timeout=2,
            read_timeout=SQS_READ_TIMEOUT_SECONDS if service == "sqs" else 3,
            retries={"mode": "standard", "total_max_attempts": 3},
        ),
        **options,
    )


def _sqs_request(operation: Callable, **kwargs) -> dict | None:
    """SDK retries are bounded; the worker loop backs off before the next poll."""
    try:
        return operation(**kwargs)
    except (ConnectionClosedError, ConnectTimeoutError, EndpointConnectionError, ReadTimeoutError):
        logger.warning("sqs_transport_unavailable")
    except ClientError as exc:
        status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0)
        code = exc.response.get("Error", {}).get("Code", "")
        if (
            status not in {408, 429}
            and status < 500
            and code
            not in {
                "RequestTimeout",
                "RequestTimeoutException",
                "RequestThrottled",
                "Throttling",
                "ThrottlingException",
                "ServiceUnavailable",
                "InternalError",
                "InternalFailure",
            }
        ):
            raise  # AccessDenied, missing queues and invalid configuration need operator action.
        logger.warning("sqs_service_unavailable")
    return None


def receive_messages(client, queue_url: str, *, wait_seconds: int = 10) -> list[dict]:
    """An exhausted transient receive behaves like an empty poll, without logging payloads."""
    if not 0 <= wait_seconds <= 20:
        raise ValueError("SQS wait_seconds must be between 0 and 20")
    result = _sqs_request(
        client.receive_message,
        QueueUrl=queue_url,
        WaitTimeSeconds=wait_seconds,
        VisibilityTimeout=60,
        MaxNumberOfMessages=1,
    )
    return result.get("Messages", []) if result is not None else []


def acknowledge_message(client, queue_url: str, receipt_handle: str) -> bool:
    """A failed acknowledgement leaves a committed event available for inbox-safe replay."""
    return (
        _sqs_request(
            client.delete_message,
            QueueUrl=queue_url,
            ReceiptHandle=receipt_handle,
        )
        is not None
    )


def message_attributes(event: DomainEvent) -> dict:
    return {
        "event_type": {"DataType": "String", "StringValue": event.event_type},
        "schema_version": {"DataType": "Number", "StringValue": str(event.schema_version)},
    }


class SnsPublisher:
    def __init__(self, client, topic_arn: str):
        if not topic_arn or topic_arn.endswith(".fifo"):
            raise ValueError("A standard SNS topic ARN is required")
        self.client = client
        self.topic_arn = topic_arn

    def publish(self, event: DomainEvent) -> str:
        response = self.client.publish(
            TopicArn=self.topic_arn,
            Message=event.to_message(),
            MessageAttributes=message_attributes(event),
        )
        return response["MessageId"]


class SqsPublisher:
    def __init__(self, client, queue_url: str):
        if not queue_url or queue_url.endswith(".fifo"):
            raise ValueError("A standard SQS queue URL is required")
        self.client = client
        self.queue_url = queue_url

    def publish(self, event: DomainEvent) -> str:
        response = self.client.send_message(
            QueueUrl=self.queue_url,
            MessageBody=event.to_message(),
            MessageAttributes=message_attributes(event),
        )
        return response["MessageId"]
