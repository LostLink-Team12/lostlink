"""Main UC5 flow against real PostgreSQL/RLS, Venue membership and Item reservations."""

import pytest
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

pytestmark = pytest.mark.integration


def test_claim_approval_qr_and_handover_main_flow(claim_env):
    env = claim_env
    item_id = make_item(env)
    visible = questions(env, item_id)
    assert [q["prompt"] for q in visible["questions"]] == [
        "What card is inside?",
        "What colour is the zip?",
    ]
    assert "PRIVATE" not in str(visible)

    claim = submit(env, item_id)
    assert claim["state"] == "submitted" and claim["venue_id"] == str(env.tenants["a"])
    assert [a["answer"] for a in claim["answers"]] == ["answer 0", "answer 1"]
    assert "PRIVATE" not in str(claim) and "example.com" not in str(claim)

    listed = env.client.get("/v1/venue/claims", headers=headers("a")).json()
    assert [row["id"] for row in listed["items"]] == [claim["id"]]
    assert listed["items"][0]["owner_sub"] == "owner-1"
    hidden = env.client.get(f"/v1/venue/claims/{claim['id']}/contact", headers=headers("a"))
    assert hidden.status_code == 200
    assert hidden.json()["phone"] == "+65 8000 0000"
    assert hidden.headers["cache-control"] == "no-store"
    assert (
        env.client.get(f"/v1/venue/claims/{claim['id']}", headers=headers("b")).status_code == 404
    )

    accepted = approve(env, claim, run=False)
    assert accepted["state"] == "approval_pending"
    operation = env.client.get(
        f"/v1/claim-operations/{accepted['operation_id']}", headers=headers("a")
    ).json()
    assert operation["state"] == "pending" and operation["kind"] == "approve"
    drain(env)
    approved = owner_claim(env, claim["id"])
    assert approved["state"] == "approved" and approved["approved_at"]
    state, reservations = item_state(env, item_id)
    assert state == "reserved" and reservations[0][1] == "held"
    assert [e.event_type for e in claim_events(env, claim["id"])] == ["claim.approved"]

    contact = env.client.get(f"/v1/venue/claims/{claim['id']}/contact", headers=headers("a"))
    assert contact.status_code == 200, contact.text
    assert contact.json() == {
        "name": "Alex Owner",
        "phone": "+65 8000 0000",
        "email": "owner1@example.com",
    }

    token = issue_token(env, claim["id"])
    assert token["qr_payload"].startswith(f"LL1.{claim['id']}.{token['token_id']}.")
    response = scan(env, claim["id"], token["qr_payload"], version=approved["version"])
    assert response.status_code == 202, response.text
    assert response.json()["state"] == "handover_pending"
    drain(env)
    done = owner_claim(env, claim["id"])
    assert done["state"] == "handed_over" and done["closed_at"]
    state, reservations = item_state(env, item_id)
    assert state == "handed_over" and reservations[0][1] == "committed"
    events = claim_events(env, claim["id"])
    assert [e.event_type for e in events] == ["claim.approved", "claim.handed_over"]
    assert events[1].data["mode"] == "counter" and "email" not in str(events[1].data)

    again = scan(env, claim["id"], token["qr_payload"], version=done["version"])
    assert again.status_code == 409
    after = env.client.get(f"/v1/venue/claims/{claim['id']}/contact", headers=headers("a"))
    assert after.status_code == 403
    finished = env.client.get(
        f"/v1/claim-operations/{response.json()['operation_id']}", headers=headers("owner")
    ).json()
    assert finished["state"] == "completed" and finished["result_code"] == "handed_over"


def test_courier_handover_is_published_for_seed2_delivery(claim_env):
    env = claim_env
    item_id = make_item(env)
    claim = submit(env, item_id)
    approve(env, claim)
    approved = owner_claim(env, claim["id"])
    assert approved["state"] == "approved"
    token = issue_token(env, claim["id"])
    invalid = scan(env, claim["id"], token["qr_payload"], version=approved["version"], mode="drone")
    assert invalid.status_code == 422
    response = scan(
        env, claim["id"], token["qr_payload"], version=approved["version"], mode="courier"
    )
    assert response.status_code == 202, response.text
    drain(env)
    assert owner_claim(env, claim["id"])["state"] == "handed_over"
    state, _ = item_state(env, item_id)
    assert state == "handed_over"
    events = claim_events(env, claim["id"])
    assert [e.event_type for e in events] == ["claim.approved", "claim.handed_over"]
    assert events[1].data["mode"] == "courier"


def test_two_way_contacts_are_scoped_encrypted_and_approval_gated(claim_env):
    from uuid import UUID

    from claim_app.models import ClaimContact
    from sqlalchemy import text

    env = claim_env
    claim = submit(env, make_item(env))
    path = f"/v1/claims/{claim['id']}/venue-contact"
    staff_path = f"/v1/venue/claims/{claim['id']}"
    assert env.client.get(path, headers=headers()).status_code == 403
    assert env.client.get(path, headers=headers("owner2")).status_code == 404
    assert env.client.get(staff_path + "/contact", headers=headers("b")).status_code == 404
    with env.db["claim_api"].scoped_session(
        subject="staff-b", tenant_id=env.tenants["b"]
    ) as session:
        assert session.scalar(text("SELECT count(*) FROM claim.claim_contacts")) == 0
    contact = {"name": "Campus counter", "phone": "+65 6123 4567"}
    request_headers = headers("a", version=claim["version"])
    response = env.client.post(
        staff_path + "/approve", headers=request_headers, json={"contact": contact}
    )
    assert response.status_code == 202, response.text
    assert (
        env.client.post(
            staff_path + "/approve", headers=request_headers, json={"contact": contact}
        ).json()
        == response.json()
    )
    assert env.client.get(path, headers=headers()).status_code == 403
    drain(env)
    shared = env.client.get(path, headers=headers())
    assert shared.status_code == 200, shared.text
    assert shared.json() == {**contact, "email": "admin-a@example.com"}
    assert shared.headers["cache-control"] == "no-store"
    assert env.client.get(f"/v1/claims/{claim['id']}/contact", headers=headers()).json() == {
        "name": "Alex Owner",
        "phone": "+65 8000 0000",
        "email": "owner1@example.com",
    }
    approved = owner_claim(env, claim["id"])
    update_headers = headers("a", version=approved["version"])
    update = env.client.put(
        staff_path + "/reply-contact",
        headers=update_headers,
        json={"name": "Service desk", "phone": "+65 6333 4444"},
    )
    assert update.status_code == 200, update.text
    assert env.client.get(path, headers=headers()).json()["name"] == "Service desk"
    assert (
        env.client.put(
            staff_path + "/reply-contact",
            headers=update_headers,
            json={"name": "Service desk", "phone": "+65 6333 4444"},
        ).json()
        == update.json()
    )
    assert (
        env.client.put(
            staff_path + "/reply-contact", headers=headers("a", version=1), json=contact
        ).status_code
        == 412
    )
    assert (
        env.client.put(
            staff_path + "/reply-contact", headers=headers("b", version=1), json=contact
        ).status_code
        == 404
    )
    with env.db["claim_api"].scoped_session(subject="owner-1") as session:
        row = session.get(ClaimContact, UUID(claim["id"]))
        assert b"Campus counter" not in row.encrypted_contact
        assert b"Service desk" not in row.encrypted_contact
        assert b"example.com" not in row.encrypted_contact


def test_legacy_approval_can_publish_contact_later(claim_env):
    env = claim_env
    claim = submit(env, make_item(env))
    approve(env, claim)
    path = f"/v1/claims/{claim['id']}/venue-contact"
    assert env.client.get(path, headers=headers()).status_code == 404
    approved = owner_claim(env, claim["id"])
    response = env.client.put(
        f"/v1/venue/claims/{claim['id']}/reply-contact",
        headers=headers("a", version=approved["version"]),
        json={"name": "Campus counter"},
    )
    assert response.status_code == 200, response.text
    assert env.client.get(path, headers=headers()).json() == {
        "name": "Campus counter",
        "phone": None,
        "email": "admin-a@example.com",
    }
