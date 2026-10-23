"""Claim state transitions in short transactions; Item calls happen in the coordinator."""

import base64
import hashlib
import hmac
import json
import os
import re
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from lostlink_common.crypto import DecryptionFailed, EncryptedValue, EncryptionContext
from lostlink_common.events import DomainEvent
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from claim_app.errors import fail
from claim_app.models import (
    AuditEntry,
    Claim,
    ClaimAnswer,
    ClaimContact,
    ClaimOperation,
    HandoverRecord,
    HandoverToken,
    OutboxEvent,
)
from claim_app.transactions import expected_version

SERVICE = "claim-service"
TOKEN_PATTERN = re.compile(
    r"LL1\.([0-9a-f-]{36})\.([0-9a-f-]{36})\.([A-Za-z0-9_-]{43})", re.IGNORECASE
)
REPLAY_WINDOW = timedelta(minutes=10)


def iso(value):
    return value.isoformat() if value else None


def contact_context(claim_id):
    return EncryptionContext(SERVICE, "claim_contacts", "encrypted_contact", claim_id)


def replay_context(token_id):
    return EncryptionContext(SERVICE, "handover_tokens", "replay_ciphertext", token_id)


def token_hash(key: bytes, token_id: UUID, secret: str) -> str:
    return hmac.new(key, f"{token_id}.{secret}".encode(), hashlib.sha256).hexdigest()


def parse_token(value: str):
    match = TOKEN_PATTERN.fullmatch(value)
    if not match:
        fail(409, "token_invalid", "Handover code is not valid")
    try:
        return UUID(match.group(1)), UUID(match.group(2)), match.group(3)
    except ValueError:
        fail(409, "token_invalid", "Handover code is not valid")


def claim_view(claim, active_operation_id=None, *, venue=False):
    value = {
        "id": str(claim.id),
        "item_id": str(claim.item_id),
        "venue_id": str(claim.tenant_id),
        "item_title": claim.item_title,
        "item_version": claim.item_version,
        "state": claim.state,
        "version": claim.version,
        "decision_code": claim.decision_code,
        "created_at": iso(claim.created_at),
        "approved_at": iso(claim.approved_at),
        "closed_at": iso(claim.closed_at),
        "active_operation_id": str(active_operation_id) if active_operation_id else None,
    }
    if venue:
        value.update(owner_sub=claim.owner_sub, reviewed_by=claim.reviewed_by)
    return value


def etag(claim):
    return f'"{claim.version}"'


class ClaimService:
    def __init__(self, session, subject, request_id=None, *, settings=None, cipher=None):
        self.session, self.subject, self.request_id = session, subject, request_id
        self.settings, self.cipher = settings, cipher

    def now(self):
        return datetime.now(UTC)

    def get(self, claim_id, *, lock=False, tenant_id=None, owner_sub=None):
        # RLS ORs owner and venue visibility; each caller also pins its own mode explicitly.
        query = select(Claim).where(Claim.id == claim_id)
        if tenant_id is not None:
            query = query.where(Claim.tenant_id == tenant_id)
        if owner_sub is not None:
            query = query.where(Claim.owner_sub == owner_sub)
        if lock:
            query = query.with_for_update()
        claim = self.session.scalar(query.execution_options(populate_existing=True))
        if claim is None:
            fail(404, "claim_not_found", "Claim is not visible")
        return claim

    def active_operation(self, claim_id, *, lock=False):
        query = select(ClaimOperation).where(
            ClaimOperation.claim_id == claim_id, ClaimOperation.state.in_(["pending", "leased"])
        )
        if lock:
            query = query.with_for_update()
        return self.session.scalar(query)

    def answers(self, claim_id):
        rows = self.session.scalars(
            select(ClaimAnswer)
            .where(ClaimAnswer.claim_id == claim_id)
            .order_by(ClaimAnswer.position)
        )
        return [
            {
                "question_id": str(row.question_id),
                "question_version": row.question_version,
                "prompt": row.prompt_snapshot,
                "answer": row.answer_text,
            }
            for row in rows
        ]

    def detail(self, claim, *, venue=False):
        operation = self.active_operation(claim.id)
        return {
            **claim_view(claim, operation.id if operation else None, venue=venue),
            "answers": self.answers(claim.id),
        }

    def audit(self, claim, action):
        self.session.add(
            AuditEntry(
                actor_sub=self.subject,
                tenant_id=claim.tenant_id,
                action=action,
                resource_id=claim.id,
                request_id=self.request_id,
            )
        )

    def event(self, claim, event_type, data):
        event = DomainEvent(
            event_type=event_type,
            source=SERVICE,
            tenant_id=claim.tenant_id,
            data={"claim_id": str(claim.id), "item_id": str(claim.item_id), **data},
        )
        self.session.add(
            OutboxEvent(
                id=event.event_id,
                tenant_id=claim.tenant_id,
                envelope=event.model_dump(mode="json"),
            )
        )

    def operation(self, claim, kind):
        operation = ClaimOperation(
            tenant_id=claim.tenant_id,
            owner_sub=claim.owner_sub,
            claim_id=claim.id,
            kind=kind,
            requested_by=self.subject,
            deadline_at=self.now()
            + timedelta(minutes=self.settings.operation_deadline_minutes if self.settings else 15),
        )
        self.session.add(operation)
        return operation

    def accepted(self, claim, operation):
        return (
            {
                "operation_id": str(operation.id),
                "claim_id": str(claim.id),
                "state": claim.state,
                "version": claim.version,
            },
            202,
            etag(claim),
        )

    def flush_unique(self, code, message):
        try:
            self.session.flush()
        except IntegrityError:
            fail(409, code, message)

    def create(self, owner_sub, body, context, email):
        """context is the Item claim-context fetched immediately before this transaction."""
        if context.state != "available":
            fail(409, "item_not_available", "This item cannot be claimed now")
        questions = {question.id: question for question in context.questions}
        supplied = {answer.question_id: answer for answer in body.answers}
        if set(supplied) != set(questions):
            fail(422, "answers_incomplete", "Answer every verification question exactly once")
        if any(supplied[qid].version != questions[qid].version for qid in questions):
            fail(409, "questions_changed", "Verification questions changed; reload them")
        claim = Claim(
            id=uuid4(),
            tenant_id=context.tenant_id,
            item_id=context.item_id,
            item_version=context.item_version,
            item_title=context.title,
            owner_sub=owner_sub,
            state="submitted",
            version=1,
        )
        self.session.add(claim)
        self.flush_unique("claim_exists", "You already have an open claim for this item")
        for position, question in enumerate(context.questions):
            self.session.add(
                ClaimAnswer(
                    tenant_id=claim.tenant_id,
                    owner_sub=owner_sub,
                    claim_id=claim.id,
                    question_id=question.id,
                    question_version=question.version,
                    position=position,
                    prompt_snapshot=question.prompt,
                    answer_text=supplied[question.id].answer,
                )
            )
        contact = body.contact
        plaintext = json.dumps(
            {
                "name": contact.name if contact else None,
                "phone": contact.phone if contact else None,
                "email": email,
            }
        ).encode()
        sealed = self.cipher.encrypt(plaintext, contact_context(claim.id))
        self.session.add(
            ClaimContact(
                claim_id=claim.id,
                tenant_id=claim.tenant_id,
                owner_sub=owner_sub,
                key_id=sealed.key_id,
                encrypted_contact=sealed.ciphertext,
            )
        )
        self.audit(claim, "claim.submitted")
        self.session.flush()
        return self.detail(claim), 201, etag(claim)

    def review_target(self, principal, claim_id, version):
        claim = self.get(claim_id, lock=True, tenant_id=principal.tenant_id)
        if claim.owner_sub == principal.subject:
            fail(403, "self_review_forbidden", "Staff cannot review or hand over their own claim")
        expected_version(version, claim.version)
        return claim

    def approve(self, principal, claim_id, version):
        claim = self.review_target(principal, claim_id, version)
        if claim.state != "submitted":
            fail(409, "claim_not_submitted", "Only submitted claims can be approved")
        claim.state, claim.reviewed_by = "approval_pending", principal.subject
        claim.version += 1
        self.flush_unique("item_claim_in_progress", "Another claim for this item is being handled")
        operation = self.operation(claim, "approve")
        self.audit(claim, "claim.approval_started")
        self.session.flush()
        return self.accepted(claim, operation)

    def reject(self, principal, claim_id, version, reason):
        claim = self.review_target(principal, claim_id, version)
        if claim.state != "submitted":
            fail(409, "claim_not_submitted", "Only submitted claims can be rejected")
        claim.state, claim.reviewed_by, claim.decision_code = "rejected", principal.subject, reason
        claim.closed_at = self.now()
        claim.version += 1
        self.audit(claim, "claim.rejected")
        self.session.flush()
        return self.detail(claim, venue=True), 200, etag(claim)

    def revoke_tokens(self, claim):
        self.session.execute(
            update(HandoverToken)
            .where(HandoverToken.claim_id == claim.id, HandoverToken.state == "active")
            .values(
                state="revoked", replay_key_id=None, replay_ciphertext=None, replay_expires_at=None
            )
        )

    def cancel(self, owner_sub, claim_id, version, reason):
        claim = self.get(claim_id, lock=True, owner_sub=owner_sub)
        expected_version(version, claim.version)
        if claim.state == "submitted":
            claim.state, claim.decision_code, claim.closed_at = "cancelled", reason, self.now()
            claim.version += 1
            self.event(claim, "claim.cancelled", {"owner_id": owner_sub, "reason_code": reason})
            self.audit(claim, "claim.cancelled")
            self.session.flush()
            return self.detail(claim), 200, etag(claim)
        if claim.state in {"handover_pending", "handed_over"}:
            fail(409, "handover_in_progress", "A handover has already been accepted")
        if claim.state not in {"approval_pending", "approved"}:
            fail(409, "claim_closed", "This claim can no longer be cancelled")
        claim.state, claim.decision_code = "cancellation_pending", reason
        claim.version += 1
        self.revoke_tokens(claim)
        # Reuse an in-flight approval: the coordinator follows the claim state and releases.
        operation = self.active_operation(claim.id, lock=True)
        if operation is None:
            operation = self.operation(claim, "cancel")
        else:
            # Match the worker's database clock even when API hosts have clock skew.
            operation.kind, operation.available_at = (
                "cancel",
                self.session.scalar(select(func.now())),
            )
        self.audit(claim, "claim.cancellation_started")
        self.session.flush()
        return self.accepted(claim, operation)

    def issue_token(self, owner_sub, claim_id, token_key):
        claim = self.get(claim_id, lock=True, owner_sub=owner_sub)
        if claim.state != "approved":
            fail(409, "claim_not_approved", "A handover code requires an approved claim")
        self.revoke_tokens(claim)
        self.session.flush()
        token_id = uuid4()
        secret = base64.urlsafe_b64encode(os.urandom(32)).rstrip(b"=").decode()
        value = f"LL1.{claim.id}.{token_id}.{secret}"
        expires_at = self.now() + timedelta(
            hours=self.settings.handover_token_hours if self.settings else 24
        )
        sealed = self.cipher.encrypt(value.encode(), replay_context(token_id))
        self.session.add(
            HandoverToken(
                id=token_id,
                tenant_id=claim.tenant_id,
                owner_sub=owner_sub,
                claim_id=claim.id,
                token_hash=token_hash(token_key, token_id, secret),
                state="active",
                expires_at=expires_at,
                replay_key_id=sealed.key_id,
                replay_ciphertext=sealed.ciphertext,
                replay_expires_at=min(expires_at, self.now() + REPLAY_WINDOW),
            )
        )
        self.audit(claim, "handover_token.issued")
        self.session.flush()
        return (
            {"token_id": str(token_id), "claim_id": str(claim.id), "expires_at": iso(expires_at)},
            201,
            None,
        )

    def token_payload(self, value):
        """Recover the QR value for the original request within the short replay window."""
        token = self.session.get(HandoverToken, UUID(value["token_id"]))
        if (
            token is None
            or token.state != "active"
            or token.replay_ciphertext is None
            or token.replay_expires_at is None
            or token.replay_expires_at <= self.now()
        ):
            fail(409, "token_replay_expired", "Issue a new handover code")
        try:
            payload = self.cipher.decrypt(
                EncryptedValue(token.replay_key_id, token.replay_ciphertext),
                replay_context(token.id),
            ).decode()
        except DecryptionFailed:
            fail(409, "token_replay_expired", "Issue a new handover code")
        return {**value, "qr_payload": payload}

    def scan(self, principal, claim_id, version, raw_token, token_key, mode="counter"):
        token_claim, token_id, secret = parse_token(raw_token)
        if token_claim != claim_id:
            fail(409, "token_invalid", "Handover code is not valid for this claim")
        claim = self.review_target(principal, claim_id, version)
        if claim.state == "handover_pending":
            fail(409, "handover_in_progress", "A handover is already in progress")
        if claim.state != "approved":
            fail(409, "claim_not_approved", "Only approved claims can be handed over")
        token = self.session.scalar(
            select(HandoverToken)
            .where(HandoverToken.id == token_id, HandoverToken.claim_id == claim.id)
            .with_for_update()
        )
        if token is None or not hmac.compare_digest(
            token.token_hash, token_hash(token_key, token_id, secret)
        ):
            fail(409, "token_invalid", "Handover code is not valid")
        if token.state != "active":
            fail(409, "token_used", "Handover code is no longer active")
        if token.expires_at <= self.now():
            fail(409, "token_expired", "Handover code has expired")
        handover_id = uuid4()
        token.state, token.handover_id = "processing", handover_id
        token.replay_key_id = token.replay_ciphertext = token.replay_expires_at = None
        claim.state = "handover_pending"
        claim.version += 1
        self.session.add(
            HandoverRecord(
                handover_id=handover_id,
                claim_id=claim.id,
                tenant_id=claim.tenant_id,
                owner_sub=claim.owner_sub,
                token_id=token.id,
                mode=mode,
                operator_sub=principal.subject,
                state="pending",
            )
        )
        operation = self.operation(claim, "handover")
        self.audit(claim, "handover.accepted")
        self.session.flush()
        return self.accepted(claim, operation)

    def contact(self, claim, action):
        row = self.session.get(ClaimContact, claim.id)
        if row is None:
            fail(404, "contact_not_available", "Contact details are not available")
        try:
            value = json.loads(
                self.cipher.decrypt(
                    EncryptedValue(row.key_id, row.encrypted_contact), contact_context(claim.id)
                )
            )
        except DecryptionFailed:
            fail(503, "contact_unavailable", "Contact details cannot be read")
        self.audit(claim, action)
        return value
