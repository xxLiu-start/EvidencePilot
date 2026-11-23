"""Non-destructive baseline for existing EvidencePilot schemas.

This baseline creates missing objects and applies additive compatibility columns.
It intentionally does not drop existing tables or data on downgrade.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

from app.db.session import Base
from app.db import models  # noqa: F401


revision = "20261123_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    is_postgres = bind.dialect.name == "postgresql"
    if is_postgres:
        op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    # create_all creates missing tables and is non-destructive for existing tables.
    Base.metadata.create_all(bind=bind, checkfirst=True)

    if is_postgres:
        op.execute(
            "ALTER TABLE research_tasks ADD COLUMN IF NOT EXISTS "
            "domain_id VARCHAR(64) NOT NULL DEFAULT 'general_research'"
        )
        op.execute(
            "ALTER TABLE agent_runs "
            "ADD COLUMN IF NOT EXISTS owner_token VARCHAR(64), "
            "ADD COLUMN IF NOT EXISTS lease_expires_at TIMESTAMP WITH TIME ZONE, "
            "ADD COLUMN IF NOT EXISTS checkpoint_seq INTEGER NOT NULL DEFAULT 0"
        )
        op.execute("ALTER TABLE evidences ADD COLUMN IF NOT EXISTS scope JSON NOT NULL DEFAULT '{}'")
        op.execute(
            "ALTER TABLE document_chunks "
            "ADD COLUMN IF NOT EXISTS embedding vector(1024), "
            "ADD COLUMN IF NOT EXISTS embedding_model TEXT"
        )
        op.execute(
            "CREATE INDEX IF NOT EXISTS ix_document_chunks_embedding_hnsw "
            "ON document_chunks USING hnsw (embedding vector_cosine_ops) "
            "WHERE embedding IS NOT NULL"
        )
        op.execute(
            "ALTER TABLE evidences ALTER COLUMN source_url DROP NOT NULL, "
            "ADD COLUMN IF NOT EXISTS chunk_id UUID REFERENCES document_chunks(id)"
        )
    else:
        inspector = inspect(bind)
        additions = {
            "research_tasks": [
                ("domain_id", sa.Column("domain_id", sa.String(64), nullable=False, server_default="general_research")),
            ],
            "agent_runs": [
                ("owner_token", sa.Column("owner_token", sa.String(64), nullable=True)),
                ("lease_expires_at", sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True)),
                ("checkpoint_seq", sa.Column("checkpoint_seq", sa.Integer(), nullable=False, server_default="0")),
            ],
            "evidences": [
                ("scope", sa.Column("scope", sa.JSON(), nullable=False, server_default=sa.text("'{}'"))),
                ("chunk_id", sa.Column("chunk_id", sa.Uuid(), nullable=True)),
            ],
            "document_chunks": [
                ("embedding", sa.Column("embedding", sa.JSON(), nullable=True)),
                ("embedding_model", sa.Column("embedding_model", sa.Text(), nullable=True)),
            ],
        }
        for table, columns in additions.items():
            existing = {column["name"] for column in inspect(bind).get_columns(table)}
            for name, column in columns:
                if name not in existing:
                    op.add_column(table, column)


def downgrade() -> None:
    # This baseline may be adopted by a database containing user data. It is
    # intentionally irreversible; future revisions should provide explicit,
    # data-safe downgrade operations for their own changes.
    pass
