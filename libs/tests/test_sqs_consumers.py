"""SQS transport recovery must preserve acknowledgement ordering in every consumer."""

import logging
from uuid import uuid4

import pytest
from botocore.exceptions import (
    ClientError,
    ConnectionClosedError,
    ConnectTimeoutError,
    EndpointConnectionError,
    ReadTimeoutError,
)
from item_app.workers import ItemWorker
from lostlink_common.config import Settings
from lostlink_common.events import (
    DomainEvent,
    acknowledge_message,
    aws_client,
    receive_messages,
)
from matching_app.workers import MatchingWorker
from notification_app.workers import NotificationWorker

QUEUE = "https://sqs.invalid/private-queue"


class Queue:
    def __init__(self, messages=(), *, receive_error=None, delete_error=None):
        self.messages = list(messages)
        self.receive_error, self.delete_error = receive_error, delete_error
        self.deleted = []

    def receive_message(self, **kwargs):
        if self.receive_error:
            error, self.receive_error = self.receive_error, None
            raise error
        return {"Messages": self.messages}

    def delete_message(self, **kwargs):
        if self.delete_error:
            error, self.delete_error = self.delete_error, None
            raise error
        self.deleted.append(kwargs["ReceiptHandle"])
        return {}


@pytest.mark.parametrize("service,timeout", [("sqs", 25), ("sns", 3), ("cognito-idp", 3)])
def test_only_sqs_has_a_timeout_longer_than_the_maximum_long_poll(service, timeout):
    settings = Settings(_env_file=None, aws_endpoint_url="http://127.0.0.1:4566")
    client = aws_client(service, settings)
    try:
        assert client.meta.config.read_timeout == timeout
        assert client.meta.config.retries["total_max_attempts"] == 3
    finally:
        client.close()


@pytest.mark.parametrize(
    "error_class",
    [ReadTimeoutError, ConnectTimeoutError, EndpointConnectionError, ConnectionClosedError],
)
def test_transport_errors_are_recoverable_without_logging_queue_or_receipt(
    error_class, caplog, monkeypatch
):
    # App lifespans configure the parent lostlink logger with propagation disabled.
    logger = logging.getLogger("lostlink.events")
    monkeypatch.setattr(logger, "handlers", [caplog.handler])
    monkeypatch.setattr(logger, "propagate", False)
    queue = Queue(receive_error=error_class(endpoint_url=QUEUE))
    assert receive_messages(queue, QUEUE) == []
    queue.delete_error = error_class(endpoint_url=QUEUE)
    assert not acknowledge_message(queue, QUEUE, "private-receipt")
    assert "sqs_transport_unavailable" in caplog.text
    assert "private-queue" not in caplog.text and "private-receipt" not in caplog.text


@pytest.mark.parametrize(
    "status,code", [(429, "RequestThrottled"), (503, "ServiceUnavailable"), (400, "RequestTimeout")]
)
def test_transient_service_errors_return_to_the_worker_loop(status, code):
    error = ClientError(
        {"Error": {"Code": code}, "ResponseMetadata": {"HTTPStatusCode": status}}, "ReceiveMessage"
    )
    assert receive_messages(Queue(receive_error=error), QUEUE) == []


@pytest.mark.parametrize("code", ["AccessDenied", "AWS.SimpleQueueService.NonExistentQueue"])
def test_configuration_and_permission_errors_are_not_hidden(code):
    error = ClientError({"Error": {"Code": code}}, "ReceiveMessage")
    with pytest.raises(ClientError):
        receive_messages(Queue(receive_error=error), QUEUE)
    with pytest.raises(ClientError):
        acknowledge_message(Queue(delete_error=error), QUEUE, "receipt")


@pytest.mark.parametrize("wait_seconds", [-1, 21])
def test_invalid_long_poll_duration_is_rejected(wait_seconds):
    with pytest.raises(ValueError):
        receive_messages(Queue(), QUEUE, wait_seconds=wait_seconds)


@pytest.fixture(params=[ItemWorker, MatchingWorker, NotificationWorker])
def worker(request):
    return request.param(None)


@pytest.fixture
def message():
    event = DomainEvent(
        event_type="claim.approved",
        source="claim-service",
        tenant_id=uuid4(),
        data={"item_id": str(uuid4()), "claim_id": str(uuid4()), "owner_id": "owner"},
    )
    return {"Body": event.to_message(), "ReceiptHandle": "receipt"}


def test_every_consumer_continues_after_a_transient_receive_failure(worker, message):
    queue = Queue([message], receive_error=ReadTimeoutError(endpoint_url=QUEUE))
    committed = []

    def consume(event):
        assert not queue.deleted
        committed.append(event.event_id)

    worker.consume = consume
    assert not worker.poll(queue, QUEUE)
    assert not committed and not queue.deleted
    assert worker.poll(queue, QUEUE)
    assert len(committed) == 1 and queue.deleted == ["receipt"]


def test_every_consumer_leaves_failed_acknowledgements_available_for_replay(worker, message):
    queue = Queue([message], delete_error=ReadTimeoutError(endpoint_url=QUEUE))
    committed = []
    worker.consume = lambda event: committed.append(event.event_id)
    assert worker.poll(queue, QUEUE)
    assert len(committed) == 1 and not queue.deleted
    assert worker.poll(queue, QUEUE)
    assert committed[0] == committed[1] and queue.deleted == ["receipt"]


def test_every_consumer_leaves_rejected_messages_unacknowledged(worker, message):
    def reject(event):
        raise ValueError("conflicting event")

    worker.consume = reject
    queue = Queue([message])
    assert worker.poll(queue, QUEUE)
    assert not queue.deleted
