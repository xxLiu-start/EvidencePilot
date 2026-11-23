"""Add persisted tenant/project ownership and backfill legacy rows safely."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect, select
from sqlalchemy.dialects import postgresql
import uuid

from app.db.session import Base
from app.db import models  # noqa: F401


revision = "20261123_0003"
down_revision = "20261123_0002"
branch_labels = None
depends_on = None

LEGACY_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
LEGACY_PROJECT_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")

OWNED_TABLES = {
    "research_tasks": ("created_by_user_id",),
    "documents": ("created_by_user_id",),
    "document_chunks": (),
    "evidences": (),
    "task_feedback": ("created_by_user_id",),
    "evaluation_results": (),
}


def _column_type(bind):
    return postgresql.UUID(as_uuid=True) if bind.dialect.name == "postgresql" else sa.Uuid(as_uuid=True)


def _ensure_column(table: str, name: str, column_type, *, nullable=True):
    bind = op.get_bind()
    existing = {column["name"] for column in inspect(bind).get_columns(table)}
    if name not in existing:
        op.add_column(table, sa.Column(name, column_type, nullable=nullable))


def _seed_legacy_tenant_and_project(bind):
    from app.db.models import Tenant, Project

    tenant = bind.execute(select(Tenant.id).where(Tenant.slug == "legacy-default")).scalar_one_or_none()
    if tenant is None:
        bind.execute(sa.insert(Tenant).values(
            id=LEGACY_TENANT_ID, slug="legacy-default", name="Legacy Default Tenant",
            external_id=None, is_active=True,
        ))
        tenant = LEGACY_TENANT_ID
    project = bind.execute(select(Project.id).where(
        Project.tenant_id == tenant, Project.slug == "default"
    )).scalar_one_or_none()
    if project is None:
        bind.execute(sa.insert(Project).values(
            id=LEGACY_PROJECT_ID, tenant_id=tenant, slug="default",
            name="Default Legacy Project", is_active=True,
        ))
    return tenant, project or LEGACY_PROJECT_ID


def _ensure_postgres_fk(table: str, column: str, target: str, name: str):
    bind = op.get_bind()
    foreign_keys = inspect(bind).get_foreign_keys(table)
    exists = any(item.get("constrained_columns") == [column]
                 and item.get("referred_table") == target for item in foreign_keys)
    if not exists:
        op.create_foreign_key(name, table, target, [column], ["id"])


def upgrade() -> None:
    bind = op.get_bind()
    # Create membership/audit tables and ownership columns on a fresh database.
    # Existing tables are altered below because create_all is intentionally non-destructive.
    Base.metadata.create_all(bind=bind, checkfirst=True)

    tenant_id, project_id = _seed_legacy_tenant_and_project(bind)
    uuid_type = _column_type(bind)

    for table, extra_columns in OWNED_TABLES.items():
        _ensure_column(table, "tenant_id", uuid_type)
        _ensure_column(table, "project_id", uuid_type)
        for name in extra_columns:
            _ensure_column(table, name, uuid_type)

        bind.execute(sa.text(
            f"UPDATE {table} SET tenant_id = :tenant_id WHERE tenant_id IS NULL"
        ), {"tenant_id": tenant_id})
        bind.execute(sa.text(
            f"UPDATE {table} SET project_id = :project_id WHERE project_id IS NULL"
        ), {"project_id": project_id})

        for column in ("tenant_id", "project_id"):
            index_name = f"ix_{table}_{column}"
            indexes = {item["name"] for item in inspect(bind).get_indexes(table)}
            if index_name not in indexes:
                op.create_index(index_name, table, [column])

        if bind.dialect.name == "postgresql":
            _ensure_postgres_fk(table, "tenant_id", "tenants", f"fk_{table}_tenant_id_tenants")
            _ensure_postgres_fk(table, "project_id", "projects", f"fk_{table}_project_id_projects")
            for name in extra_columns:
                _ensure_postgres_fk(table, name, "users", f"fk_{table}_{name}_users")
            if table != "evaluation_results":
                op.alter_column(table, "tenant_id", existing_type=uuid_type, nullable=False)
                op.alter_column(table, "project_id", existing_type=uuid_type, nullable=False)


def downgrade() -> None:
    # Ownership backfill is security-critical and cannot be safely reversed.
    # Keep the columns and membership data rather than risk cross-tenant exposure.
    pass
