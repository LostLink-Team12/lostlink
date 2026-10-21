"""Real SQL/RLS/state transitions, actual Venue contracts and optional LocalStack media."""

import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from alembic import command
from item_app.cleanup import cleanup
from item_app.models import Item, ItemLocator, OutboxEvent, Photo, Question, WorkJob
from item_app.workers import ItemWorker, ProtocolError
from lostlink_common.events import DomainEvent, SnsPublisher, aws_client, decode_message
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.exc import DBAPIError

pytestmark = pytest.mark.integration


def headers(actor="a", *, key=None, version=None):
    value = {"Authorization": f"Bearer {actor}", "Idempotency-Key": key or uuid4().hex}
    if version is not None:
        value["If-Match"] = f'"{version}"'
    return value


def body():
    return {
        "title": "Blue umbrella",
        "description": "Blue umbrella found near the entrance",
        "category": "umbrella",
        "found_at": datetime.now(UTC).isoformat(),
        "verification_questions": [
            {"prompt": "What initials are inside?", "reference_answer": "PRIVATE-XY"}
        ],
    }


def create(env, actor="a"):
    response = env.client.post("/v1/items", headers=headers(actor), json=body())
    assert response.status_code == 201, response.text
    return response.json()


def processing(env, *, s3=None):
    item = create(env)
    response = env.client.post(
        f"/v1/items/{item['id']}/uploads",
        headers=headers(version=1),
        json={"content_type": "image/png", "size": 3},
    )
    assert response.status_code == 201, response.text
    upload = response.json()
    if s3:
        client, bucket = s3
        client.put_object(
            Bucket=bucket,
            Key=f"staging/{env.tenants['a']}/{item['id']}/{upload['upload_id']}",
            Body=b"png",
            ContentType="image/png",
        )
    response = env.client.post(
        f"/v1/items/{item['id']}/uploads/{upload['upload_id']}/confirm", headers=headers()
    )
    assert response.status_code == 202, response.text
    assert env.worker.once("tasks")
    current = env.client.get(f"/v1/items/{item['id']}", headers=headers()).json()
    assert current["state"] == "processing", current
    return current, upload, response.json()["operation_id"]


def embedding(env, item, *, failed=False, correlation=None, tenant=None):
    context = env.client.get(
        f"/internal/v1/items/{item['id']}/index-context", headers=headers("index")
    )
    assert context.status_code == 200, context.text
    return DomainEvent(
        event_type="item.embedding_failed" if failed else "item.embedded",
        source="embedding-worker",
        tenant_id=tenant or env.tenants["a"],
        correlation_id=correlation or UUID(context.json()["processing_correlation_id"]),
        data={
            "item_id": item["id"],
            "item_version": item["item_version"],
            **(
                {"error_code": "model_failure"}
                if failed
                else {"model_version": "test-model", "dimensions": 512}
            ),
        },
    )


def available(env):
    item, _, _ = processing(env)
    assert env.worker.consume(embedding(env, item)) == "applied"
    value = env.client.get(f"/v1/items/{item['id']}", headers=headers()).json()
    assert value["state"] == "available"
    return value


def drain(env):
    count = 0
    while env.worker.once("tasks"):
        count += 1
    return count


def test_item_crud_rls_hidden_fields_etags_and_real_venue_revocation(item_env):
    env = item_env
    request, request_headers = body(), headers(key="same-item-request")
    first = env.client.post("/v1/items", headers=request_headers, json=request)
    assert first.status_code == 201, first.text
    assert (
        env.client.post("/v1/items", headers=request_headers, json=request).json() == first.json()
    )
    item = first.json()
    assert "PRIVATE-XY" not in first.text
    listing = env.client.get("/v1/items", headers=headers())
    assert "PRIVATE-XY" not in listing.text
    detail = env.client.get(f"/v1/items/{item['id']}", headers=headers())
    assert detail.headers["etag"] == '"1"' and "PRIVATE-XY" in detail.text
    assert env.client.get(f"/v1/items/{item['id']}", headers=headers("b")).status_code == 404
    assert env.client.get("/v1/items", headers=headers("b")).json()["items"] == []
    assert env.client.get("/v1/items", headers=headers("owner")).status_code == 403
    with env.db["item_api"].context(tenant_id=env.tenants["b"]) as session:
        assert session.scalars(select(Item)).all() == []
    with env.db["item_api"].context() as session:
        assert session.scalars(select(Item)).all() == []
    # Add a second admin, then suspend the first through the real Venue API.
    response = env.venue.post(
        "/v1/venues/current/members",
        headers=headers(),
        json={"user_sub": "second-admin", "role": "venue_admin"},
    )
    assert response.status_code == 201
    response = env.venue.patch(
        f"/v1/venues/current/members/{env.admins['a']}",
        headers=headers(version=1),
        json={"status": "suspended"},
    )
    assert response.status_code == 200
    assert env.client.get("/v1/items", headers=headers()).status_code == 403


def test_patch_versions_and_events_never_expose_hidden_answers(item_env):
    env = item_env
    item = create(env)
    path = f"/v1/items/{item['id']}"
    assert env.client.patch(path, headers=headers(), json={"title": "Changed"}).status_code == 428
    request_headers = headers(key="change-content", version=1)
    changed = env.client.patch(path, headers=request_headers, json={"title": "Changed"})
    assert changed.status_code == 200 and changed.json()["item_version"] == 2
    assert (
        env.client.patch(path, headers=request_headers, json={"title": "Changed"}).json()
        == changed.json()
    )
    assert (
        env.client.patch(path, headers=headers(version=1), json={"title": "Older"}).status_code
        == 412
    )
    context = env.client.get(
        f"/internal/v1/items/{item['id']}/claim-context", headers=headers("claim")
    )
    assert context.status_code == 200 and "PRIVATE-XY" not in context.text
    assert context.json()["questions"][0]["version"] == 2
    with env.db["item_worker"].context() as session:
        for row in session.scalars(select(OutboxEvent)):
            assert "PRIVATE-XY" not in json.dumps(row.envelope)
    with (
        pytest.raises(DBAPIError),
        env.db["item_internal"].context(tenant_id=env.tenants["a"]) as session,
    ):
        session.scalars(select(Question)).all()
    with env.db["item_internal"].context() as session:
        assert session.scalars(select(ItemLocator)).all() == []
    assert (
        env.client.get(
            f"/internal/v1/items/{item['id']}/claim-context", headers=headers()
        ).status_code
        == 403
    )


def test_photo_confirmation_is_durable_and_embedding_results_are_deduplicated(item_env):
    env = item_env
    item, upload, operation_id = processing(env)
    operation = env.client.get(f"/v1/item-operations/{operation_id}", headers=headers()).json()
    assert operation["state"] == "completed"
    assert (
        env.client.post(
            f"/v1/items/{item['id']}/uploads/{upload['upload_id']}/confirm", headers=headers()
        ).json()["operation_id"]
        == operation_id
    )
    event = embedding(env, item)
    assert env.worker.consume(event) == "applied"
    assert env.worker.consume(event) == "duplicate"
    changed = event.model_copy(update={"data": {**event.data, "model_version": "other"}})
    with pytest.raises(ProtocolError):
        env.worker.consume(changed)
    assert (
        env.client.get(f"/v1/items/{item['id']}", headers=headers()).json()["state"] == "available"
    )


def test_old_processing_rounds_and_withdrawn_items_cannot_be_restored(item_env):
    env = item_env
    item, _, _ = processing(env)
    old_result = embedding(env, item)
    assert env.worker.consume(embedding(env, item, failed=True)) == "applied"
    failed = env.client.get(f"/v1/items/{item['id']}", headers=headers()).json()
    response = env.client.post(
        f"/v1/items/{item['id']}/retry-embedding", headers=headers(version=failed["version"])
    )
    assert response.status_code == 202
    assert env.worker.consume(old_result) == "ignored"
    current = response.json()
    new_result = embedding(env, current)
    assert (
        env.client.post(
            f"/v1/items/{item['id']}/withdraw",
            headers=headers(version=current["version"]),
            json={"reason_code": "duplicate"},
        ).status_code
        == 200
    )
    assert env.worker.consume(new_result) == "ignored"
    assert (
        env.client.get(f"/v1/items/{item['id']}", headers=headers()).json()["state"] == "withdrawn"
    )
    with pytest.raises(ProtocolError):
        env.worker.consume(embedding(env, item, correlation=uuid4(), tenant=env.tenants["b"]))


def test_reservation_concurrency_release_tombstones_and_idempotent_handover(item_env):
    env = item_env
    item = available(env)
    path = f"/internal/v1/items/{item['id']}/reservations"
    claims = [uuid4(), uuid4()]

    def reserve(claim):
        return env.client.post(
            path,
            headers=headers("claim"),
            json={"claim_id": str(claim), "item_version": item["item_version"]},
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(executor.map(reserve, claims))
    assert sorted(response.status_code for response in responses) == [200, 409]
    winner = claims[
        next(index for index, response in enumerate(responses) if response.status_code == 200)
    ]
    assert env.client.patch(
        f"/v1/items/{item['id']}", headers=headers(version=99), json={"title": "changed"}
    ).status_code in {409, 412}
    assert (
        env.client.post(
            path + f"/{winner}/release",
            headers=headers("claim"),
            json={"reason_code": "claim_cancelled"},
        ).status_code
        == 200
    )
    assert reserve(winner).status_code == 409
    tombstone = uuid4()
    assert (
        env.client.post(
            path + f"/{tombstone}/release",
            headers=headers("claim"),
            json={"reason_code": "recovery"},
        ).status_code
        == 200
    )
    assert reserve(tombstone).status_code == 409
    next_claim = uuid4()
    assert reserve(next_claim).status_code == 200
    handover = {"handover_id": str(uuid4()), "mode": "counter"}
    target = path + f"/{next_claim}/commit-handover"
    assert env.client.post(target, headers=headers("claim"), json=handover).status_code == 200
    assert env.client.post(target, headers=headers("claim"), json=handover).status_code == 200
    assert (
        env.client.post(
            path + f"/{next_claim}/release",
            headers=headers("claim"),
            json={"reason_code": "recovery"},
        ).status_code
        == 409
    )
    assert (
        env.client.post(
            target, headers=headers("claim"), json={**handover, "handover_id": str(uuid4())}
        ).status_code
        == 409
    )
    assert (
        env.client.get(f"/v1/items/{item['id']}", headers=headers()).json()["state"]
        == "handed_over"
    )


def test_bulk_import_partial_failure_repairs_only_failed_rows_and_stable_batches(item_env):
    env = item_env
    rows = [{**body(), "client_reference": str(index)} for index in range(100)]
    rows[7]["photo_url"] = "https://untrusted.invalid/photo"
    manifest = {"batch_reference": "batch-one", "entries": rows}
    response = env.client.post(
        "/v1/item-imports", headers=headers(key="import-batch-one"), json=manifest
    )
    assert response.status_code == 202, response.text
    job_id = response.json()["id"]
    assert (
        env.client.post("/v1/item-imports", headers=headers(), json=manifest).json()["id"] == job_id
    )
    # Surface a first-row SQL failure directly rather than silently exercising retry loops.
    env.worker.import_entry(env.worker.claim(WorkJob))
    assert drain(env) == 99
    status = env.client.get(f"/v1/item-imports/{job_id}?limit=100", headers=headers()).json()
    assert status["job"]["accepted"] == 99 and status["job"]["failed"] == 1
    assert status["job"]["state"] == "completed_with_errors"
    assert "PRIVATE-XY" not in json.dumps(status)
    failed = next(row for row in status["items"] if row["state"] == "failed")
    repair = {"entries": [{"entry_id": failed["id"], "item": body()}]}
    response = env.client.post(f"/v1/item-imports/{job_id}/retry", headers=headers(), json=repair)
    assert response.status_code == 202, response.text
    assert drain(env) == 1
    final = env.client.get(f"/v1/item-imports/{job_id}", headers=headers()).json()["job"]
    assert final["accepted"] == 100 and final["failed"] == 0
    assert (
        env.client.post(
            f"/v1/item-imports/{job_id}/retry", headers=headers(), json=repair
        ).status_code
        == 409
    )
    with env.db["item_api"].context(tenant_id=env.tenants["a"]) as session:
        assert session.scalar(select(func.count()).select_from(Item)) == 100
    assert env.client.get(f"/v1/item-imports/{job_id}", headers=headers("b")).status_code == 404


def test_task_crash_lease_reclaim_and_late_ack_cannot_change_final_state(item_env):
    env = item_env
    item = create(env)
    upload = env.client.post(
        f"/v1/items/{item['id']}/uploads",
        headers=headers(version=1),
        json={"size": 3, "content_type": "image/png"},
    ).json()
    env.client.post(
        f"/v1/items/{item['id']}/uploads/{upload['upload_id']}/confirm", headers=headers()
    )
    first = env.worker.claim(WorkJob)
    assert first and env.worker.claim(WorkJob) is None
    with env.db["item_worker"].context() as session:
        session.get(WorkJob, first.id).lease_until = datetime.now(UTC) - timedelta(seconds=1)
    second = env.worker.claim(WorkJob)
    assert second.lease_token != first.lease_token
    env.worker.photo(first)
    assert (
        env.client.get(f"/v1/items/{item['id']}", headers=headers()).json()["state"]
        == "awaiting_upload"
    )
    env.worker.photo(second)
    assert (
        env.client.get(f"/v1/items/{item['id']}", headers=headers()).json()["state"] == "processing"
    )


def test_photo_replacement_and_source_overwrite_cannot_change_confirmed_sealed_bytes(
    item_env, item_s3
):
    env = item_env
    item, upload, _ = processing(env, s3=item_s3)
    s3, bucket = item_s3
    source = f"staging/{env.tenants['a']}/{item['id']}/{upload['upload_id']}"
    sealed = f"sealed/{env.tenants['a']}/{item['id']}/1/{upload['upload_id']}"
    assert s3.get_object(Bucket=bucket, Key=sealed)["Body"].read() == b"png"
    s3.put_object(Bucket=bucket, Key=source, Body=b"new", ContentType="image/png")
    with env.db["item_worker"].context(tenant_id=env.tenants["a"]) as session:
        photo = session.get(Photo, UUID(upload["upload_id"]))
    assert env.worker.storage.seal(photo) == photo.etag
    assert s3.get_object(Bucket=bucket, Key=sealed)["Body"].read() == b"png"
    second = env.client.post(
        f"/v1/items/{item['id']}/uploads",
        headers=headers(version=item["version"]),
        json={"size": 3, "content_type": "image/png"},
    )
    assert second.status_code == 201 and second.json()["item_version"] == 2
    assert env.worker.consume(embedding(env, item, correlation=uuid4())) == "ignored"


def test_missing_and_invalid_photos_never_publish_registration(item_env, item_s3):
    env = item_env
    item = create(env)
    upload = env.client.post(
        f"/v1/items/{item['id']}/uploads",
        headers=headers(version=1),
        json={"size": 3, "content_type": "image/png"},
    ).json()
    response = env.client.post(
        f"/v1/items/{item['id']}/uploads/{upload['upload_id']}/confirm", headers=headers()
    )
    operation_id = UUID(response.json()["operation_id"])
    assert env.worker.once("tasks")
    with env.db["item_worker"].context() as session:
        job = session.get(WorkJob, operation_id)
        assert job.state == "pending" and job.last_error_code == "photo_not_uploaded"
        job.available_at = datetime.now(UTC) - timedelta(seconds=1)
    client, bucket = item_s3
    client.put_object(
        Bucket=bucket,
        Key=f"staging/{env.tenants['a']}/{item['id']}/{upload['upload_id']}",
        Body=b"wrong size",
        ContentType="image/png",
    )
    assert env.worker.once("tasks")
    with env.db["item_worker"].context() as session:
        assert session.get(WorkJob, operation_id).state == "failed"
        assert all(
            row.envelope["event_type"] != "item.registered"
            for row in session.scalars(select(OutboxEvent))
        )
    assert (
        env.client.get(f"/v1/items/{item['id']}", headers=headers()).json()["state"]
        == "awaiting_upload"
    )


def test_cleanup_retains_confirmed_photos_and_only_removes_old_unreferenced_keys(item_env, item_s3):
    env = item_env
    item, upload, _ = processing(env, s3=item_s3)
    client, bucket = item_s3
    stray = f"sealed/{env.tenants['a']}/{uuid4()}/1/{uuid4()}"
    client.put_object(Bucket=bucket, Key=stray, Body=b"orphan")
    # Moving the cleanup clock proves the 24-hour grace period without changing real objects.
    assert cleanup(env.db["item_worker"], env.worker.storage, bucket) == 0
    assert (
        cleanup(
            env.db["item_worker"],
            env.worker.storage,
            bucket,
            now=datetime.now(UTC) + timedelta(hours=25),
        )
        == 2
    )  # staging + orphan, not sealed
    sealed = f"sealed/{env.tenants['a']}/{item['id']}/1/{upload['upload_id']}"
    assert client.get_object(Bucket=bucket, Key=sealed)["Body"].read() == b"png"


def test_migrations_roles_and_unknown_internal_clients_fail_closed(item_env):
    env = item_env
    env.db["item_api"].check_ready("item", "0001_item")
    item = create(env)
    for mode, sql in [
        ("item_api", "SELECT * FROM venue.memberships"),
        ("item_api", "UPDATE item.outbox_events SET state='sent'"),
        ("item_internal", "SELECT * FROM item.import_entries"),
        ("item_worker", "DELETE FROM item.audit_entries"),
    ]:
        with pytest.raises(DBAPIError), env.db[mode].context(tenant_id=env.tenants["a"]) as session:
            session.execute(text(sql))
    assert (
        env.client.get(
            f"/internal/v1/items/{item['id']}/index-context", headers=headers("claim")
        ).status_code
        == 403
    )
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("MIGRATION_DATABASE_URL", env.migration_url)
        for name, role in env.roles.items():
            patch.setenv(name.upper() + "_ROLE", role)
        command.check(env.configs["item"])
        command.downgrade(env.configs["item"], "base")
        command.upgrade(env.configs["item"], "head")
    env.db["item_api"].check_ready("item", "0001_item")


def test_real_message_pipeline_publishes_committed_events_and_consumes_results(item_env, item_s3):
    env = item_env
    item, _, _ = processing(env, s3=item_s3)
    from lostlink_common.config import Settings

    settings = Settings(
        _env_file=None,
        environment="test",
        aws_endpoint_url=os.environ["LOSTLINK_TEST_AWS_ENDPOINT"],
    )
    sns, sqs = aws_client("sns", settings), aws_client("sqs", settings)
    topic, outgoing, incoming = None, None, None
    try:
        name = "item-test-" + uuid4().hex
        topic = sns.create_topic(Name=name)["TopicArn"]
        outgoing = sqs.create_queue(QueueName=name + "-events")["QueueUrl"]
        incoming = sqs.create_queue(QueueName=name + "-results")["QueueUrl"]
        arn = sqs.get_queue_attributes(QueueUrl=outgoing, AttributeNames=["QueueArn"])[
            "Attributes"
        ]["QueueArn"]
        sqs.set_queue_attributes(
            QueueUrl=outgoing,
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
        publisher = ItemWorker(env.db["item_worker"], publisher=SnsPublisher(sns, topic))
        count = 0
        while publisher.once("outbox"):
            count += 1
        assert count == 4
        messages = sqs.receive_message(
            QueueUrl=outgoing, WaitTimeSeconds=5, MaxNumberOfMessages=10
        ).get("Messages", [])
        events = [decode_message(message["Body"]) for message in messages]
        assert len(events) == 4 and any(event.event_type == "item.registered" for event in events)
        assert all("PRIVATE-XY" not in event.to_message() for event in events)
        result = embedding(env, item)
        sqs.send_message(QueueUrl=incoming, MessageBody=result.to_message())
        assert env.worker.poll(sqs, incoming, wait_seconds=1)
        assert (
            env.client.get(f"/v1/items/{item['id']}", headers=headers()).json()["state"]
            == "available"
        )
        assert sqs.receive_message(QueueUrl=incoming, WaitTimeSeconds=1).get("Messages", []) == []
    finally:
        if topic:
            sns.delete_topic(TopicArn=topic)
        for queue in [outgoing, incoming]:
            if queue:
                sqs.delete_queue(QueueUrl=queue)
        sns.close()
        sqs.close()


def test_membership_failure_returns_503_and_never_accepts_a_write(item_env):
    from lostlink_common.membership import MembershipUnavailable

    env = item_env

    def unavailable(*_):
        raise MembershipUnavailable()

    env.app.state.membership_client.transport = unavailable
    assert env.client.post("/v1/items", headers=headers(), json=body()).status_code == 503
    with env.db["item_api"].context(tenant_id=env.tenants["a"]) as session:
        assert session.scalar(select(func.count()).select_from(Item)) == 0


def test_failed_delivery_replay_is_audited_and_does_not_change_event_id(item_env):
    env = item_env
    create(env)

    class Broken:
        def publish(self, _):
            raise OSError("SNS unavailable")

    worker = ItemWorker(env.db["item_worker"], publisher=Broken())
    with env.db["item_worker"].context() as session:
        row = session.scalar(select(OutboxEvent))
        event_id, envelope = row.id, row.envelope
        row.attempts = 9
    assert worker.once("outbox")
    with env.db["item_worker"].context() as session:
        assert session.get(OutboxEvent, event_id).state == "failed"
    worker.retry_failed("outbox", event_id, "operator", "dependency_restored")

    class Capture:
        def publish(self, event):
            assert event.event_id == event_id
            assert event.model_dump(mode="json") == envelope

    worker.publisher = Capture()
    assert worker.once("outbox")
    engine = create_engine(env.migration_url)
    try:
        with engine.connect() as connection:
            assert (
                connection.scalar(
                    text(
                        "SELECT count(*) FROM item.audit_entries "
                        "WHERE action='outbox.retry.dependency_restored' AND resource_id=:id"
                    ),
                    {"id": event_id},
                )
                == 1
            )
    finally:
        engine.dispose()


def test_old_reservation_creates_attention_without_releasing_the_item(item_env):
    env = item_env
    item = available(env)
    response = env.client.post(
        f"/internal/v1/items/{item['id']}/reservations",
        headers=headers("claim"),
        json={"claim_id": str(uuid4()), "item_version": item["item_version"]},
    )
    assert response.status_code == 200
    with env.db["item_worker"].context() as session:
        job = session.scalar(select(WorkJob).where(WorkJob.kind == "reservation_watch"))
        job.available_at = datetime.now(UTC) - timedelta(seconds=1)
    assert env.worker.once("tasks")
    assert (
        env.client.get(f"/v1/items/{item['id']}", headers=headers()).json()["state"] == "reserved"
    )
    with env.db["item_worker"].context() as session:
        job = session.scalar(select(WorkJob).where(WorkJob.kind == "reservation_watch"))
        assert (
            job.state == "needs_attention"
            and job.last_error_code == "reservation_recovery_required"
        )


@pytest.mark.scale
def test_five_thousand_records_are_imported_in_resumable_batches(item_env):
    if os.getenv("LOSTLINK_TEST_SCALE") != "1":
        pytest.skip("Set LOSTLINK_TEST_SCALE=1 for the 5,000-record database exercise")
    env = item_env
    base = body()
    for start in range(0, 5000, 100):
        manifest = {
            "batch_reference": f"scale-{start}",
            "entries": [
                {**base, "client_reference": str(index)} for index in range(start, start + 100)
            ],
        }
        response = env.client.post("/v1/item-imports", headers=headers(), json=manifest)
        assert response.status_code == 202, response.text
        assert drain(env) == 100
        assert (
            env.client.post("/v1/item-imports", headers=headers(), json=manifest).json()["id"]
            == response.json()["id"]
        )
    with env.db["item_api"].context(tenant_id=env.tenants["a"]) as session:
        assert session.scalar(select(func.count()).select_from(Item)) == 5000
