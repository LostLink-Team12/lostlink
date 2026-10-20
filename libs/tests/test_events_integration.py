import json
import os
from uuid import uuid4

import pytest
from lostlink_common.config import Settings
from lostlink_common.events import (
    DomainEvent,
    SnsPublisher,
    SqsPublisher,
    aws_client,
    decode_message,
)

pytestmark = pytest.mark.integration


def test_localstack_sns_and_sqs_roundtrip():
    endpoint = os.getenv("LOSTLINK_TEST_AWS_ENDPOINT")
    if not endpoint:
        pytest.skip("Set LOSTLINK_TEST_AWS_ENDPOINT to a LocalStack URL")
    settings = Settings(_env_file=None, environment="test", aws_endpoint_url=endpoint)
    sns, sqs = aws_client("sns", settings), aws_client("sqs", settings)
    name = f"lostlink-test-{uuid4().hex}"
    topic = None
    queue = None
    try:
        topic = sns.create_topic(Name=name)["TopicArn"]
        queue = sqs.create_queue(QueueName=name)["QueueUrl"]
        arn = sqs.get_queue_attributes(QueueUrl=queue, AttributeNames=["QueueArn"])["Attributes"][
            "QueueArn"
        ]
        sqs.set_queue_attributes(
            QueueUrl=queue,
            Attributes={
                "Policy": json.dumps(
                    {
                        "Version": "2012-10-17",
                        "Statement": [
                            {
                                "Effect": "Allow",
                                "Principal": {"Service": "sns.amazonaws.com"},
                                "Action": "sqs:SendMessage",
                                "Resource": arn,
                                "Condition": {"ArnEquals": {"aws:SourceArn": topic}},
                            }
                        ],
                    }
                )
            },
        )
        sns.subscribe(TopicArn=topic, Protocol="sqs", Endpoint=arn)
        event = DomainEvent(
            event_type="claim.approved",
            source="claim-service",
            tenant_id=uuid4(),
            data={"item_id": str(uuid4()), "claim_id": str(uuid4()), "owner_id": "owner-1"},
        )
        SnsPublisher(sns, topic).publish(event)
        messages = sqs.receive_message(QueueUrl=queue, WaitTimeSeconds=5).get("Messages", [])
        assert len(messages) == 1
        assert decode_message(messages[0]["Body"]) == event
        sqs.delete_message(QueueUrl=queue, ReceiptHandle=messages[0]["ReceiptHandle"])
        SqsPublisher(sqs, queue).publish(event)
        messages = sqs.receive_message(QueueUrl=queue, WaitTimeSeconds=5).get("Messages", [])
        assert len(messages) == 1
        assert decode_message(messages[0]["Body"]) == event
    finally:
        if topic:
            sns.delete_topic(TopicArn=topic)
        if queue:
            sqs.delete_queue(QueueUrl=queue)
        sns.close()
        sqs.close()
