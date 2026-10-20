"""Real Matching match.found events, PostgreSQL RLS and explicit email outcomes."""

import os
from uuid import UUID, uuid4

import pytest
from alembic import command
from lostlink_common.config import Settings
from lostlink_common.events import DomainEvent, SnsPublisher, aws_client
from notification_app.models import Notification, NotificationAttempt
from notification_app.workers import ProtocolError
from notification_support import admin_sql, drain, headers, matched
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

pytestmark = pytest.mark.integration


def notification(env, match_id):
    with env.db["notification_worker"].system_session() as session:
        return session.scalar(select(Notification).where(Notification.match_id == match_id))


def attempts(env, notification_id):
    with env.db["notification_worker"].system_session() as session:
        rows = session.scalars(
            select(NotificationAttempt)
            .where(NotificationAttempt.notification_id == notification_id)
            .order_by(NotificationAttempt.attempt_number)
        )
        return [(row.result, row.error_code) for row in rows]


def forged(event, **data):
    return DomainEvent(
        event_type="match.found",
        source="matching-service",
        tenant_id=event.tenant_id,
        data={**event.data, **data},
    )


def test_real_match_sends_one_escaped_email_after_revalidation(notification_env):
    env = notification_env
    title = 'red <script>alert("x")</script> backpack'
    _, _, item_id, event = matched(env, title=title)
    assert env.worker.consume(event) == "created"
    assert env.worker.consume(event) == "duplicate"
    # A different event ID for the same match is a business duplicate.
    assert env.worker.consume(forged(event)) == "duplicate_match"
    with pytest.raises(ProtocolError):
        env.worker.consume(event.model_copy(update={"data": {**event.data, "score": 0.1}}))

    assert drain(env) == 1
    assert len(env.sender.messages) == 1
    message = env.sender.messages[0]
    match_id = event.data["match_id"]
    assert message.to == "owner@example.com"
    assert f"https://app.lostlink.example/matches/{match_id}" in message.text
    assert "<script>" not in message.html and "&lt;script&gt;" in message.html
    assert "owner@example.com" not in repr(message)
    row = notification(env, UUID(match_id))
    assert row.state == "sent" and row.sent_at and row.item_id == item_id
    assert attempts(env, row.id) == [("sent", None)]

    mine = env.client.get("/v1/notifications", headers=headers()).json()["items"]
    assert [n["id"] for n in mine] == [str(row.id)] and mine[0]["state"] == "sent"
    assert "example.com" not in str(mine)
    assert env.client.get("/v1/notifications", headers=headers("other")).json()["items"] == []
    other = env.client.get(f"/v1/notifications/{row.id}", headers=headers("other"))
    assert other.status_code == 404
    assert drain(env) == 0 and len(env.sender.messages) == 1


def test_closed_report_unknown_match_and_mismatch_are_suppressed(notification_env):
    env = notification_env
    report, etag, _, event = matched(env)
    closed = env.matching.post(
        f"/v1/lost-reports/{report['id']}/close",
        headers=headers(**{"Idempotency-Key": uuid4().hex, "If-Match": etag}),
        json={"reason": "found"},
    )
    assert closed.status_code == 200, closed.text
    env.worker.consume(event)
    env.worker.consume(forged(event, match_id=str(uuid4())))
    env.worker.consume(forged(event, match_id=event.data["match_id"], owner_id="other"))
    drain(env)
    assert env.sender.messages == []
    with env.db["notification_worker"].system_session() as session:
        reasons = sorted(
            (row.state, row.reason_code) for row in session.scalars(select(Notification))
        )
    assert reasons == [
        ("suppressed", "context_mismatch"),
        ("suppressed", "match_inactive"),
        ("suppressed", "match_not_found"),
    ]


def test_identity_and_matching_failures(notification_env):
    env = notification_env
    _, _, _, event = matched(env, actor="other")
    env.worker.consume(event)
    drain(env)
    row = notification(env, UUID(event.data["match_id"]))
    assert (row.state, row.reason_code) == ("failed", "email_not_verified")

    _, _, _, second = matched(env, title="red umbrella")
    env.worker.consume(second)
    env.directory.available = False
    assert env.worker.once()
    row = notification(env, UUID(second.data["match_id"]))
    assert row.state == "pending"
    env.directory.available = True
    env.transport.failures = 1
    admin_sql(env, "UPDATE notification.notification_jobs SET available_at = now()")
    assert env.worker.once()
    assert notification(env, UUID(second.data["match_id"])).state == "pending"
    admin_sql(env, "UPDATE notification.notification_jobs SET available_at = now(), attempts = 5")
    env.transport.failures = 0
    assert env.worker.once()
    row = notification(env, UUID(second.data["match_id"]))
    assert (row.state, row.reason_code) == ("failed", "retry_exhausted")
    assert env.sender.messages == []


def test_provider_outcomes_unknown_is_never_resent_automatically(notification_env):
    env = notification_env
    _, _, _, rejected = matched(env, title="red bag one")
    env.sender.outcomes = ["not_sent"]
    env.worker.consume(rejected)
    drain(env)
    row = notification(env, UUID(rejected.data["match_id"]))
    assert (row.state, row.reason_code) == ("failed", "message_rejected")

    _, _, _, throttled = matched(env, title="red bag two")
    env.sender.outcomes = ["throttled"]
    env.worker.consume(throttled)
    assert env.worker.once()
    row = notification(env, UUID(throttled.data["match_id"]))
    assert row.state == "retry_wait"
    admin_sql(env, "UPDATE notification.notification_jobs SET available_at = now()")
    drain(env)
    row = notification(env, UUID(throttled.data["match_id"]))
    assert row.state == "sent"
    assert attempts(env, row.id) == [("not_sent", "provider_throttled"), ("sent", None)]

    _, _, _, unknown = matched(env, title="red bag three")
    env.sender.outcomes = ["unknown"]
    env.worker.consume(unknown)
    drain(env)
    row = notification(env, UUID(unknown.data["match_id"]))
    assert (row.state, row.reason_code) == ("delivery_unknown", "provider_timeout")
    admin_sql(env, "UPDATE notification.notification_jobs SET available_at = now()")
    before = len(env.sender.messages)
    assert drain(env) == 0 and len(env.sender.messages) == before


def test_crash_after_attempt_start_becomes_unknown(notification_env):
    env = notification_env
    _, _, _, event = matched(env)
    env.worker.consume(event)

    class Crash(BaseException):
        pass

    class CrashingSender:
        def send(self, message):
            raise Crash()

    sender, env.worker.sender = env.worker.sender, CrashingSender()
    with pytest.raises(Crash):
        env.worker.once()
    env.worker.sender = sender
    row = notification(env, UUID(event.data["match_id"]))
    assert row.state == "sending"
    admin_sql(
        env, "UPDATE notification.notification_jobs SET lease_until = now() - interval '1 second'"
    )
    assert env.worker.once()
    row = notification(env, UUID(event.data["match_id"]))
    assert (row.state, row.reason_code) == ("delivery_unknown", "worker_interrupted")
    assert attempts(env, row.id) == [("unknown", "worker_interrupted")]
    assert env.sender.messages == []


def test_operator_retry_requires_acknowledgement_and_is_audited(notification_env):
    env = notification_env
    _, _, _, event = matched(env)
    env.sender.outcomes = ["unknown"]
    env.worker.consume(event)
    drain(env)
    row = notification(env, UUID(event.data["match_id"]))
    path = f"/v1/admin/notifications/{row.id}/retry"
    body = {"reason_code": "provider_confirmed_not_sent"}
    for actor in ["owner", "fake-operator"]:
        response = env.client.post(
            path, headers=headers(actor, **{"Idempotency-Key": uuid4().hex}), json=body
        )
        assert response.status_code == 403
    refused = env.client.post(
        path, headers=headers("operator", **{"Idempotency-Key": uuid4().hex}), json=body
    )
    assert refused.status_code == 409
    assert refused.json()["error"]["code"] == "duplicate_risk_not_acknowledged"
    acknowledged = {**body, "acknowledge_duplicate_risk": True}
    key = {"Idempotency-Key": "retry-key-0001"}
    first = env.client.post(path, headers=headers("operator", **key), json=acknowledged)
    assert first.status_code == 200, first.text
    assert first.json()["state"] == "pending" and first.json()["attempts"] == 0
    replay = env.client.post(path, headers=headers("operator", **key), json=acknowledged)
    assert replay.json() == first.json()
    drain(env)
    assert notification(env, row.match_id).state == "sent"
    assert len(env.sender.messages) == 2
    again = env.client.post(
        path,
        headers=headers("operator", **{"Idempotency-Key": uuid4().hex}),
        json=acknowledged,
    )
    assert again.status_code == 409
    listing = env.client.get(
        "/v1/admin/notifications", params={"state": "sent"}, headers=headers("operator")
    ).json()
    assert [item["id"] for item in listing["items"]] == [str(row.id)]
    assert "example.com" not in str(listing)
    stats = env.client.get("/v1/admin/notification-stats", headers=headers("operator")).json()
    assert stats["counts"]["sent"] == 1 and stats["oldest_pending_seconds"] is None
    audit = admin_sql(env, "SELECT action, reason_code FROM notification.audit_entries")
    assert [tuple(row) for row in audit] == [("notification.retry", "provider_confirmed_not_sent")]


def test_database_roles_are_restricted(notification_env):
    env = notification_env
    _, _, _, event = matched(env)
    env.worker.consume(event)
    api, admin, worker = (
        env.db["notification_api"],
        env.db["notification_admin"],
        env.db["notification_worker"],
    )
    with api.scoped_session(subject="other") as session:
        assert session.scalar(text("SELECT count(*) FROM notification.notifications")) == 0
    with api.scoped_session(subject="owner") as session:
        assert session.scalar(text("SELECT count(*) FROM notification.notifications")) == 1
    denied = [
        (api.scoped_session(subject="owner"), "UPDATE notification.notifications SET state='sent'"),
        (api.scoped_session(subject="owner"), "SELECT * FROM notification.notification_jobs"),
        (admin.scoped_session(subject="op"), "UPDATE notification.notifications SET owner_sub='x'"),
        (admin.scoped_session(subject="op"), "SELECT * FROM notification.inbox_receipts"),
        (worker.system_session(), "DELETE FROM notification.notifications"),
        (worker.system_session(), "SELECT * FROM matching.matches"),
    ]
    for opener, statement in denied:
        with pytest.raises(DBAPIError, match="permission denied"):
            with opener as session:
                session.execute(text(statement))


def test_localstack_sns_sqs_consumption_acknowledges_only_after_commit(notification_env):
    env = notification_env
    endpoint = os.getenv("LOSTLINK_TEST_AWS_ENDPOINT")
    if not endpoint:
        pytest.skip("Set LOSTLINK_TEST_AWS_ENDPOINT to a LocalStack endpoint")
    _, _, _, event = matched(env)
    settings = Settings(_env_file=None, aws_endpoint_url=endpoint)
    sns, sqs = aws_client("sns", settings), aws_client("sqs", settings)
    suffix = uuid4().hex[:12]
    topic = sns.create_topic(Name=f"notification-test-{suffix}")["TopicArn"]
    queue_url = sqs.create_queue(QueueName=f"notification-test-{suffix}")["QueueUrl"]
    try:
        queue_arn = sqs.get_queue_attributes(QueueUrl=queue_url, AttributeNames=["QueueArn"])[
            "Attributes"
        ]["QueueArn"]
        sns.subscribe(TopicArn=topic, Protocol="sqs", Endpoint=queue_arn)
        SnsPublisher(sns, topic).publish(event)
        for _ in range(10):
            if env.worker.poll(sqs, queue_url, wait_seconds=1):
                break
        assert notification(env, UUID(event.data["match_id"])).state == "pending"
        sqs.send_message(QueueUrl=queue_url, MessageBody='{"not": "an event"}')
        assert env.worker.poll(sqs, queue_url, wait_seconds=1)
        remaining = sqs.get_queue_attributes(
            QueueUrl=queue_url, AttributeNames=["ApproximateNumberOfMessagesNotVisible"]
        )["Attributes"]["ApproximateNumberOfMessagesNotVisible"]
        assert remaining == "1"
        drain(env)
        assert notification(env, UUID(event.data["match_id"])).state == "sent"
    finally:
        sns.delete_topic(TopicArn=topic)
        sqs.delete_queue(QueueUrl=queue_url)
        sns.close()
        sqs.close()


def test_migration_downgrade_and_upgrade(notification_env):
    env = notification_env
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("MIGRATION_DATABASE_URL", env.migration_url)
        for key, role in env.roles.items():
            patch.setenv(key.upper() + "_ROLE", role)
        command.downgrade(env.configs["notification"], "base")
        command.upgrade(env.configs["notification"], "head")
    env.db["notification_api"].check_ready("notification", "0001_notification")
    _, _, _, event = matched(env)
    assert env.worker.consume(event) == "created"
