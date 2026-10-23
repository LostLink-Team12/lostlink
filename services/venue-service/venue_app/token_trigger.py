"""Cognito V2 pre-token trigger backed by a read-only, subject-scoped DB role."""

import logging
from functools import lru_cache

from venue_app.config import VenueSettings
from venue_app.database import VenueDatabase
from venue_app.service import resolve_identity

MANAGED_ROLES = {"venue_admin", "venue_staff"}
logger = logging.getLogger("lostlink.venue.trigger")


def customize_token(event, resolver, *, user_pool_id, user_client_ids):
    # V3 can be invoked for machine credentials. Do not resolve machine subjects as users.
    if event.get("triggerSource") == "TokenGeneration_ClientCredentials":
        return event
    if (
        event.get("version") != "2"
        or event.get("userPoolId") != user_pool_id
        or event.get("callerContext", {}).get("clientId") not in user_client_ids
    ):
        raise ValueError("Unsupported token trigger invocation")
    request = event["request"]
    subject = request["userAttributes"]["sub"]
    if not isinstance(subject, str) or not 1 <= len(subject) <= 128:
        raise ValueError("Invalid user subject")
    identity = resolver(subject)  # Fail closed on directory/DB failures.
    group_config = request.get("groupConfiguration", {})
    groups = set(group_config.get("groupsToOverride", [])) - MANAGED_ROLES
    groups.add("owner")
    additions, suppressed = {}, ["custom:tenant_id"]
    if identity["tenant_id"] is not None:
        if identity["role"] not in MANAGED_ROLES:
            raise ValueError("Invalid directory role")
        groups.add(identity["role"])
        additions["custom:tenant_id"] = identity["tenant_id"]
        suppressed = []
    event["response"] = {
        "claimsAndScopeOverrideDetails": {
            "accessTokenGeneration": {
                "claimsToAddOrOverride": additions,
                "claimsToSuppress": suppressed,
            },
            "groupOverrideDetails": {**group_config, "groupsToOverride": sorted(groups)},
        }
    }
    return event


@lru_cache(maxsize=1)
def dependencies():
    # Lambda configuration comes from its environment, never a bundled/local .env file.
    settings = VenueSettings(process_mode="trigger", _env_file=None)
    if not settings.identity_database_url or not settings.cognito_user_pool_id:
        raise RuntimeError("Token trigger requires identity database and user pool configuration")
    database = VenueDatabase(
        settings.model_copy(update={"database_url": settings.identity_database_url})
    )
    return settings, database


def handler(event, _context):
    if event.get("triggerSource") == "TokenGeneration_ClientCredentials":
        return event
    try:
        settings, database = dependencies()

        def resolver(subject):
            with database.context(subject=subject) as session:
                return resolve_identity(session, subject)

        return customize_token(
            event,
            resolver,
            user_pool_id=settings.cognito_user_pool_id,
            user_client_ids=settings.user_client_ids + settings.platform_client_ids,
        )
    except Exception as exc:
        # Lambda logs uncaught exceptions. Do not expose SQL parameters or configuration values.
        logger.error("token_customization_failed", extra={"error_type": type(exc).__name__})
        raise RuntimeError("Token customization unavailable") from None
