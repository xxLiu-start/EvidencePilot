"""Create tables and apply additive local-development schema upgrades."""

from sqlalchemy import Engine, inspect, select, text
from sqlalchemy import Uuid
import uuid

from app.db.session import Base
from app.retrieval.embedding import BGE_M3_DIMENSIONS


def initialize_database(engine: Engine) -> None:
    # Ensure tables have been registered even for standalone CLI callers.
    from app.db import models  # noqa: F401
    from app.db.models import Project, Tenant

    with engine.begin() as connection:
        postgres = connection.dialect.name == "postgresql"
        if postgres:
            connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        Base.metadata.create_all(connection)
        if "cache_scope" not in {c["name"] for c in inspect(connection).get_columns("web_contents")}:
            raise RuntimeError("Legacy cache schema requires Alembic upgrade; do not start with global cache keys")

        additions = {
            "research_tasks": {
                "domain_id": "VARCHAR(64) NOT NULL DEFAULT 'general_research'",
                "tenant_id": "UUID" if postgres else "CHAR(32)",
                "project_id": "UUID" if postgres else "CHAR(32)",
                "created_by_user_id": "UUID" if postgres else "CHAR(32)",
            },
            "documents": {
                "tenant_id": "UUID" if postgres else "CHAR(32)",
                "project_id": "UUID" if postgres else "CHAR(32)",
                "created_by_user_id": "UUID" if postgres else "CHAR(32)",
            },
            "document_chunks": {
                "tenant_id": "UUID" if postgres else "CHAR(32)",
                "project_id": "UUID" if postgres else "CHAR(32)",
            },
            "evidences": {
                "scope": "JSON NOT NULL DEFAULT '{}'",
                "tenant_id": "UUID" if postgres else "CHAR(32)",
                "project_id": "UUID" if postgres else "CHAR(32)",
            },
            "task_feedback": {
                "tenant_id": "UUID" if postgres else "CHAR(32)",
                "project_id": "UUID" if postgres else "CHAR(32)",
                "created_by_user_id": "UUID" if postgres else "CHAR(32)",
            },
            "evaluation_results": {
                "tenant_id": "UUID" if postgres else "CHAR(32)",
                "project_id": "UUID" if postgres else "CHAR(32)",
            },
            "agent_runs": {
                "owner_token": "VARCHAR(64)",
                "lease_expires_at": "TIMESTAMP WITH TIME ZONE" if postgres else "DATETIME",
                "checkpoint_seq": "INTEGER NOT NULL DEFAULT 0",
            },
        }
        for table, fields in additions.items():
            columns = {column["name"] for column in inspect(connection).get_columns(table)}
            for name, definition in fields.items():
                if name not in columns:
                    connection.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {definition}"))

        tenant_id = connection.execute(select(Tenant.id).where(Tenant.slug == "legacy-default")).scalar_one_or_none()
        if tenant_id is None:
            tenant = Tenant(id=uuid.uuid4(), slug="legacy-default", name="Legacy Default Tenant", is_active=True)
            connection.execute(Tenant.__table__.insert().values(
                id=tenant.id, slug=tenant.slug, name=tenant.name, external_id=None, is_active=True
            ))
            tenant_id = tenant.id
        project_id = connection.execute(select(Project.id).where(
            Project.tenant_id == tenant_id, Project.slug == "default"
        )).scalar_one_or_none()
        if project_id is None:
            project = Project(id=uuid.uuid4(), tenant_id=tenant_id, slug="default", name="Default Legacy Project", is_active=True)
            connection.execute(Project.__table__.insert().values(
                id=project.id, tenant_id=tenant_id, slug=project.slug, name=project.name, is_active=True
            ))
            project_id = project.id

        for table_name in ("research_tasks", "documents", "document_chunks",
                           "task_feedback", "evaluation_results"):
            table = Base.metadata.tables[table_name]
            connection.execute(table.update().where(table.c.tenant_id.is_(None)).values(tenant_id=tenant_id))
            connection.execute(table.update().where(
                table.c.tenant_id == tenant_id, table.c.project_id.is_(None)
            ).values(project_id=project_id))

        if postgres:
            connection.execute(text(
                "ALTER TABLE document_chunks "
                f"ADD COLUMN IF NOT EXISTS embedding vector({BGE_M3_DIMENSIONS}), "
                "ADD COLUMN IF NOT EXISTS embedding_model text"
            ))
            connection.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_document_chunks_embedding_hnsw "
                "ON document_chunks USING hnsw (embedding vector_cosine_ops) "
                "WHERE embedding IS NOT NULL"
            ))
            connection.execute(text(
                "ALTER TABLE evidences ALTER COLUMN source_url DROP NOT NULL, "
                "ADD COLUMN IF NOT EXISTS chunk_id uuid REFERENCES document_chunks(id)"
            ))
