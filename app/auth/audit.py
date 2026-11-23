"""Tenant-scoped audit event helpers."""
from __future__ import annotations

from typing import Any
from uuid import UUID

from app.auth.oidc import RequestContext
from app.db.models import AuditEvent


def require_selected_project(context: RequestContext) -> UUID:
    if context.selected_project_id is not None:
        return context.selected_project_id
    if len(context.project_ids) == 1:
        return context.project_ids[0]
    from fastapi import HTTPException

    raise HTTPException(status_code=400, detail="Select a project with the X-Project-ID header")


def record_audit_event(
    db,
    context: RequestContext,
    *,
    action: str,
    resource_type: str,
    resource_id: str | UUID | None = None,
    project_id: UUID | None = None,
    outcome: str = "success",
    metadata: dict[str, Any] | None = None,
) -> AuditEvent:
    """Append a minimal audit record; never store tokens, secrets or full document bodies."""
    event = AuditEvent(
        tenant_id=context.tenant_id,
        project_id=project_id if project_id is not None else context.selected_project_id,
        actor_user_id=context.user_id,
        action=action[:100],
        resource_type=resource_type[:100],
        resource_id=str(resource_id)[:200] if resource_id is not None else None,
        outcome=outcome[:20],
        metadata_json=metadata or {},
    )
    db.add(event)
    return event
