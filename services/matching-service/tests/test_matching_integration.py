"""Real pgvector, RLS, Item contracts, durable delivery and stale-event safety."""

from concurrent.futures import ThreadPoolExecutor
from uuid import UUID, uuid4

import pytest
from alembic import command
from item_app.models import Item
from lostlink_common.events import DomainEvent
from lostlink_common.membership import MembershipUnavailable
from matching_app.embedding import EmbeddingError
from matching_app.models import (
    Catalog,
    InboxReceipt,
    ItemVector,
    Match,
    OutboxEvent,
    Report,
    WorkJob,
)
from matching_app.workers import ProtocolError
from matching_support import (
    PHOTO,
    create_item,
    drain,
    headers,
    index_item,
    item_events,
    new_report,
)
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

pytestmark = pytest.mark.integration


def test_search_diagnostics_count_live_checks_without_changing_filtering(matching_env):
    env = matching_env
    red = index_item(env)
    blue = index_item(env, "b", "blue backpack")
    original = env.item_client.context

    def current(item_id):
        context = original(item_id)
        return context.model_copy(update={"state": "handed_over"}) if item_id == red else context

    env.item_client.context = current
    response = env.client.post(
        "/v1/search",
        json={"text": "red backpack", "limit": 1},
        headers=headers(),
    )
    assert response.status_code == 200, response.text
    assert [item["item_id"] for item in response.json()["items"]] == [str(blue)]
    metrics = env.client.get("/metrics").text
    for phase in ("encode", "candidates", "item_context"):
        assert f'lostlink_search_phase_duration_seconds_count{{phase="{phase}"}} 1.0' in metrics
    assert "lostlink_search_item_context_calls_total 2.0" in metrics
    assert "lostlink_search_candidates_sum 2.0" in metrics
    assert "lostlink_search_results_sum 1.0" in metrics
    assert str(red) not in metrics and str(blue) not in metrics

    def unavailable(item_id):
        raise MembershipUnavailable()

    env.item_client.context = unavailable
    assert env.client.post("/v1/search", json={"text": "red"}, headers=headers()).status_code == 503
    metrics = env.client.get("/metrics").text
    assert 'lostlink_search_phase_duration_seconds_count{phase="item_context"} 2.0' in metrics
    assert "lostlink_search_item_context_calls_total 3.0" in metrics
    assert "lostlink_search_results_count 1.0" in metrics


def test_report_ownership_idempotency_etag_and_private_event_payload(matching_env):
    env = matching_env
    key = uuid4().hex
    hdr = headers(**{"Idempotency-Key": key})
    payload = {"description": "red backpack", "photo_base64": PHOTO}
    first = env.client.post("/v1/lost-reports", json=payload, headers=hdr)
    assert first.status_code == 201, first.text
    assert first.headers["ETag"] == '"1"'
    report_id = first.json()["id"]
    assert "embedding" not in first.json() and "photo_base64" not in first.json()

    def unavailable(*_args):
        raise EmbeddingError()

    env.encoder.encode = unavailable
    assert env.client.post("/v1/lost-reports", json=payload, headers=hdr).json() == first.json()
    assert (
        env.client.post(
            "/v1/lost-reports", json={"description": "changed"}, headers=hdr
        ).status_code
        == 409
    )
    assert (
        env.client.get(f"/v1/lost-reports/{report_id}", headers=headers("other")).status_code == 404
    )
    assert env.client.get("/v1/lost-reports", headers=headers("other")).json()["items"] == []
    with env.db["matching_api"].context(subject="other") as session:
        assert session.get(Report, UUID(report_id)) is None
    with env.db["matching_worker"].context() as session:
        events = list(session.scalars(select(OutboxEvent)))
        assert len(events) == 1
        assert events[0].envelope["data"] == {"report_id": report_id, "owner_id": "owner"}
        assert session.scalar(select(func.count()).select_from(WorkJob)) == 1
    path = f"/v1/lost-reports/{report_id}/close"
    assert (
        env.client.post(
            path, json={}, headers=headers(**{"Idempotency-Key": uuid4().hex})
        ).status_code
        == 428
    )
    close_headers = headers(**{"Idempotency-Key": uuid4().hex, "If-Match": '"1"'})
    closed = env.client.post(path, json={}, headers=close_headers)
    assert closed.status_code == 200 and closed.json()["state"] == "closed", closed.text
    assert env.client.post(path, json={}, headers=close_headers).json() == closed.json()
    assert (
        env.client.post(
            path, json={}, headers=headers(**{"Idempotency-Key": uuid4().hex, "If-Match": '"1"'})
        ).status_code
        == 412
    )


def test_end_to_end_item_embedding_cross_venue_search_and_filters(matching_env):
    env = matching_env
    a = index_item(env)
    b = index_item(env, "b", "blue backpack")
    response = env.client.post("/v1/search", json={"text": "red backpack"}, headers=headers())
    assert response.status_code == 200, response.text
    rows = response.json()["items"]
    assert [x["item_id"] for x in rows] == [str(a), str(b)]
    assert rows[0]["score"] > rows[1]["score"]
    assert "private" not in response.text and "photo_bucket" not in response.text
    for filters in [
        {"venue_id": str(env.tenants["a"])},
        {"min_score": 0.5},
        {
            "found_from": "2026-10-19T00:00:00Z",
            "found_until": "2026-10-20T00:00:00Z",
            "venue_id": str(env.tenants["a"]),
        },
    ]:
        result = env.client.post("/v1/search", json={"text": "red", **filters}, headers=headers())
        assert result.status_code == 200, result.text
        assert [x["item_id"] for x in result.json()["items"]] == [str(a)]
    assert (
        env.client.post(
            "/v1/search", json={"text": "red", "category": "keys"}, headers=headers()
        ).json()["items"]
        == []
    )
    photo = env.client.post("/v1/search", json={"photo_base64": PHOTO}, headers=headers())
    assert photo.status_code == 200, photo.text
    assert photo.json()["items"][0]["item_id"] == str(b)
    with env.db["matching_api"].context(subject="owner") as session:
        assert (
            session.scalar(
                text(
                    "SELECT count(*) FROM pg_indexes WHERE schemaname='matching' "
                    "AND indexdef LIKE '%USING hnsw%'"
                )
            )
            == 1
        )


def test_report_before_and_after_index_rematches_once_and_notification_revalidates(matching_env):
    env = matching_env
    early = new_report(env)
    item_id = index_item(env)
    late = new_report(env)
    drain(env)
    for report in [early, late]:
        result = env.client.get(f"/v1/lost-reports/{report['id']}/matches", headers=headers())
        assert result.status_code == 200, result.text
        assert result.json()["items"][0]["item"]["item_id"] == str(item_id)
    with env.db["matching_worker"].context() as session:
        matches = list(session.scalars(select(Match)))
        assert len(matches) == 2
        emitted = [
            row.envelope
            for row in session.scalars(select(OutboxEvent))
            if row.envelope["event_type"] == "match.found"
        ]
        assert len(emitted) == 2
        match_id = matches[0].id
    path = f"/internal/v1/matches/{match_id}/notification-context"
    assert env.client.get(path, headers=headers("owner")).status_code == 403
    assert env.client.get(path, headers=headers("wrong-scope")).status_code == 403
    active = env.client.get(path, headers=headers("notify"))
    assert active.status_code == 200 and active.json()["active"], active.text
    # Authoritative state wins before any matching catalog update is delivered.
    with env.db["item_worker"].context(tenant_id=env.tenants["a"]) as session:
        item = session.get(Item, item_id)
        item.state = "withdrawn"
        item.version += 1
    assert env.client.get(path, headers=headers("notify")).json()["active"] is False
    assert (
        env.client.post("/v1/search", json={"text": "red"}, headers=headers()).json()["items"] == []
    )


def test_catalog_tombstone_out_of_order_and_receipt_conflict(matching_env):
    env = matching_env
    item_id = index_item(env)
    events = [x for x in item_events(env, item_id) if x.event_type == "item.catalog_changed"]
    latest = max(events, key=lambda x: x.data["revision"])
    tombstone = latest.model_copy(
        update={"event_id": uuid4(), "data": {**latest.data, "state": "withdrawn", "revision": 100}}
    )
    assert env.worker.consume(tombstone) == "accepted"
    assert env.worker.consume(tombstone) == "duplicate"
    old = latest.model_copy(update={"event_id": uuid4()})
    env.worker.consume(old)
    with env.db["matching_worker"].context() as session:
        assert session.get(Catalog, item_id).state == "withdrawn"
    conflict = tombstone.model_copy(update={"data": {**tombstone.data, "revision": 101}})
    with pytest.raises(ProtocolError):
        env.worker.consume(conflict)
    wrong = latest.model_copy(update={"event_id": uuid4(), "tenant_id": env.tenants["b"]})
    with pytest.raises(ProtocolError):
        env.worker.consume(wrong)
    with env.db["matching_api"].context(subject="owner") as session:
        assert session.get(Catalog, item_id) is None
        assert session.get(ItemVector, item_id) is None


def test_stale_embedding_is_superseded_and_terminal_failure_reaches_item(matching_env):
    env = matching_env
    item_id = create_item(env)
    event = next(x for x in item_events(env, item_id) if x.event_type == "item.registered")
    stale = event.model_copy(update={"event_id": uuid4(), "correlation_id": uuid4()})
    env.worker.consume(stale)
    drain(env)
    with env.db["matching_worker"].context() as session:
        assert session.scalar(select(WorkJob)).state == "superseded"
        assert session.get(ItemVector, item_id) is None
    env.storage.content = b"invalid"
    env.worker.consume(event)
    drain(env)
    with env.db["matching_worker"].context() as session:
        failed = next(
            x
            for x in session.scalars(select(OutboxEvent))
            if x.envelope["event_type"] == "item.embedding_failed"
        )
        failed_job = session.scalar(select(WorkJob).where(WorkJob.state == "failed"))
    assert failed.envelope["correlation_id"] == str(event.correlation_id)
    assert env.item_worker.consume(DomainEvent.model_validate(failed.envelope)) == "applied"
    with pytest.raises(ValueError, match="through Item"):
        env.worker.retry_failed(WorkJob, failed_job.id, "operator")


def test_worker_lease_recovery_cas_and_outbox_replay(matching_env):
    env = matching_env
    new_report(env)
    first = env.worker.claim(WorkJob)
    assert first is not None
    with env.db["matching_worker"].context() as session:
        session.execute(
            text(
                "UPDATE matching.work_jobs SET lease_until=now()-interval '1 second' WHERE id=:id"
            ),
            {"id": first.id},
        )
    second = env.worker.claim(WorkJob)
    assert second.id == first.id and second.lease_token != first.lease_token
    with env.db["matching_worker"].context() as session:
        assert env.worker.current(session, WorkJob, first) is None
    env.worker.report_match(second)

    class Publisher:
        calls = []

        def publish(self, event):
            self.calls.append(event.event_id)
            raise OSError("offline")

    env.worker.publisher = Publisher()
    assert env.worker.once("outbox")
    with env.db["matching_worker"].context() as session:
        row = session.scalar(select(OutboxEvent))
        assert row.state == "pending" and row.attempts == 1
        row.state = "failed"
        job_id = row.id
    env.worker.retry_failed(OutboxEvent, job_id, "operator")
    env.worker.publisher.publish = lambda event: env.worker.publisher.calls.append(event.event_id)
    assert env.worker.once("outbox")
    assert env.worker.publisher.calls == [job_id, job_id]


def test_restricted_roles_cannot_read_other_services_or_mutate_indexes(matching_env):
    env = matching_env
    for query in [
        "SELECT * FROM item.items",
        "SELECT * FROM matching.inbox_receipts",
        "SELECT * FROM matching.work_jobs",
        "UPDATE matching.catalog SET state='available'",
    ]:
        with pytest.raises(DBAPIError), env.db["matching_api"].context(subject="owner") as session:
            session.execute(text(query))
    with pytest.raises(DBAPIError), env.db["matching_internal"].context() as session:
        session.execute(text("SELECT embedding,description FROM matching.reports"))
    with pytest.raises(DBAPIError), env.db["matching_worker"].context() as session:
        session.execute(text("SELECT * FROM item.items"))
    with env.db["matching_api"].context(subject="owner") as session:
        flags = session.execute(
            text("SELECT rolsuper,rolbypassrls FROM pg_roles WHERE rolname=current_user")
        ).one()
        assert not flags.rolsuper and not flags.rolbypassrls


def test_item_outage_fails_closed(matching_env):
    env = matching_env
    index_item(env)

    def unavailable(_id):
        raise MembershipUnavailable()

    env.app.state.item_client = type("Offline", (), {"context": staticmethod(unavailable)})()
    response = env.client.post("/v1/search", json={"text": "red"}, headers=headers())
    assert response.status_code == 503, response.text


def test_concurrent_report_retries_have_one_report_and_one_job(matching_env):
    env = matching_env
    hdr = headers(**{"Idempotency-Key": uuid4().hex})

    def create(_):
        return env.client.post("/v1/lost-reports", json={"description": "red"}, headers=hdr)

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(create, range(4)))
    assert all(x.status_code in {201, 409} for x in results), [x.text for x in results]
    assert len({x.json()["id"] for x in results if x.status_code == 201}) == 1
    with env.db["matching_worker"].context() as session:
        assert session.scalar(select(func.count()).select_from(Report)) == 1
        assert session.scalar(select(func.count()).select_from(WorkJob)) == 1


def test_migration_downgrade_is_matching_only(matching_env, monkeypatch):
    env = matching_env
    monkeypatch.setenv("MIGRATION_DATABASE_URL", env.migration_url)
    for key, value in env.roles.items():
        monkeypatch.setenv(key.upper() + "_ROLE", value)
    command.downgrade(env.configs["matching"], "base")
    command.upgrade(env.configs["matching"], "head")
    with env.db["item_worker"].context() as session:
        assert session.scalar(text("SELECT count(*) FROM item.items")) == 0


def test_closed_report_never_reactivates_and_notification_scope_has_no_bulk_access(matching_env):
    env = matching_env
    created = new_report(env)
    item_id = index_item(env)
    with env.db["matching_worker"].context() as session:
        match = session.scalar(select(Match))
    result = env.client.post(
        f"/v1/lost-reports/{created['id']}/close",
        json={},
        headers=headers(**{"Idempotency-Key": uuid4().hex, "If-Match": '"1"'}),
    )
    assert result.status_code == 200
    latest = max(
        (x for x in item_events(env, item_id) if x.event_type == "item.catalog_changed"),
        key=lambda x: x.data["revision"],
    )
    env.worker.consume(latest.model_copy(update={"event_id": uuid4()}))
    drain(env)
    value = env.client.get(
        f"/internal/v1/matches/{match.id}/notification-context", headers=headers("notify")
    )
    assert value.status_code == 200 and not value.json()["active"], value.text
    with env.db["matching_internal"].context(resource_id=uuid4()) as session:
        assert session.scalar(select(Match)) is None
    assert (
        env.client.get(f"/v1/lost-reports/{created['id']}/matches", headers=headers()).json()[
            "items"
        ]
        == []
    )


def test_rematch_pages_resume_and_match_events_stay_unique(matching_env):
    env = matching_env
    # Reports use one authenticated owner but distinct IDs, exercising the durable UUID cursor.
    for _ in range(53):
        new_report(env)
    item_id = index_item(env)
    with env.db["matching_worker"].context() as session:
        assert session.scalar(select(func.count()).select_from(Match)) == 53
        assert all(x.state == "completed" for x in session.scalars(select(WorkJob)))
    latest = max(
        (x for x in item_events(env, item_id) if x.event_type == "item.catalog_changed"),
        key=lambda x: x.data["revision"],
    )
    env.worker.consume(latest.model_copy(update={"event_id": uuid4()}))
    drain(env)
    with env.db["matching_worker"].context() as session:
        assert session.scalar(select(func.count()).select_from(Match)) == 53
        assert (
            len(
                [
                    x
                    for x in session.scalars(select(OutboxEvent))
                    if x.envelope["event_type"] == "match.found"
                ]
            )
            == 53
        )


def test_localstack_sns_sqs_inbox_commits_before_ack_and_outbox_roundtrip(matching_env):
    import json
    import os

    from lostlink_common.events import SnsPublisher, aws_client, decode_message

    env = matching_env
    endpoint = os.getenv("LOSTLINK_TEST_AWS_ENDPOINT")
    if not endpoint:
        pytest.skip("Set LOSTLINK_TEST_AWS_ENDPOINT")
    settings = env.worker.settings.model_copy(update={"aws_endpoint_url": endpoint})
    sns, sqs = aws_client("sns", settings), aws_client("sqs", settings)
    topic = queue = None
    try:
        name = f"matching-test-{uuid4().hex}"
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
        item_id = create_item(env)
        original = next(x for x in item_events(env, item_id) if x.event_type == "item.registered")
        publisher = SnsPublisher(sns, topic)
        publisher.publish(original)
        assert env.worker.poll(sqs, queue, wait_seconds=5)
        with env.db["matching_worker"].context() as session:
            assert session.get(InboxReceipt, original.event_id)
            assert session.scalar(select(func.count()).select_from(WorkJob)) == 1
        publisher.publish(original)
        assert env.worker.poll(sqs, queue, wait_seconds=5)
        with env.db["matching_worker"].context() as session:
            assert session.scalar(select(func.count()).select_from(WorkJob)) == 1
        drain(env)
        env.worker.publisher = publisher
        assert env.worker.once("outbox")
        messages = sqs.receive_message(QueueUrl=queue, WaitTimeSeconds=5).get("Messages", [])
        assert len(messages) == 1
        event = decode_message(messages[0]["Body"])
        assert event.event_type == "item.embedded"
        assert event.correlation_id == original.correlation_id
    finally:
        if topic:
            sns.delete_topic(TopicArn=topic)
        if queue:
            sqs.delete_queue(QueueUrl=queue)
        sns.close()
        sqs.close()


def test_saved_query_candidates_match_search_without_changing_automatic_matches(matching_env):
    env = matching_env
    index_item(env, "a", "red backpack")
    index_item(env, "b", "blue backpack")
    report = new_report(env)
    path = f"/v1/lost-reports/{report['id']}/candidates"
    search = env.client.post("/v1/search", json={"text": "red backpack"}, headers=headers())
    first = env.client.get(path + "?limit=1", headers=headers())
    second = env.client.get(path + "?limit=1&offset=1", headers=headers())
    assert first.status_code == second.status_code == 200
    assert first.json()["has_more"] and not second.json()["has_more"]
    items = first.json()["items"] + second.json()["items"]
    assert [x["item_id"] for x in items] == [x["item_id"] for x in search.json()["items"]]
    assert env.client.get(path, headers=headers("other")).status_code == 404
    with env.db["matching_worker"].context() as session:
        assert session.scalar(select(func.count()).select_from(Match)) == 0
    closed = env.client.post(
        f"/v1/lost-reports/{report['id']}/close",
        headers=headers(**{"Idempotency-Key": uuid4().hex, "If-Match": '"1"'}),
        json={},
    )
    assert closed.status_code == 200
    assert env.client.get(path, headers=headers()).json()["items"] == []
