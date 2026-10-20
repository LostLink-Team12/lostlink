"""Cognito access-token verification and FastAPI authorization dependencies."""

from typing import Annotated, Protocol
from uuid import UUID

import jwt
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt import PyJWKClient
from jwt.exceptions import PyJWKClientConnectionError
from pydantic import BaseModel, ConfigDict, Field

from lostlink_common.config import Settings

SUBJECT_MAX_LENGTH = 128


def validate_subject(subject: str, *, allow_empty: bool = False) -> str:
    """Reject values that cannot be a Cognito subject before they reach SQL or filters."""
    if not isinstance(subject, str) or len(subject) > SUBJECT_MAX_LENGTH:
        raise ValueError("Subject must be a string of at most 128 characters")
    if not subject and not allow_empty:
        raise ValueError("Subject must not be empty")
    if any(ord(character) < 32 or ord(character) == 127 for character in subject):
        raise ValueError("Subject must not contain control characters")
    return subject


class InvalidToken(Exception):
    """The caller did not present a valid access token."""


class IdentityUnavailable(Exception):
    """The configured identity provider is temporarily unavailable."""


class Principal(BaseModel):
    model_config = ConfigDict(frozen=True)

    subject: str = Field(min_length=1)
    client_id: str = Field(min_length=1)
    tenant_id: UUID | None = None
    roles: frozenset[str] = frozenset()
    scopes: frozenset[str] = frozenset()


class TokenVerifier(Protocol):
    def verify(self, token: str) -> Principal: ...


class CognitoVerifier:
    def __init__(self, settings: Settings, jwks_client: PyJWKClient | None = None):
        if not settings.cognito_issuer or not settings.cognito_client_ids:
            raise ValueError("Cognito configuration is required")
        self.settings = settings
        self.jwks_client = jwks_client or PyJWKClient(
            f"{settings.cognito_issuer}/.well-known/jwks.json",
            cache_keys=False,
            cache_jwk_set=True,
            lifespan=300,
            timeout=3,
        )

    def verify(self, token: str) -> Principal:
        try:
            if len(token) > 16384:
                raise InvalidToken()
            header = jwt.get_unverified_header(token)
            if header.get("alg") != "RS256" or not isinstance(header.get("kid"), str):
                raise InvalidToken()
            if not 1 <= len(header["kid"]) <= 256:
                raise InvalidToken()
            signing_key = self.jwks_client.get_signing_key_from_jwt(token)
            claims = jwt.decode(
                token,
                signing_key.key,
                algorithms=["RS256"],
                issuer=self.settings.cognito_issuer,
                audience=self.settings.cognito_audience,
                leeway=self.settings.jwt_leeway_seconds,
                options={
                    "require": ["exp", "iat", "iss", "sub", "token_use", "client_id"],
                    "verify_aud": self.settings.cognito_audience is not None,
                },
            )
            if (
                claims["token_use"] != "access"
                or claims["client_id"] not in self.settings.cognito_client_ids
            ):
                raise InvalidToken()
            groups = claims.get("cognito:groups", [])
            scope = claims.get("scope", "")
            if not isinstance(groups, list) or any(not isinstance(g, str) for g in groups):
                raise InvalidToken()
            if not isinstance(scope, str):
                raise InvalidToken()
            tenant = claims.get(self.settings.cognito_tenant_claim)
            return Principal(
                subject=claims["sub"],
                client_id=claims["client_id"],
                tenant_id=UUID(tenant) if tenant is not None else None,
                roles=frozenset(groups),
                scopes=frozenset(scope.split()),
            )
        except PyJWKClientConnectionError as exc:
            raise IdentityUnavailable() from exc
        except (jwt.PyJWTError, ValueError, TypeError, AttributeError) as exc:
            raise InvalidToken() from exc


bearer = HTTPBearer(auto_error=False)


def get_principal(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> Principal:
    if credentials is None:
        raise HTTPException(401, "Authentication required", headers={"WWW-Authenticate": "Bearer"})
    verifier: TokenVerifier | None = request.app.state.token_verifier
    if verifier is None:
        raise HTTPException(503, "Identity provider is not configured")
    try:
        principal = verifier.verify(credentials.credentials)
    except InvalidToken:
        raise HTTPException(
            401, "Invalid access token", headers={"WWW-Authenticate": "Bearer"}
        ) from None
    except IdentityUnavailable:
        raise HTTPException(503, "Identity provider is unavailable") from None
    request.state.principal = principal
    return principal


def require_roles(*roles: str):
    """Require at least one role; an empty requirement is a programming error."""
    if not roles:
        raise ValueError("At least one role is required")

    def dependency(principal: Annotated[Principal, Depends(get_principal)]) -> Principal:
        if not principal.roles.intersection(roles):
            raise HTTPException(403, "Insufficient role")
        return principal

    return dependency


def require_scopes(*scopes: str):
    """Require every listed OAuth scope, including for machine clients."""
    if not scopes:
        raise ValueError("At least one scope is required")

    def dependency(principal: Annotated[Principal, Depends(get_principal)]) -> Principal:
        if not set(scopes).issubset(principal.scopes):
            raise HTTPException(403, "Insufficient scope")
        return principal

    return dependency


def require_tenant(principal: Annotated[Principal, Depends(get_principal)]) -> UUID:
    if principal.tenant_id is None:
        raise HTTPException(403, "Venue membership required")
    return principal.tenant_id
