"""Tenant project discovery and membership administration."""
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.audit import record_audit_event
from app.auth.oidc import RequestContext, get_request_context, require_roles
from app.db.models import Project, ProjectMembership, TenantMembership, User
from app.db.session import get_db


router = APIRouter()


class ProjectCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    slug: str = Field(min_length=2, max_length=100, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    name: str = Field(min_length=2, max_length=200)


class ProjectMemberCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user_id: UUID
    role: str = Field(pattern=r"^(PROJECT_MANAGER|RESEARCHER|REVIEWER|VIEWER)$")


@router.get("/auth/me")
def current_membership(
    context: RequestContext = Depends(get_request_context),
    db: Annotated[Session, Depends(get_db)] = None,
):
    projects = list(db.scalars(select(Project).where(Project.id.in_(context.project_ids)).order_by(Project.slug)))
    return {
        "user_id": context.user_id,
        "issuer": context.principal.issuer,
        "subject": context.principal.subject,
        "email": context.principal.email,
        "tenant_id": context.tenant_id,
        "tenant_role": context.tenant_role,
        "roles": list(context.roles),
        "projects": [{"id": item.id, "slug": item.slug, "name": item.name} for item in projects],
        "selected_project_id": context.selected_project_id,
    }


@router.get("/projects")
def list_projects(
    context: RequestContext = Depends(get_request_context),
    db: Annotated[Session, Depends(get_db)] = None,
):
    rows = db.scalars(select(Project).where(Project.id.in_(context.project_ids)).order_by(Project.slug))
    return {"items": [{"id": item.id, "slug": item.slug, "name": item.name} for item in rows]}


@router.post("/projects", status_code=201,
             dependencies=[Depends(require_roles("TENANT_ADMIN", "SUPER_ADMIN"))])
def create_project(
    request: ProjectCreate,
    db: Annotated[Session, Depends(get_db)],
    context: RequestContext = Depends(get_request_context),
):
    duplicate = db.scalar(select(Project.id).where(Project.tenant_id == context.tenant_id, Project.slug == request.slug))
    if duplicate:
        raise HTTPException(status_code=409, detail="Project slug already exists in this tenant")
    project = Project(tenant_id=context.tenant_id, slug=request.slug, name=request.name, is_active=True)
    db.add(project)
    db.flush()
    record_audit_event(db, context, action="project.create", resource_type="project",
                       resource_id=project.id, metadata={"slug": project.slug})
    db.commit()
    return {"id": project.id, "slug": project.slug, "name": project.name}


@router.post("/projects/{project_id}/members", status_code=201,
             dependencies=[Depends(require_roles("TENANT_ADMIN", "SUPER_ADMIN"))])
def add_project_member(
    project_id: UUID,
    request: ProjectMemberCreate,
    db: Annotated[Session, Depends(get_db)],
    context: RequestContext = Depends(get_request_context),
):
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    user = db.get(User, request.user_id)
    if user is None or not user.is_active:
        raise HTTPException(status_code=404, detail="User not found")
    tenant_membership = db.scalar(select(TenantMembership).where(
        TenantMembership.tenant_id == context.tenant_id,
        TenantMembership.user_id == user.id,
        TenantMembership.is_active.is_(True),
    ))
    if tenant_membership is None:
        raise HTTPException(status_code=422, detail="User must be an active member of this tenant first")
    membership = db.scalar(select(ProjectMembership).where(
        ProjectMembership.project_id == project.id, ProjectMembership.user_id == user.id
    ))
    if membership is None:
        membership = ProjectMembership(project_id=project.id, user_id=user.id,
                                       role=request.role, is_active=True)
        db.add(membership)
    else:
        membership.role = request.role
        membership.is_active = True
    record_audit_event(db, context, action="project.member.upsert", resource_type="project_membership",
                       resource_id=membership.id, project_id=project.id,
                       metadata={"user_id": str(user.id), "role": request.role})
    db.commit()
    return {"id": membership.id, "project_id": project.id, "user_id": user.id, "role": membership.role}
