"""Durable leased jobs; no database transaction spans an external request."""

import argparse
import logging
import random
import signal
import threading
import time
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import boto3
from botocore.config import Config
from lostlink_common.events import DomainEvent, SnsPublisher, aws_client
from lostlink_common.observability import configure_logging
from lostlink_common.worker_health import heartbeat
from sqlalchemy import and_, or_, select

from venue_app.config import VenueSettings
from venue_app.database import VenueDatabase
from venue_app.models import (
    DATABASE_REVISION,
    DATABASE_SCHEMA,
    AuditEntry,
    IdentityJob,
    Membership,
    OutboxEvent,
    Venue,
)

logger = logging.getLogger("lostlink.venue.worker")


class IdentityAccountMissing(Exception):
    """The subject does not identify one existing Cognito account."""


class CognitoSessionRevoker:
    """Never rewrite groups/tenant attributes: the token trigger owns effective claims."""

    def __init__(self, client, pool_id):
        self.client, self.pool_id = client, pool_id

    def revoke(self, subject):
        escaped = subject.replace("\\", "\\\\").replace('"', '\\"')
        users = []
        token = None
        deadline = time.monotonic() + 40
        for _ in range(3):
            if time.monotonic() >= deadline:
                raise TimeoutError("Identity lookup budget exhausted")
            params = {"UserPoolId": self.pool_id, "Filter": f'sub = "{escaped}"', "Limit": 2}
            if token:
                params["PaginationToken"] = token
            page = self.client.list_users(**params)
            users.extend(
                user
                for user in page.get("Users", [])
                if any(
                    attr["Name"] == "sub" and attr["Value"] == subject
                    for attr in user.get("Attributes", [])
                )
            )
            token = page.get("PaginationToken")
            if not token or len(users) > 1:
                break
        else:
            raise TimeoutError("Identity lookup pagination limit exceeded")
        if len(users) != 1:
            raise IdentityAccountMissing()
        self.client.admin_user_global_sign_out(
            UserPoolId=self.pool_id, Username=users[0]["Username"]
        )


class JobRunner:
    def __init__(self, database, *, publisher=None, revoker=None):
        self.database, self.publisher, self.revoker = database, publisher, revoker

    def claim(self, model):
        now = datetime.now(UTC)
        with self.database.context() as session:
            row = session.scalar(
                select(model)
                .where(
                    or_(
                        and_(model.state == "pending", model.available_at <= now),
                        and_(model.state == "leased", model.lease_until < now),
                    )
                )
                .order_by(model.available_at, model.id)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if row is None:
                return None
            # A crashing process must not reset the retry budget on every restart.
            if row.attempts >= 10:
                row.state, row.last_error_code = "failed", "retry_exhausted"
                row.lease_token, row.lease_until = None, None
                return None
            row.state, row.lease_token = "leased", uuid4()
            row.lease_until, row.attempts = now + timedelta(seconds=60), row.attempts + 1
            session.flush()
            return row

    def finish(self, model, claimed, state, error=None):
        with self.database.context() as session:
            row = session.scalar(
                select(model)
                .where(
                    model.id == claimed.id,
                    model.state == "leased",
                    model.lease_token == claimed.lease_token,
                )
                .with_for_update()
            )
            if row is None:
                return
            row.state = "failed" if error and row.attempts >= 10 else state
            row.last_error_code = error
            row.available_at = datetime.now(UTC) + timedelta(
                seconds=random.uniform(1, min(300, 2**row.attempts))
            )
            row.lease_token, row.lease_until = None, None

    def once(self, mode):
        model = OutboxEvent if mode == "outbox" else IdentityJob
        claimed = self.claim(model)
        if claimed is None:
            return False
        try:
            if mode == "outbox":
                if self.publisher is None:
                    raise RuntimeError("Publisher is not configured")
                self.publisher.publish(DomainEvent.model_validate(claimed.envelope))
                state = "sent"
            else:
                with self.database.context(tenant_id=claimed.tenant_id) as session:
                    member = session.get(Membership, claimed.membership_id)
                    venue = session.get(Venue, claimed.tenant_id)
                    current = (
                        member is not None
                        and venue is not None
                        and member.version == claimed.membership_version
                        and venue.version == claimed.venue_version
                    )
                if current:
                    if self.revoker is None:
                        raise RuntimeError("Identity revocation is not configured")
                    self.revoker.revoke(claimed.user_sub)
                state = "completed" if current else "superseded"
            self.finish(model, claimed, state)
        except IdentityAccountMissing:
            self.finish(model, claimed, "failed", "identity_account_missing")
        except Exception as exc:
            logger.warning(
                "venue_job_failed", extra={"error_type": type(exc).__name__, "mode": mode}
            )
            self.finish(model, claimed, "pending", "dependency_unavailable")
        return True

    def retry_failed(self, mode, job_id, operator, reason):
        model = OutboxEvent if mode == "outbox" else IdentityJob
        with self.database.context(subject=operator) as session:
            row = session.scalar(select(model).where(model.id == job_id).with_for_update())
            if row is None or row.state != "failed":
                raise ValueError("Only a failed job can be replayed")
            row.state, row.attempts = "pending", 0
            row.available_at = datetime.now(UTC)
            row.last_error_code, row.lease_token, row.lease_until = None, None, None
            session.add(
                AuditEntry(
                    actor_sub=operator,
                    tenant_id=row.tenant_id,
                    action=f"{mode}.retry.{reason}",
                    resource_id=row.id,
                )
            )


def main():
    parser = argparse.ArgumentParser(description="Run the Venue outbox or identity worker")
    parser.add_argument("mode", choices=["outbox", "identity"])
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--retry-id", type=UUID)
    parser.add_argument("--operator")
    parser.add_argument("--reason", choices=["dependency_restored", "manual_review"])
    args = parser.parse_args()
    settings = VenueSettings(process_mode="worker")
    configure_logging(settings.service_name, settings.log_level)
    if settings.worker_database_url is None:
        parser.error("WORKER_DATABASE_URL is required")
    if args.retry_id and (not args.operator or not args.reason or len(args.operator) > 128):
        parser.error("Replay requires an operator (1-128 characters) and a reason")
    if not args.retry_id and args.mode == "outbox" and not settings.event_topic_arn:
        parser.error("EVENT_TOPIC_ARN is required")
    if not args.retry_id and args.mode == "identity" and not settings.cognito_user_pool_id:
        parser.error("COGNITO_USER_POOL_ID is required")
    database = VenueDatabase(
        settings.model_copy(update={"database_url": settings.worker_database_url})
    )
    client = None
    stopped = threading.Event()
    for sig in [signal.SIGINT, signal.SIGTERM]:
        signal.signal(sig, lambda *_: stopped.set())
    try:
        database.check_ready(DATABASE_SCHEMA, DATABASE_REVISION)
        if args.retry_id:
            JobRunner(database).retry_failed(args.mode, args.retry_id, args.operator, args.reason)
            return
        if args.mode == "outbox":
            client = aws_client("sns", settings)
            runner = JobRunner(database, publisher=SnsPublisher(client, settings.event_topic_arn))
        else:
            client = boto3.client(
                "cognito-idp",
                region_name=settings.aws_region,
                config=Config(connect_timeout=2, read_timeout=3, retries={"total_max_attempts": 2}),
            )
            runner = JobRunner(
                database, revoker=CognitoSessionRevoker(client, settings.cognito_user_pool_id)
            )
        while not stopped.is_set():
            worked = runner.once(args.mode)
            heartbeat()
            if args.once:
                break
            if not worked:
                stopped.wait(1)
    finally:
        if client:
            client.close()
        database.close()


if __name__ == "__main__":
    main()
