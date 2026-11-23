"""OIDC JWT verification boundary using the provider's JWKS endpoint.

This module authenticates a principal. It deliberately does not claim to implement
tenant authorization or object-level access control; those must be enforced by the
service/data-access layer before multi-user production deployment.
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Callable

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt import InvalidTokenError, PyJWKClient

from app.config import settings


_bearer = HTTPBearer(auto_error=False)
_ALLOWED_ALGORITHMS = {"RS256", "ES256"}


@dataclass(frozen=True)
class Principal:
    subject: str
    issuer: str
    audience: tuple[str, ...]
    roles: tuple[str, ...] = ()
    email: str | None = None
    tenant_id: str | None = None
    development_identity: bool = False


@lru_cache(maxsize=8)
def _jwks_client(url: str) -> PyJWKClient:
    return PyJWKClient(url, cache_keys=True, timeout=5)


def validate_auth_configuration() -> None:
    if not settings.auth_enabled:
        if settings.environment == "production":
            raise RuntimeError("AUTH_ENABLED must be true in production")
        return
    if not settings.oidc_issuer or not settings.oidc_audience or not settings.oidc_jwks_url:
        raise RuntimeError("OIDC_ISSUER, OIDC_AUDIENCE and OIDC_JWKS_URL are required")
    if settings.environment == "production" and not settings.oidc_jwks_url.startswith("https://"):
        raise RuntimeError("Production OIDC_JWKS_URL must use HTTPS")


def _roles_from_claims(claims: dict) -> tuple[str, ...]:
    roles = set()
    direct = claims.get("roles", [])
    if isinstance(direct, list):
        roles.update(item for item in direct if isinstance(item, str))
    realm_access = claims.get("realm_access", {})
    if isinstance(realm_access, dict) and isinstance(realm_access.get("roles"), list):
        roles.update(item for item in realm_access["roles"] if isinstance(item, str))
    return tuple(sorted(roles))


def verify_access_token(token: str) -> Principal:
    if not settings.auth_enabled:
        raise RuntimeError("OIDC authentication is disabled")
    try:
        header = jwt.get_unverified_header(token)
        algorithm = header.get("alg")
        if algorithm not in _ALLOWED_ALGORITHMS:
            raise InvalidTokenError("Unsupported signing algorithm")
        signing_key = _jwks_client(settings.oidc_jwks_url).get_signing_key_from_jwt(token).key
        claims = jwt.decode(
            token,
            signing_key,
            algorithms=[algorithm],
            issuer=settings.oidc_issuer,
            audience=settings.oidc_audience,
            options={"require": ["exp", "iat", "iss", "sub", "aud"]},
            leeway=30,
        )
        audiences = claims.get("aud", [])
        if isinstance(audiences, str):
            audiences = [audiences]
        return Principal(
            subject=claims["sub"],
            issuer=claims["iss"],
            audience=tuple(audiences),
            roles=_roles_from_claims(claims),
            email=claims.get("email") if isinstance(claims.get("email"), str) else None,
            tenant_id=claims.get("tenant_id") if isinstance(claims.get("tenant_id"), str) else None,
        )
    except (InvalidTokenError, KeyError, ValueError, jwt.PyJWTError):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="Invalid or expired access token",
                            headers={"WWW-Authenticate": "Bearer"}) from None
    except Exception:
        # JWKS/network failures are dependency outages, not invalid credentials.
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail="Identity provider temporarily unavailable") from None


def get_current_principal(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> Principal:
    if not settings.auth_enabled:
        if settings.environment == "production":
            raise HTTPException(status_code=503, detail="Authentication is not configured")
        return Principal(subject="local-development", issuer="local", audience=("evidencepilot-api",),
                         roles=("RESEARCHER",), development_identity=True)
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Bearer token required",
                            headers={"WWW-Authenticate": "Bearer"})
    return verify_access_token(credentials.credentials)


def require_roles(*allowed_roles: str) -> Callable:
    required = set(allowed_roles)

    def dependency(principal: Principal = Depends(get_current_principal)) -> Principal:
        if not required.intersection(principal.roles):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient role")
        return principal

    return dependency
