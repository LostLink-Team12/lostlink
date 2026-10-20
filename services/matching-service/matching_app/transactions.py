"""Atomic request replay and version checks; snapshots contain no answers or credentials."""

import hashlib
import json
import re
from datetime import UTC, datetime, timedelta

from sqlalchemy import text

from matching_app.errors import fail
from matching_app.models import IdempotencyRecord


def expected_version(header, actual):
    if header is None:
        fail(428, "precondition_required", "If-Match is required")
    if not re.fullmatch(r'"[1-9][0-9]*"', header):
        fail(422, "invalid_etag", "Use a quoted integer ETag")
    if header != f'"{actual}"':
        fail(412, "version_mismatch", "Resource version has changed")


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def idempotent(session, principal, tenant, operation, key, body, version, callback):
    scope, request_hash = request_identity(principal, tenant, operation, key, body, version)
    lock_id = int.from_bytes(bytes.fromhex(scope[:16]), signed=True)
    if not session.scalar(text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": lock_id}):
        fail(409, "operation_in_progress", "An identical operation is in progress")
    record = session.get(IdempotencyRecord, scope)
    now = datetime.now(UTC)
    if record and record.expires_at > now:
        if record.request_hash != request_hash:
            fail(409, "idempotency_conflict", "Key was used for a different request")
        return record.response, record.response_status, record.etag
    if record:
        session.delete(record)
        session.flush()
    body, status, etag = callback()
    session.add(
        IdempotencyRecord(
            scope=scope,
            actor_sub=principal.subject,
            request_hash=request_hash,
            response=body,
            response_status=status,
            etag=etag,
            expires_at=now + timedelta(hours=24),
        )
    )
    session.flush()
    return body, status, etag


def request_identity(principal, tenant, operation, key, body, version):
    if key is None:
        fail(428, "precondition_required", "Idempotency-Key is required")
    if not 8 <= len(key) <= 128 or any(not 32 <= ord(c) <= 126 for c in key):
        fail(422, "invalid_idempotency_key", "Use 8 to 128 printable ASCII characters")
    scope = digest([principal.client_id, principal.subject, str(tenant), operation, key])
    request_hash = digest({"body": body, "if_match": version})
    return scope, request_hash


def replay(session, principal, tenant, operation, key, body, version):
    scope, request_hash = request_identity(principal, tenant, operation, key, body, version)
    record = session.get(IdempotencyRecord, scope)
    if record and record.expires_at > datetime.now(UTC):
        if record.request_hash != request_hash:
            fail(409, "idempotency_conflict", "Key was used for a different request")
        return record.response, record.response_status, record.etag
    return None
