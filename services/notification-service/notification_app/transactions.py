"""Atomic operator request replay keyed by caller, operation and Idempotency-Key."""

import hashlib
import json
from datetime import UTC, datetime, timedelta

from sqlalchemy import text

from notification_app.errors import fail
from notification_app.models import IdempotencyRecord


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def idempotent(session, principal, operation, key, body, callback):
    if key is None:
        fail(428, "precondition_required", "Idempotency-Key is required")
    if not 8 <= len(key) <= 128 or any(not 32 <= ord(c) <= 126 for c in key):
        fail(422, "invalid_idempotency_key", "Use 8 to 128 printable ASCII characters")
    scope = digest([principal.client_id, principal.subject, operation, key])
    request_hash = digest(body)
    lock_id = int.from_bytes(bytes.fromhex(scope[:16]), signed=True)
    if not session.scalar(text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": lock_id}):
        fail(409, "operation_in_progress", "An identical operation is in progress")
    record = session.get(IdempotencyRecord, scope)
    now = datetime.now(UTC)
    if record and record.expires_at > now:
        if record.request_hash != request_hash:
            fail(409, "idempotency_conflict", "Key was used for a different request")
        return record.response, record.response_status
    if record:
        session.delete(record)
        session.flush()
    response, status = callback()
    session.add(
        IdempotencyRecord(
            scope=scope,
            actor_sub=principal.subject,
            request_hash=request_hash,
            response=response,
            response_status=status,
            expires_at=now + timedelta(hours=24),
        )
    )
    session.flush()
    return response, status
