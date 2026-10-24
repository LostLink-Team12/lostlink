"""Recovery, races, QR attacks and access boundaries against real Venue/Item/PostgreSQL."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from alembic import command
from claim_app.clients import ItemRejected
from claim_app.models import ClaimOperation, HandoverToken
from claim_app.service import ClaimService
from claim_support import (
    approve,
    claim_events,
    drain,
    headers,
    issue_token,
    item_state,
    owner_claim,
    questions,
    scan,
    submit,
)
from claim_support import available_item as make_item
from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import DBAPIError

pytestmark = pytest.mark.integration


def admin_sql(env, statement, **params):
    engine = create_engine(env.migration_url)
    try:
        with engine.begin() as connection:
            connection.execute(text(statement), params)
    finally:
        engine.dispose()


def operation_row(env, operation_id):
    with env.db["claim_worker"].system_session() as session:
        return session.get(ClaimOperation, UUID(str(operation_id)))


def claim_body(env, item_id):
    return {
        "item_id": str(item_id),
        "answers": [
            {"question_id": q["id"], "version": q["version"], "answer": "x"}
            for q in questions(env, item_id)["questions"]
        ],
    }


def test_idempotency_versions_and_replay_before_item_checks(claim_env):
    env = claim_env
    item_id = make_item(env)
    body = claim_body(env, item_id)
    first = env.client.post("/v1/claims", headers=headers(key="create-claim-1"), json=body)
    assert first.status_code == 201, first.text
    claim = first.json()
    changed = {**body, "answers": body["answers"][:1]}
    conflict = env.client.post("/v1/claims", headers=headers(key="create-claim-1"), json=changed)
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "idempotency_conflict"
    duplicate = env.client.post("/v1/claims", headers=headers(), json=body)
    assert duplicate.status_code == 409 and duplicate.json()["error"]["code"] == "claim_exists"
    missing = env.client.post("/v1/claims", headers={"Authorization": "Bearer owner"}, json=body)
    assert missing.status_code == 428

    path = f"/v1/venue/claims/{claim['id']}/approve"
    assert env.client.post(path, headers=headers("a")).status_code == 428
    assert env.client.post(path, headers=headers("a", version=7)).status_code == 412
    assert env.client.post(path, headers=headers("a", version="7")).status_code == 422
    one = env.client.post(path, headers=headers("a", key="approve-key-1", version=1))
    assert one.status_code == 202, one.text
    drain(env)
    # The item is reserved now, yet replays still return the original committed outcomes.
    replay = env.client.post(path, headers=headers("a", key="approve-key-1", version=1))
    assert replay.status_code == 202 and replay.json() == one.json()
    again = env.client.post("/v1/claims", headers=headers(key="create-claim-1"), json=body)
    assert again.status_code == 201 and again.json() == claim
    other = env.client.post(path, headers=headers("a", version=3))
    assert other.status_code == 409 and other.json()["error"]["code"] == "claim_not_submitted"


def test_two_owners_race_for_one_item_and_only_one_is_approved(claim_env):
    env = claim_env
    item_id = make_item(env)
    first, second = submit(env, item_id), submit(env, item_id, "owner2")

    def approve_call(claim):
        return env.client.post(
            f"/v1/venue/claims/{claim['id']}/approve", headers=headers("a", version=1)
        ).status_code

    with ThreadPoolExecutor(2) as pool:
        codes = sorted(pool.map(approve_call, [first, second]))
    assert codes == [202, 409]
    drain(env)
    states = sorted(
        owner_claim(env, claim["id"], actor)["state"]
        for claim, actor in [(first, "owner"), (second, "owner2")]
    )
    assert states == ["approved", "submitted"]
    _, reservations = item_state(env, item_id)
    assert len(reservations) == 1 and reservations[0][1] == "held"


def test_item_content_change_rejects_stale_approval_without_reserving(claim_env):
    env = claim_env
    item_id = make_item(env)
    claim = submit(env, item_id)
    detail = env.item.get(f"/v1/items/{item_id}", headers=headers("a")).json()
    changed = env.item.patch(
        f"/v1/items/{item_id}",
        headers=headers("a", version=detail["version"]),
        json={"description": "Updated description after the claim"},
    )
    assert changed.status_code == 200, changed.text
    approve(env, claim)
    rejected = owner_claim(env, claim["id"])
    assert rejected["state"] == "rejected" and rejected["decision_code"] == "item_unavailable"
    assert item_state(env, item_id)[1] == []


@pytest.mark.parametrize("clock_skew_seconds", [0, 3600, -3600])
def test_cancel_during_approval_releases_and_late_reserve_cannot_win(
    claim_env, monkeypatch, clock_skew_seconds
):
    env = claim_env
    item_id = make_item(env)
    claim = submit(env, item_id)
    accepted = approve(env, claim, run=False)
    # Only the cancelling API's clock changes; workers schedule using PostgreSQL time.
    monkeypatch.setattr(
        ClaimService, "now", lambda self: datetime.now(UTC) + timedelta(seconds=clock_skew_seconds)
    )
    cancelled = env.client.post(
        f"/v1/claims/{claim['id']}/cancel",
        headers=headers(version=accepted["version"]),
        json={"reason_code": "found_elsewhere"},
    )
    assert cancelled.status_code == 202, cancelled.text
    assert cancelled.json()["operation_id"] == accepted["operation_id"]
    drain(env)
    final = owner_claim(env, claim["id"])
    assert final["state"] == "cancelled" and final["decision_code"] == "found_elsewhere"
    state, reservations = item_state(env, item_id)
    assert state == "available" and reservations[0][1] == "released"
    with pytest.raises(ItemRejected) as late:
        env.item_client.reserve(item_id, UUID(claim["id"]), claim["item_version"])
    assert late.value.code == "reservation_closed"
    assert item_state(env, item_id)[0] == "available"
    events = claim_events(env, claim["id"])
    assert [e.event_type for e in events] == ["claim.cancelled"]
    assert events[0].data["reason_code"] == "found_elsewhere"
    other = submit(env, item_id, "owner2")
    approve(env, other)
    assert owner_claim(env, other["id"], "owner2")["state"] == "approved"


def test_cancel_after_approval_revokes_qr_and_submitted_cancel_is_local(claim_env):
    env = claim_env
    item_id = make_item(env)
    claim = submit(env, item_id)
    approve(env, claim)
    current = owner_claim(env, claim["id"])
    token = issue_token(env, claim["id"])
    response = env.client.post(
        f"/v1/claims/{claim['id']}/cancel",
        headers=headers(version=current["version"]),
        json={"reason_code": "no_longer_needed"},
    )
    assert response.status_code == 202
    blocked = scan(env, claim["id"], token["qr_payload"], version=response.json()["version"])
    assert blocked.status_code == 409
    drain(env)
    assert owner_claim(env, claim["id"])["state"] == "cancelled"
    assert item_state(env, item_id)[0] == "available"

    second = submit(env, item_id)
    simple = env.client.post(
        f"/v1/claims/{second['id']}/cancel",
        headers=headers(version=1),
        json={"reason_code": "submitted_in_error"},
    )
    assert simple.status_code == 200 and simple.json()["state"] == "cancelled"
    invalid = env.client.post(
        f"/v1/claims/{second['id']}/cancel",
        headers=headers(version=2),
        json={"reason_code": "free text"},
    )
    assert invalid.status_code == 422


def test_crash_after_remote_reserve_recovers_without_double_reservation(claim_env):
    env = claim_env
    item_id = make_item(env)
    claim = submit(env, item_id)
    accepted = approve(env, claim, run=False)
    claimed = env.worker.claim(ClaimOperation)
    action, *rest = env.worker.snapshot(claimed)
    assert env.worker.call_item(action, claimed.claim_id, *rest) == "held"
    # The process dies before its local commit, so only lease expiry can recover it.
    admin_sql(
        env,
        "UPDATE claim.claim_operations SET lease_until = now() - interval '1 second' "
        "WHERE id = :id",
        id=accepted["operation_id"],
    )
    assert owner_claim(env, claim["id"])["state"] == "approval_pending"
    drain(env)
    assert owner_claim(env, claim["id"])["state"] == "approved"
    row = operation_row(env, accepted["operation_id"])
    assert row.attempts == 2 and row.result_code == "approved"
    _, reservations = item_state(env, item_id)
    assert len(reservations) == 1 and reservations[0][1] == "held"
    assert [e.event_type for e in claim_events(env, claim["id"])] == ["claim.approved"]


def test_lost_response_outage_needs_attention_and_audited_replay(claim_env):
    env = claim_env
    item_id = make_item(env)
    claim = submit(env, item_id)
    accepted = approve(env, claim, run=False)
    # Reserve commits at Item but the response is lost; the status lookup confirms the hold.
    env.transport.failures = ["lost"]
    assert env.worker.once()
    assert owner_claim(env, claim["id"])["state"] == "approved"

    other = submit(env, make_item(env, title="Silver watch"))
    pending = approve(env, other, run=False)
    env.transport.failures = ["before", "before"]
    assert env.worker.once()
    row = operation_row(env, pending["operation_id"])
    assert row.state == "pending" and row.last_error_code == "item_unavailable"
    assert owner_claim(env, other["id"])["state"] == "approval_pending"
    admin_sql(
        env,
        "UPDATE claim.claim_operations SET attempts = 10, available_at = now() WHERE id = :id",
        id=pending["operation_id"],
    )
    assert env.worker.once() is False
    assert operation_row(env, pending["operation_id"]).state == "needs_attention"
    status = env.client.get(
        f"/v1/claim-operations/{pending['operation_id']}", headers=headers("owner")
    ).json()
    assert status["state"] == "needs_attention" and status["claim_state"] == "approval_pending"
    with pytest.raises(ValueError):
        env.worker.replay("tasks", UUID(accepted["operation_id"]), "operator", "manual_review")
    env.transport.failures = []
    env.worker.replay("tasks", UUID(pending["operation_id"]), "operator", "dependency_restored")
    drain(env)
    assert owner_claim(env, other["id"])["state"] == "approved"


def test_qr_codes_are_single_use_bound_and_replayable_only_briefly(claim_env):
    env = claim_env
    item_id = make_item(env)
    claim = submit(env, item_id)
    approve(env, claim)
    version = owner_claim(env, claim["id"])["version"]
    first = issue_token(env, claim["id"], key="token-key-1")
    assert issue_token(env, claim["id"], key="token-key-1") == first
    second = issue_token(env, claim["id"])
    assert second["qr_payload"] != first["qr_payload"]
    stale = scan(env, claim["id"], first["qr_payload"], version=version)
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "token_used"
    replay_old = env.client.post(
        f"/v1/claims/{claim['id']}/handover-token", headers=headers(key="token-key-1")
    )
    assert replay_old.status_code == 409
    suffix = "AA" if not second["qr_payload"].endswith("AA") else "BB"
    tampered = scan(env, claim["id"], second["qr_payload"][:-2] + suffix, version=version)
    assert tampered.status_code == 409 and tampered.json()["error"]["code"] == "token_invalid"
    assert scan(env, claim["id"], second["qr_payload"], "b", version=version).status_code == 404
    garbage = scan(env, claim["id"], "LL1.not-a-valid-token-value", version=version)
    assert garbage.status_code == 409

    admin_sql(
        env,
        "UPDATE claim.handover_tokens SET expires_at = now() - interval '1 second' WHERE id = :id",
        id=second["token_id"],
    )
    expired = scan(env, claim["id"], second["qr_payload"], version=version)
    assert expired.status_code == 409 and expired.json()["error"]["code"] == "token_expired"

    fresh = issue_token(env, claim["id"], key="token-key-3")
    admin_sql(
        env,
        "UPDATE claim.handover_tokens SET replay_expires_at = now() - interval '1 second' "
        "WHERE id = :id",
        id=fresh["token_id"],
    )
    window = env.client.post(
        f"/v1/claims/{claim['id']}/handover-token", headers=headers(key="token-key-3")
    )
    assert window.status_code == 409
    assert window.json()["error"]["code"] == "token_replay_expired"

    def scan_call(key):
        return scan(env, claim["id"], fresh["qr_payload"], version=version, key=key).status_code

    with ThreadPoolExecutor(2) as pool:
        codes = sorted(pool.map(scan_call, ["scan-key-one", "scan-key-two"]))
    # The loser sees either the consumed token (409) or the advanced claim version (412).
    assert codes[0] == 202 and codes[1] in {409, 412}
    drain(env)
    assert owner_claim(env, claim["id"])["state"] == "handed_over"
    with env.db["claim_worker"].scoped_session(tenant_id=env.tenants["a"]) as session:
        states = sorted(token.state for token in session.scalars(select(HandoverToken)))
    assert states.count("used") == 1
    assert "active" not in states and "processing" not in states


def test_access_boundaries_privacy_and_self_review(claim_env):
    env = claim_env
    item_id = make_item(env)
    claim = submit(env, item_id)
    path = f"/v1/claims/{claim['id']}"
    assert env.client.get(path, headers=headers("owner2")).status_code == 404
    assert env.client.get("/v1/claims", headers=headers("owner2")).json()["items"] == []
    assert env.client.get("/v1/venue/claims", headers=headers("b")).json()["items"] == []
    for actor in ["claim", "ops"]:
        assert env.client.get("/v1/claims", headers=headers(actor)).status_code == 403
    assert env.client.get("/v1/venue/claims", headers=headers("owner")).status_code == 403

    own_elsewhere = submit(env, make_item(env, "b", title="Red umbrella"), "a")
    venue_b = env.client.get("/v1/venue/claims", headers=headers("b")).json()["items"]
    assert [row["id"] for row in venue_b] == [own_elsewhere["id"]]
    venue_a = env.client.get("/v1/venue/claims", headers=headers("a")).json()["items"]
    assert own_elsewhere["id"] not in [row["id"] for row in venue_a]
    accepted = approve(env, own_elsewhere, "b", run=False)
    operation_path = f"/v1/claim-operations/{accepted['operation_id']}"
    assert env.client.get(operation_path, headers=headers("a")).status_code == 200
    assert env.client.get(operation_path, headers=headers("owner2")).status_code == 404
    same_venue = submit(env, make_item(env, title="Blue scarf"), "a")
    forbidden = env.client.post(
        f"/v1/venue/claims/{same_venue['id']}/approve", headers=headers("a", version=1)
    )
    assert forbidden.status_code == 403
    assert forbidden.json()["error"]["code"] == "self_review_forbidden"

    reject = env.client.post(
        f"/v1/venue/claims/{claim['id']}/reject",
        headers=headers("a", version=1),
        json={"reason_code": "answers_incorrect"},
    )
    assert reject.status_code == 200 and reject.json()["state"] == "rejected"
    contact_path = f"/v1/venue/claims/{claim['id']}/contact"
    assert env.client.get(contact_path, headers=headers("a")).status_code == 403
    owner_contact = env.client.get(f"{path}/contact", headers=headers())
    assert owner_contact.json()["email"] == "owner1@example.com"

    api, worker = env.db["claim_api"], env.db["claim_worker"]
    with api.scoped_session(subject="staff-a", tenant_id=env.tenants["a"]) as session:
        assert session.scalar(text("SELECT count(*) FROM claim.claim_contacts")) == 0
        assert session.scalar(text("SELECT count(*) FROM claim.claims")) >= 2
    denied = [
        (api.scoped_session(subject="owner-1"), "UPDATE claim.claims SET owner_sub = 'x'"),
        (api.scoped_session(subject="owner-1"), "UPDATE claim.claims SET tenant_id = tenant_id"),
        (worker.scoped_session(tenant_id=env.tenants["a"]), "SELECT * FROM claim.claim_contacts"),
        (api.system_session(), "SELECT envelope FROM claim.outbox_events"),
        (api.system_session(), "SELECT * FROM item.items"),
        (worker.system_session(), "DELETE FROM claim.audit_entries"),
    ]
    for opener, statement in denied:
        with pytest.raises(DBAPIError, match="permission denied"):
            with opener as session:
                session.execute(text(statement))


def test_claim_creation_preconditions(claim_env):
    env = claim_env
    item_id = make_item(env)
    body = claim_body(env, item_id)
    unverified = env.client.post("/v1/claims", headers=headers("unverified"), json=body)
    assert unverified.status_code == 409
    assert unverified.json()["error"]["code"] == "email_not_verified"
    partial = {**body, "answers": body["answers"][:1]}
    response = env.client.post("/v1/claims", headers=headers(), json=partial)
    assert response.status_code == 422 and response.json()["error"]["code"] == "answers_incomplete"
    stale = {**body, "answers": [{**a, "version": a["version"] + 1} for a in body["answers"]]}
    response = env.client.post("/v1/claims", headers=headers(), json=stale)
    assert response.status_code == 409 and response.json()["error"]["code"] == "questions_changed"
    unknown = env.client.post(
        "/v1/claims", headers=headers(), json={**body, "item_id": str(uuid4())}
    )
    assert unknown.status_code == 404
    approve(env, submit(env, item_id))
    reserved = env.client.post("/v1/claims", headers=headers("owner2"), json=body)
    assert reserved.status_code == 409 and reserved.json()["error"]["code"] == "item_not_available"
    env.directory.available = False
    try:
        other = claim_body(env, make_item(env, title="Green hat"))
        assert (
            env.client.post("/v1/claims", headers=headers("owner2"), json=other).status_code == 503
        )
    finally:
        env.directory.available = True


def test_outbox_publishes_with_stable_event_ids(claim_env):
    env = claim_env
    claim = submit(env, make_item(env))
    approve(env, claim)

    class Publisher:
        def __init__(self):
            self.events, self.fail = [], True

        def publish(self, event):
            if self.fail:
                self.fail = False
                raise RuntimeError("transport down")
            self.events.append(event)

    env.worker.publisher = publisher = Publisher()
    assert env.worker.once("outbox")
    admin_sql(env, "UPDATE claim.outbox_events SET available_at = now()")
    assert env.worker.once("outbox")
    assert env.worker.once("outbox") is False
    assert [event.event_type for event in publisher.events] == ["claim.approved"]
    assert publisher.events[0].event_id == claim_events(env, claim["id"])[0].event_id


def test_migration_downgrade_and_upgrade(claim_env):
    env = claim_env
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("MIGRATION_DATABASE_URL", env.migration_url)
        for key, role in env.roles.items():
            patch.setenv(key.upper() + "_ROLE", role)
        command.downgrade(env.configs["claim"], "base")
        command.upgrade(env.configs["claim"], "head")
    env.db["claim_api"].check_ready("claim", "0002_claim_report")
    assert submit(env, make_item(env))["state"] == "submitted"


def test_report_provenance_is_atomic_replayable_and_owner_filtered(claim_env):
    env = claim_env
    item_id = make_item(env)
    report_id = uuid4()
    body = {**claim_body(env, item_id), "lost_report_id": str(report_id)}
    auth = headers(key="report-claim-create-1")
    created = env.client.post("/v1/claims", headers=auth, json=body)
    assert created.status_code == 201, created.text
    claim = created.json()
    assert claim["lost_report_id"] == str(report_id)
    replay = env.client.post("/v1/claims", headers=auth, json=body)
    assert replay.json() == claim
    filtered = f"/v1/claims?lost_report_id={report_id}&limit=1&offset=0"
    assert env.client.get(filtered, headers=headers()).json()["items"][0]["id"] == claim["id"]
    assert env.client.get(filtered, headers=headers("owner2")).json()["items"] == []
    assert (
        env.client.get(f"/v1/claims?lost_report_id={uuid4()}", headers=headers()).json()["items"]
        == []
    )
    conflict = env.client.post(
        "/v1/claims", headers=auth, json={**body, "lost_report_id": str(uuid4())}
    )
    assert conflict.status_code == 409
    cancelled = env.client.post(
        f"/v1/claims/{claim['id']}/cancel",
        headers=headers(version=claim["version"]),
        json={"reason_code": "no_longer_needed"},
    )
    assert cancelled.status_code == 200
    row = env.client.get(filtered, headers=headers()).json()["items"][0]
    assert row["state"] == "cancelled" and row["lost_report_id"] == str(report_id)


def test_report_source_migration_keeps_existing_claims(claim_env):
    env = claim_env
    claim = submit(env, make_item(env))
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("MIGRATION_DATABASE_URL", env.migration_url)
        for key, role in env.roles.items():
            patch.setenv(key.upper() + "_ROLE", role)
        command.downgrade(env.configs["claim"], "0001_claim")
        command.upgrade(env.configs["claim"], "head")
    saved = owner_claim(env, claim["id"])
    assert saved["state"] == "submitted" and saved["lost_report_id"] is None
