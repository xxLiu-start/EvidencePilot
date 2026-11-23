"""Enable PostgreSQL row-level security for tenant-owned records."""
from alembic import op
from sqlalchemy import text


revision = "20261123_0004"
down_revision = "20261123_0003"
branch_labels = None
depends_on = None

TENANT_TABLES = (
    "audit_events",
    "research_tasks",
    "documents",
    "document_chunks",
    "evidences",
    "task_feedback",
    "evaluation_results",
)


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        # SQLite remains the fast unit-test backend; PostgreSQL integration CI
        # exercises the actual RLS policy below.
        return
    for table in TENANT_TABLES:
        policy = f"{table}_tenant_isolation"
        bind.execute(text(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY"))
        bind.execute(text(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY"))
        bind.execute(text(f"DROP POLICY IF EXISTS {policy} ON {table}"))
        bind.execute(text(
            f"CREATE POLICY {policy} ON {table} "
            "USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid) "
            "WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)"
        ))


def downgrade() -> None:
    # Security policies are intentionally retained on downgrade to avoid
    # silently removing a tenant boundary during an application rollback.
    pass
