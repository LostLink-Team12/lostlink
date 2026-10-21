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
    assert hidden.status_code == 403
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
