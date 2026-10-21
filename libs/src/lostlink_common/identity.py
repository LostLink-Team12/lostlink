"""Resolve a subject's verified email from the trusted identity provider.

Only Claim and Notification use this, server side, for a subject taken from a verified
token or a validated event. Never expose it as a "look up any user" endpoint and never
fall back to an address supplied by a request or an event.
"""

import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Protocol

from botocore.exceptions import BotoCoreError, ClientError

from lostlink_common.auth import validate_subject

MAX_EMAIL_LENGTH = 254


class IdentityLookupError(Exception):
    code = "identity_lookup_failed"


class IdentityNotFound(IdentityLookupError):
    """No single enabled account has exactly this subject."""

    code = "identity_not_found"


class EmailNotVerified(IdentityLookupError):
    code = "email_not_verified"


class IdentityDirectoryUnavailable(IdentityLookupError):
    """Retryable: the provider failed, timed out or throttled the lookup."""

    code = "identity_directory_unavailable"


@dataclass(frozen=True)
class VerifiedEmail:
    subject: str
    email: str = field(repr=False)


class IdentityDirectory(Protocol):
    def resolve_verified_email(self, subject: str) -> VerifiedEmail: ...


def _valid_email(value: object) -> bool:
    return (
        isinstance(value, str)
        and 3 <= len(value) <= MAX_EMAIL_LENGTH
        and value.count("@") == 1
        and not value.startswith("@")
        and not value.endswith("@")
        and not any(character.isspace() or ord(character) < 32 for character in value)
    )


class CognitoIdentityDirectory:
    """Exact-subject lookup in one fixed user pool (IAM: cognito-idp:ListUsers only)."""

    def __init__(self, client, user_pool_id: str):
        if not user_pool_id or "_" not in user_pool_id:
            raise ValueError("A Cognito user pool ID is required")
        self.client, self.pool_id = client, user_pool_id

    def resolve_verified_email(self, subject: str) -> VerifiedEmail:
        validate_subject(subject)
        escaped = subject.replace("\\", "\\\\").replace('"', '\\"')
        users = []
        token = None
        deadline = time.monotonic() + 10
        try:
            for _ in range(3):
                if time.monotonic() >= deadline:
                    raise IdentityDirectoryUnavailable("Identity lookup budget exhausted")
                params = {
                    "UserPoolId": self.pool_id,
                    "Filter": f'sub = "{escaped}"',
                    "Limit": 2,
                    "AttributesToGet": ["sub", "email", "email_verified"],
                }
                if token:
                    params["PaginationToken"] = token
                page = self.client.list_users(**params)
                for user in page.get("Users", []):
                    attributes = {
                        attribute.get("Name"): attribute.get("Value")
                        for attribute in user.get("Attributes", [])
                    }
                    if attributes.get("sub") == subject:
                        users.append((user, attributes))
                token = page.get("PaginationToken")
                if not token or len(users) > 1:
                    break
            else:
                raise IdentityDirectoryUnavailable("Identity lookup pagination limit exceeded")
        except (ClientError, BotoCoreError) as exc:
            raise IdentityDirectoryUnavailable("Identity provider is unavailable") from exc
        if len(users) != 1 or users[0][0].get("Enabled") is False:
            raise IdentityNotFound("No single enabled account matches the subject")
        attributes = users[0][1]
        email = attributes.get("email")
        if attributes.get("email_verified") != "true" or not _valid_email(email):
            raise EmailNotVerified("The account has no verified email")
        return VerifiedEmail(subject, email)


class StaticIdentityDirectory:
    """Test and local double. Do not wire it into dev or prod deployments."""

    def __init__(self, users: Mapping[str, tuple[str, bool]], *, available: bool = True):
        self.users, self.available = dict(users), available

    def resolve_verified_email(self, subject: str) -> VerifiedEmail:
        validate_subject(subject)
        if not self.available:
            raise IdentityDirectoryUnavailable("Identity provider is unavailable")
        if subject not in self.users:
            raise IdentityNotFound("No single enabled account matches the subject")
        email, verified = self.users[subject]
        if not verified or not _valid_email(email):
            raise EmailNotVerified("The account has no verified email")
        return VerifiedEmail(subject, email)
