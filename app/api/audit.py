"""Tenant/project scoped audit log inspection."""
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.oidc import RequestContext, get_request_context, require_roles
from app.db.models import AuditEvent
from app.db.session import get_db


router = APIRouter()


@router.get("/audit/events", dependencies=[Depends(require_roles("TENANT_ADMIN", "SUPER_ADMIN", "PROJECT_MANAGER"))])
def list_audit_events(
    db: Annotated[Session, Depends(get_db)],
    context: RequestContext = Depends(get_request_context),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    rows = db.scalars(
        select(AuditEvent).order_by(AuditEvent.created_at.desc(), AuditEvent.id)
        .offset(offset).limit(limit)
    )
    return {
        "tenant_id": context.tenant_id,
        "items": [{
            "id": row.id,
            "project_id": row.project_id,
            "actor_user_id": row.actor_user_id,
            "action": row.action,
            "resource_type": row.resource_type,
            "resource_id": row.resource_id,
            "outcome": row.outcome,
            "metadata": row.metadata_json,
            "created_at": row.created_at,
        } for row in rows],
        "limit": limit,
        "offset": offset,
    }
