"""Contact encryption, response separation and state gates without cloud dependencies."""

import json
import os
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from claim_app import routes
from claim_app.errors import ClaimError
from claim_app.service import ClaimService, contact_context
from lostlink_common.crypto import FieldCipher


def test_encrypted_venue_contact_preserves_owner_contact_and_separates_views():
    claim = SimpleNamespace(id=uuid4(), tenant_id=uuid4())
    cipher = FieldCipher({"k1": os.urandom(32)}, "k1")
    owner = {"name": "Owner", "phone": None, "email": "owner@example.com"}
    sealed = cipher.encrypt(json.dumps(owner).encode(), contact_context(claim.id))
    row = SimpleNamespace(key_id=sealed.key_id, encrypted_contact=sealed.ciphertext)
    session = MagicMock()
    session.get.return_value = row
    service = ClaimService(session, "staff", cipher=cipher)
    with pytest.raises(ClaimError):
        service.contact(claim, "read", venue=True)
    venue = {"name": "Counter", "phone": "+65 6123 4567", "email": "staff@example.com"}
    service.save_venue_contact(claim, venue)
    assert b"staff@example.com" not in row.encrypted_contact
    assert service.contact(claim, "read") == owner
    assert service.contact(claim, "read", venue=True) == venue
    assert session.add.call_count == 3  # Sharing and the two successful reads are audited.


@pytest.mark.parametrize("venue", [False, True])
@pytest.mark.parametrize(
    "state",
    [
        "submitted",
        "approval_pending",
        "approved",
        "handover_pending",
        "handed_over",
        "cancelled",
        "rejected",
        "cancellation_pending",
    ],
)
def test_counterpart_contact_state_gates(monkeypatch, venue, state):
    svc = MagicMock()
    svc.get.return_value = SimpleNamespace(state=state)
    svc.contact.return_value = {"name": None, "phone": None, "email": "test@example.com"}
    monkeypatch.setattr(routes, "service", lambda *args: svc)
    monkeypatch.setattr(routes, "owner_session", lambda *args: nullcontext(None))
    monkeypatch.setattr(routes, "staff_session", lambda *args: nullcontext(None))
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(cipher=True)))
    principal = SimpleNamespace(subject="owner", tenant_id=uuid4())
    claim_id = uuid4()
    allowed = state in (
        {"submitted", "approval_pending", "approved", "handover_pending"}
        if venue
        else {"approved", "handover_pending", "handed_over"}
    )
    endpoint = routes.venue_contact if venue else routes.owner_venue_contact
    if not allowed:
        with pytest.raises(ClaimError):
            endpoint(claim_id, request, principal)
        svc.contact.assert_not_called()
    else:
        response = endpoint(claim_id, request, principal)
        assert response.headers["cache-control"] == "no-store"
        svc.get.assert_called_once_with(
            claim_id,
            **({"tenant_id": principal.tenant_id} if venue else {"owner_sub": principal.subject}),
        )


@pytest.mark.parametrize("body", [None, {}, {"contact": {"name": "Counter"}}])
def test_approval_body_stays_optional_and_forwards_only_supplied_contact(monkeypatch, body):
    from claim_app.config import ClaimSettings
    from claim_app.main import create_app
    from fastapi.testclient import TestClient
    from lostlink_common.auth import Principal

    svc = MagicMock()
    svc.approve.return_value = ({"operation_id": str(uuid4())}, 202, '"2"')
    monkeypatch.setattr(routes, "staff_session", lambda *args: nullcontext(None))
    monkeypatch.setattr(routes, "stored", lambda *args: None)
    monkeypatch.setattr(routes, "service", lambda *args: svc)
    monkeypatch.setattr(routes, "idempotent", lambda *args: args[-1]())
    verified = {"name": "Counter", "phone": None, "email": "staff@example.com"}
    resolver = MagicMock(return_value=verified)
    monkeypatch.setattr(routes, "verified_venue_contact", resolver)
    app = create_app(ClaimSettings(_env_file=None))
    principal = Principal(
        subject="staff", client_id="web", tenant_id=uuid4(), roles={"venue_staff"}
    )
    app.dependency_overrides[routes.Staff.__metadata__[0].dependency] = lambda: principal
    claim_id = uuid4()
    with TestClient(app) as client:
        response = client.post(
            f"/v1/venue/claims/{claim_id}/approve",
            json=body,
            headers={"Idempotency-Key": "test-approval", "If-Match": '"1"'},
        )
    assert response.status_code == 202, response.text
    svc.approve.assert_called_once_with(principal, claim_id, '"1"', verified if body else None)
    assert resolver.call_count == (1 if body else 0)
