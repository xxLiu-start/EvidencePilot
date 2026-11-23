"""OIDC JWT verification boundary using the provider's JWKS endpoint.

This module authenticates a principal. It deliberately does not claim to implement
tenant authorization or object-level access control; those must be enforced by the
service/data-access layer before multi-user production deployment.
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Callable
from uuid import UUID
from urllib.parse import urlsplit

import jwt
from fastapi import Depends, Header, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt import InvalidTokenError, PyJWKClient
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.db.models import (DocumentChunk, DocumentRecord, EvidenceRecord, EvaluationResult, Project,
                           ProjectMembership, ResearchTask, TaskFeedback, Tenant, TenantMembership, User)
from app.db.session import get_db
from app.db.rls import apply_rls_context, set_session_scope

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
    issuer = urlsplit(settings.oidc_issuer)
    jwks = urlsplit(settings.oidc_jwks_url)
    for value, parsed in (("OIDC_ISSUER", issuer), ("OIDC_JWKS_URL", jwks)):
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
            raise RuntimeError(f"{value} must be a valid HTTP(S) URL without embedded credentials or fragments")
    if settings.environment == "production" and (issuer.scheme != "https" or jwks.scheme != "https"):
        raise RuntimeError("Production OIDC issuer and JWKS URLs must use HTTPS")


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
        if not isinstance(claims.get("sub"), str) or not claims["sub"].strip():
            raise InvalidTokenError("Missing subject")
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
                         roles=("RESEARCHER", "TENANT_ADMIN", "SUPER_ADMIN"), development_identity=True)
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Bearer token required",
                            headers={"WWW-Authenticate": "Bearer"})
    return verify_access_token(credentials.credentials)


@dataclass(frozen=True)
class RequestContext:
    principal: Principal
    user_id: UUID
    tenant_id: UUID
    tenant_role: str
    project_ids: tuple[UUID, ...]
    selected_project_id: UUID | None
    roles: tuple[str, ...]


def _development_context(db: Session) -> tuple[User, Tenant, TenantMembership]:
    """Create a single local-only identity in a non-production database."""
    for key in ("tenant_id", "user_id", "project_ids", "selected_project_id", "selected_project_ids"):
        db.info.pop(key, None)
    tenant = db.scalar(select(Tenant).where(Tenant.slug == "legacy-default"))
    if tenant is None:
        tenant = db.scalar(select(Tenant).where(Tenant.slug == "local-development"))
    if tenant is None:
        tenant = Tenant(slug="local-development", name="EvidencePilot Local Development",
                        external_id="local-development", is_active=True)
        db.add(tenant)
        db.flush()
    set_session_scope(db, tenant_id=tenant.id, identity_issuer="local",
                      identity_subject="local-development")
    user = db.scalar(select(User).where(User.issuer == "local", User.subject == "local-development"))
    if user is None:
        user = User(issuer="local", subject="local-development", email="developer@localhost", is_active=True)
        db.add(user)
        db.flush()
    db.info["user_id"] = user.id
    apply_rls_context(db.connection(), db.info)
    membership = db.scalar(select(TenantMembership).where(
        TenantMembership.tenant_id == tenant.id, TenantMembership.user_id == user.id
    ))
    if membership is None:
        membership = TenantMembership(tenant_id=tenant.id, user_id=user.id, role="SUPER_ADMIN", is_active=True)
        db.add(membership)
        db.flush()
    set_session_scope(db, tenant_id=tenant.id, user_id=user.id, tenant_role=membership.role,
                      identity_issuer="local", identity_subject="local-development")
    project = db.scalar(select(Project).where(Project.tenant_id == tenant.id, Project.slug == "default"))
    if project is None:
        project = Project(tenant_id=tenant.id, slug="default", name="Default Development Project", is_active=True)
        db.add(project)
        db.flush()
    project_membership = db.scalar(select(ProjectMembership).where(
        ProjectMembership.project_id == project.id, ProjectMembership.user_id == user.id
    ))
    if project_membership is None:
        db.add(ProjectMembership(project_id=project.id, user_id=user.id, role="SUPER_ADMIN", is_active=True))
    set_session_scope(db, tenant_id=tenant.id, user_id=user.id, tenant_role=membership.role,
                      project_ids=(project.id,), selected_project_id=project.id,
                      identity_issuer="local", identity_subject="local-development")
    # Development databases and test fixtures may predate the Alembic ownership
    # migration. Attach only unowned legacy rows to the local development scope.
    for model in (ResearchTask, DocumentRecord, DocumentChunk, EvidenceRecord, TaskFeedback, EvaluationResult):
        db.execute(update(model).where(model.tenant_id.is_(None)).values(tenant_id=tenant.id))
        db.execute(update(model).where(
            model.tenant_id == tenant.id, model.project_id.is_(None)
        ).values(project_id=project.id))
    db.commit()
    db.refresh(user)
    db.refresh(tenant)
    db.refresh(membership)
    return user, tenant, membership


def get_request_context(
    db: Session = Depends(get_db),
    principal: Principal = Depends(get_current_principal),
    project_header: str | None = Header(default=None, alias="X-Project-ID"),
) -> RequestContext:
    """Resolve a verified identity to persisted tenant and project memberships.

    Token role claims are not authorization grants. Database memberships are the
    authority for tenant and project access.
    """
    for key in ("tenant_id", "user_id", "project_ids", "selected_project_id", "selected_project_ids",
                "tenant_role", "tenant_external_id", "identity_issuer", "identity_subject"):
        db.info.pop(key, None)
    db.info.update({
        "identity_issuer": principal.issuer,
        "identity_subject": principal.subject,
        "tenant_external_id": principal.tenant_id,
    })
    # The identity claims are installed before the first SELECT so RLS can
    # expose only this OIDC subject while memberships are resolved.
    apply_rls_context(db.connection(), db.info)
    if principal.development_identity:
        if settings.environment == "production":
            raise HTTPException(status_code=503, detail="Development identity is unavailable in production")
        user, tenant, membership = _development_context(db)
    else:
        user = db.scalar(select(User).where(
            User.issuer == principal.issuer,
            User.subject == principal.subject,
            User.is_active.is_(True),
        ))
        if user is None:
            raise HTTPException(status_code=403, detail="User is not provisioned")
        db.info["user_id"] = user.id
        apply_rls_context(db.connection(), db.info)
        memberships = list(db.scalars(select(TenantMembership).where(
            TenantMembership.user_id == user.id, TenantMembership.is_active.is_(True)
        )))
        if principal.tenant_id:
            tenant = db.scalar(select(Tenant).where(
                Tenant.external_id == principal.tenant_id, Tenant.is_active.is_(True)
            ))
            membership = next((item for item in memberships if tenant and item.tenant_id == tenant.id), None)
        elif len(memberships) == 1:
            membership = memberships[0]
            tenant = db.get(Tenant, membership.tenant_id)
        else:
            raise HTTPException(status_code=403, detail="An active tenant selection is required")
        if tenant is None or membership is None or not tenant.is_active:
            raise HTTPException(status_code=403, detail="Active tenant membership required")

    tenant_role = membership.role.upper()
    set_session_scope(
        db, tenant_id=tenant.id, user_id=user.id, tenant_role=tenant_role,
        identity_issuer=principal.issuer, identity_subject=principal.subject,
        tenant_external_id=principal.tenant_id,
    )
    if tenant_role in {"TENANT_ADMIN", "SUPER_ADMIN"}:
        projects = list(db.scalars(select(Project).where(
            Project.tenant_id == tenant.id, Project.is_active.is_(True)
        ).order_by(Project.slug)))
        project_roles: dict[UUID, str] = {project.id: tenant_role for project in projects}
    else:
        rows = db.execute(
            select(Project, ProjectMembership.role)
            .join(ProjectMembership, ProjectMembership.project_id == Project.id)
            .where(
                Project.tenant_id == tenant.id,
                Project.is_active.is_(True),
                ProjectMembership.user_id == user.id,
                ProjectMembership.is_active.is_(True),
            )
            .order_by(Project.slug)
        )
        project_roles = {project.id: role.upper() for project, role in rows}
    project_ids = tuple(project_roles)
    if not project_ids:
        raise HTTPException(status_code=403, detail="Active project membership required")

    selected_project_id = None
    if project_header:
        try:
            selected_project_id = UUID(project_header)
        except ValueError:
            raise HTTPException(status_code=400, detail="X-Project-ID must be a UUID") from None
        if selected_project_id not in project_roles:
            raise HTTPException(status_code=404, detail="Project not found")

    # Project roles are not additive across a user's projects. A role held in
    # Project A must never authorize writes in Project B.
    if selected_project_id is not None:
        effective_project_roles = {project_roles[selected_project_id]}
    elif len(project_roles) == 1:
        effective_project_roles = set(project_roles.values())
    else:
        # Multi-project write operations must select a project explicitly.
        effective_project_roles = set()
    roles = {tenant_role, *effective_project_roles}
    context = RequestContext(
        principal=principal,
        user_id=user.id,
        tenant_id=tenant.id,
        tenant_role=tenant_role,
        project_ids=project_ids,
        selected_project_id=selected_project_id,
        roles=tuple(sorted(roles)),
    )
    set_session_scope(
        db, tenant_id=tenant.id, user_id=user.id, tenant_role=tenant_role,
        project_ids=project_ids, selected_project_id=selected_project_id,
        identity_issuer=principal.issuer, identity_subject=principal.subject,
        tenant_external_id=principal.tenant_id,
    )
    # Do not let an ORM identity loaded before tenant resolution bypass SQL filters.
    db.expunge_all()
    return context


def require_roles(*allowed_roles: str) -> Callable:
    required = {role.upper() for role in allowed_roles}

    def dependency(context: RequestContext = Depends(get_request_context)):
        # Accept Principal only for backwards-compatible direct unit tests. FastAPI
        # always supplies RequestContext, whose roles come from persisted memberships.
        roles = set(getattr(context, "roles", ()))
        if not roles:
            roles = set(getattr(context, "roles", ()))
        if not required.intersection(roles):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient role")
        return context

    return dependency
