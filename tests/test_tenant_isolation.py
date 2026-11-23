from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.auth.oidc import Principal, get_request_context, require_roles
from app.db.models import (
    Project, ProjectMembership, ResearchTask, Tenant, TenantMembership, User,
)
from app.db.session import Base


@pytest.fixture
def tenant_db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine, autoflush=False) as db:
        tenant_a = Tenant(slug="tenant-a", name="Tenant A", external_id="tenant-a", is_active=True)
        tenant_b = Tenant(slug="tenant-b", name="Tenant B", external_id="tenant-b", is_active=True)
        user_a = User(issuer="https://id.example/realm", subject="user-a", is_active=True)
        user_b = User(issuer="https://id.example/realm", subject="user-b", is_active=True)
        db.add_all([tenant_a, tenant_b, user_a, user_b])
        db.flush()

        project_a1 = Project(tenant_id=tenant_a.id, slug="a-one", name="A One", is_active=True)
        project_a2 = Project(tenant_id=tenant_a.id, slug="a-two", name="A Two", is_active=True)
        project_b1 = Project(tenant_id=tenant_b.id, slug="b-one", name="B One", is_active=True)
        db.add_all([project_a1, project_a2, project_b1])
        db.flush()

        db.add_all([
            TenantMembership(tenant_id=tenant_a.id, user_id=user_a.id, role="RESEARCHER", is_active=True),
            TenantMembership(tenant_id=tenant_b.id, user_id=user_b.id, role="RESEARCHER", is_active=True),
            ProjectMembership(project_id=project_a1.id, user_id=user_a.id, role="RESEARCHER", is_active=True),
            ProjectMembership(project_id=project_b1.id, user_id=user_b.id, role="RESEARCHER", is_active=True),
        ])
        task_a1 = ResearchTask(tenant_id=tenant_a.id, project_id=project_a1.id,
                               query="Tenant A project one", budget=2)
        task_a2 = ResearchTask(tenant_id=tenant_a.id, project_id=project_a2.id,
                               query="Tenant A project two", budget=2)
        task_b1 = ResearchTask(tenant_id=tenant_b.id, project_id=project_b1.id,
                               query="Tenant B project one", budget=2)
        db.add_all([task_a1, task_a2, task_b1])
        db.commit()
        yield db, tenant_a, tenant_b, user_a, user_b, project_a1, project_a2, project_b1, task_a1, task_a2, task_b1
    engine.dispose()


def test_database_memberships_scope_tenant_and_project_queries(tenant_db):
    db, tenant_a, tenant_b, user_a, user_b, project_a1, project_a2, project_b1, task_a1, task_a2, task_b1 = tenant_db
    tenant_a_id, project_a1_id = tenant_a.id, project_a1.id
    task_a1_id, task_a2_id, task_b1_id = task_a1.id, task_a2.id, task_b1.id
    principal = Principal(
        subject="user-a", issuer="https://id.example/realm", audience=("evidencepilot-api",),
        roles=("SUPER_ADMIN",), tenant_id="tenant-a",
    )
    context = get_request_context(db, principal, None)
    assert context.tenant_id == tenant_a_id
    assert context.project_ids == (project_a1_id,)
    # A forged platform role in the token cannot grant admin access.
    assert "SUPER_ADMIN" not in context.roles
    assert "RESEARCHER" in context.roles

    visible = list(db.scalars(select(ResearchTask).order_by(ResearchTask.query)))
    assert [row.id for row in visible] == [task_a1_id]
    assert db.get(ResearchTask, task_a2_id) is None
    assert db.get(ResearchTask, task_b1_id) is None


def test_tenant_and_project_cross_scope_writes_are_rejected(tenant_db):
    db, tenant_a, tenant_b, user_a, user_b, project_a1, project_a2, project_b1, *_ = tenant_db
    tenant_a_id, tenant_b_id = tenant_a.id, tenant_b.id
    project_a2_id, project_b1_id = project_a2.id, project_b1.id
    principal = Principal(
        subject="user-a", issuer="https://id.example/realm", audience=("evidencepilot-api",),
        tenant_id="tenant-a",
    )
    get_request_context(db, principal, None)

    db.add(ResearchTask(tenant_id=tenant_b_id, project_id=project_b1_id,
                        query="cross tenant write", budget=2))
    with pytest.raises(PermissionError, match="Cross-tenant"):
        db.flush()
    db.rollback()
    # The user belongs to tenant A but not to project A2.
    get_request_context(db, principal, None)
    db.add(ResearchTask(tenant_id=tenant_a_id, project_id=project_a2_id,
                        query="cross project write", budget=2))
    with pytest.raises(PermissionError, match="Project membership"):
        db.flush()
    db.rollback()


def test_project_header_selects_only_an_existing_membership(tenant_db):
    db, tenant_a, tenant_b, user_a, user_b, project_a1, project_a2, project_b1, task_a1, task_a2, task_b1 = tenant_db
    principal = Principal(
        subject="user-a", issuer="https://id.example/realm", audience=("evidencepilot-api",),
        tenant_id="tenant-a",
    )
    with pytest.raises(HTTPException) as denied:
        get_request_context(db, principal, str(project_a2.id))
    assert denied.value.status_code == 404
    context = get_request_context(db, principal, str(project_a1.id))
    assert context.selected_project_id == project_a1.id


def test_user_without_active_tenant_membership_is_denied(tenant_db):
    db, tenant_a, tenant_b, user_a, user_b, *_ = tenant_db
    principal = Principal(
        subject="user-a", issuer="https://id.example/realm", audience=("evidencepilot-api",),
        tenant_id="tenant-b",
    )
    with pytest.raises(HTTPException) as denied:
        get_request_context(db, principal, None)
    assert denied.value.status_code == 403


def test_role_dependencies_use_database_membership_not_token_roles(tenant_db):
    db, tenant_a, tenant_b, user_a, user_b, *_ = tenant_db
    principal = Principal(
        subject="user-a", issuer="https://id.example/realm", audience=("evidencepilot-api",),
        roles=("SUPER_ADMIN",), tenant_id="tenant-a",
    )
    context = get_request_context(db, principal, None)
    with pytest.raises(HTTPException) as denied:
        require_roles("SUPER_ADMIN")(context)
    assert denied.value.status_code == 403
    assert require_roles("RESEARCHER")(context) is context



def test_fastapi_read_routes_apply_tenant_and_project_context(tenant_db):
    from fastapi.testclient import TestClient

    from app.auth.oidc import get_current_principal
    from app.db.session import get_db
    from app.main import app

    db, tenant_a, tenant_b, user_a, user_b, project_a1, project_a2, project_b1, task_a1, task_a2, task_b1 = tenant_db
    principal = Principal(
        subject="user-a",
        issuer="https://id.example/realm",
        audience=("evidencepilot-api",),
        roles=("SUPER_ADMIN",),
        tenant_id="tenant-a",
    )
    app.dependency_overrides[get_current_principal] = lambda: principal

    def override_db():
        yield db

    app.dependency_overrides[get_db] = override_db
    client = TestClient(app)
    try:
        response = client.get("/api/v1/tasks")
        assert response.status_code == 200, response.text
        assert {item["query"] for item in response.json()["items"]} == {"Tenant A project one"}
        assert client.get(f"/api/v1/tasks/{task_b1.id}").status_code == 404
        assert client.get(f"/api/v1/tasks/{task_a2.id}").status_code == 404

        selected = client.get("/api/v1/tasks", headers={"X-Project-ID": str(project_a1.id)})
        assert selected.status_code == 200
        assert [item["query"] for item in selected.json()["items"]] == ["Tenant A project one"]
    finally:
        client.close()
        app.dependency_overrides.clear()
