"""Enforce PostgreSQL row-level security for tenant-owned and task-owned data.

The API runtime role must be a non-owner, NOBYPASSRLS role. A separate trusted
dispatcher role may bypass RLS but receives privileges only on outbox_events.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect, select
from sqlalchemy.dialects import postgresql
import uuid


revision = "20261123_0004"
down_revision = "20261123_0003"
branch_labels = None
depends_on = None

LEGACY_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
LEGACY_PROJECT_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")


def _add_claim_scope(bind):
    columns = {item["name"] for item in inspect(bind).get_columns("claims")}
    uuid_type = postgresql.UUID(as_uuid=True)
    if "tenant_id" not in columns:
        op.add_column("claims", sa.Column("tenant_id", uuid_type, nullable=True))
    if "project_id" not in columns:
        op.add_column("claims", sa.Column("project_id", uuid_type, nullable=True))

    tenant_id = bind.execute(sa.text(
        "SELECT id FROM tenants WHERE slug = 'legacy-default' ORDER BY id LIMIT 1"
    )).scalar_one_or_none() or LEGACY_TENANT_ID
    project_id = bind.execute(sa.text(
        "SELECT id FROM projects WHERE tenant_id = :tenant_id AND slug = 'default' ORDER BY id LIMIT 1"
    ), {"tenant_id": tenant_id}).scalar_one_or_none() or LEGACY_PROJECT_ID

    bind.execute(sa.text("UPDATE claims SET tenant_id = :tenant_id WHERE tenant_id IS NULL"),
                 {"tenant_id": tenant_id})
    bind.execute(sa.text("UPDATE claims SET project_id = :project_id WHERE project_id IS NULL"),
                 {"project_id": project_id})

    foreign_keys = inspect(bind).get_foreign_keys("claims")
    if not any(item.get("constrained_columns") == ["tenant_id"] and item.get("referred_table") == "tenants"
               for item in foreign_keys):
        op.create_foreign_key("fk_claims_tenant_id_tenants", "claims", "tenants", ["tenant_id"], ["id"])
    if not any(item.get("constrained_columns") == ["project_id"] and item.get("referred_table") == "projects"
               for item in foreign_keys):
        op.create_foreign_key("fk_claims_project_id_projects", "claims", "projects", ["project_id"], ["id"])

    indexes = {item["name"] for item in inspect(bind).get_indexes("claims")}
    if "ix_claims_tenant_id" not in indexes:
        op.create_index("ix_claims_tenant_id", "claims", ["tenant_id"])
    if "ix_claims_project_id" not in indexes:
        op.create_index("ix_claims_project_id", "claims", ["project_id"])

    for item in inspect(bind).get_unique_constraints("claims"):
        if item.get("column_names") == ["claim_id", "claim_text"] and item.get("name"):
            op.drop_constraint(item["name"], "claims", type_="unique")
    unique_constraints = inspect(bind).get_unique_constraints("claims")
    if not any(item.get("column_names") == ["tenant_id", "project_id", "claim_id", "claim_text"]
               for item in unique_constraints):
        op.create_unique_constraint(
            "uq_claims_tenant_project_claim", "claims",
            ["tenant_id", "project_id", "claim_id", "claim_text"],
        )
    op.alter_column("claims", "tenant_id", existing_type=uuid_type, nullable=False)
    op.alter_column("claims", "project_id", existing_type=uuid_type, nullable=False)


def _tenant_project_policy(table: str) -> str:
    return f"""(
        tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
        AND (
            project_id IS NULL
            OR project_id::text = ANY(string_to_array(NULLIF(current_setting('app.project_ids', true), ''), ','))
        )
    )"""


def _task_policy(column: str = "task_id") -> str:
    return f"""EXISTS (
        SELECT 1 FROM research_tasks AS rls_task
        WHERE rls_task.id = {column}
          AND rls_task.tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
          AND rls_task.project_id::text = ANY(
              string_to_array(NULLIF(current_setting('app.project_ids', true), ''), ',')
          )
    )"""


def _policy(table: str, name: str, expression: str, *, command: str = "ALL") -> None:
    op.execute(sa.text(f'DROP POLICY IF EXISTS "{name}" ON "{table}"'))
    op.execute(sa.text(
        f'CREATE POLICY "{name}" ON "{table}" FOR {command} USING ({expression}) WITH CHECK ({expression})'
    ))


def _enable_force_rls(table: str) -> None:
    op.execute(sa.text(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY'))
    op.execute(sa.text(f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY'))


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        # SQLite is used for fast unit tests; production RLS is verified against
        # a real PostgreSQL instance by tests/test_postgres_rls.py.
        return

    _add_claim_scope(bind)

    direct_tables = [
        "research_tasks", "documents", "document_chunks", "evidences",
        "task_feedback", "evaluation_results", "audit_events", "claims",
    ]
    for table in direct_tables:
        _enable_force_rls(table)
        _policy(table, f"rls_{table}_tenant_project", _tenant_project_policy(table))

    # One-row worker bootstrap is SELECT-only. The task ID comes from the
    # internal queue/background executor; all subsequent queries receive the
    # tenant/project scope copied from that row.
    op.execute(sa.text('DROP POLICY IF EXISTS "rls_research_tasks_worker_bootstrap" ON research_tasks'))
    op.execute(sa.text("""
        CREATE POLICY rls_research_tasks_worker_bootstrap
        ON research_tasks FOR SELECT
        USING (id = NULLIF(current_setting('app.bootstrap_task_id', true), '')::uuid)
    """))

    identity_user = """(
        issuer = NULLIF(current_setting('app.identity_issuer', true), '')
        AND subject = NULLIF(current_setting('app.identity_subject', true), '')
    )"""
    current_user = "id = NULLIF(current_setting('app.user_id', true), '')::uuid"
    tenant_admin = "current_setting('app.tenant_admin', true) = 'true'"
    tenant_id = "NULLIF(current_setting('app.tenant_id', true), '')::uuid"
    project_ids = "string_to_array(NULLIF(current_setting('app.project_ids', true), ''), ',')"

    _enable_force_rls("users")
    _policy("users", "rls_users_self_or_tenant_admin",
            f"({identity_user} OR ({tenant_admin} AND id IN (SELECT user_id FROM tenant_memberships WHERE tenant_id = {tenant_id})))")

    _enable_force_rls("tenants")
    tenant_visibility = f"""(
        id = {tenant_id}
        OR external_id = NULLIF(current_setting('app.tenant_external_id', true), '')
        OR (slug = 'legacy-default'
            AND current_setting('app.identity_issuer', true) = 'local'
            AND current_setting('app.identity_subject', true) = 'local-development')
        OR id IN (SELECT tenant_id FROM tenant_memberships WHERE user_id = NULLIF(current_setting('app.user_id', true), '')::uuid)
    )"""
    _policy("tenants", "rls_tenants_membership", tenant_visibility)

    _enable_force_rls("tenant_memberships")
    membership_visibility = f"""(
        user_id = NULLIF(current_setting('app.user_id', true), '')::uuid
        OR (tenant_id = {tenant_id} AND {tenant_admin})
    )"""
    _policy("tenant_memberships", "rls_tenant_memberships_self_or_admin", membership_visibility)

    _enable_force_rls("projects")
    project_visibility = f"""(
        tenant_id = {tenant_id}
        AND (
            {tenant_admin}
            OR id::text = ANY({project_ids})
            OR EXISTS (
                SELECT 1 FROM project_memberships AS rls_pm
                WHERE rls_pm.project_id = projects.id
                  AND rls_pm.user_id = NULLIF(current_setting('app.user_id', true), '')::uuid
                  AND rls_pm.is_active
            )
        )
    )"""
    _policy("projects", "rls_projects_membership", project_visibility)

    _enable_force_rls("project_memberships")
    project_membership_visibility = f"""(
        user_id = NULLIF(current_setting('app.user_id', true), '')::uuid
        OR ({tenant_admin} AND project_id::text = ANY({project_ids}))
    )"""
    project_membership_check = f"""(
        user_id = NULLIF(current_setting('app.user_id', true), '')::uuid
        OR (
            {tenant_admin}
            AND project_id::text = ANY({project_ids})
            AND user_id IN (SELECT user_id FROM tenant_memberships WHERE tenant_id = {tenant_id})
        )
    )"""
    op.execute(sa.text('DROP POLICY IF EXISTS "rls_project_memberships_self_or_admin" ON project_memberships'))
    op.execute(sa.text(f"""
        CREATE POLICY rls_project_memberships_self_or_admin
        ON project_memberships FOR ALL
        USING ({project_membership_visibility})
        WITH CHECK ({project_membership_check})
    """))

    child_tables = {
        "agent_runs": ("task_id",),
        "agent_steps": ("task_id",),
        "agent_allowed_sources": ("task_id",),
        "evidence_assessments": ("task_id",),
        "agent_policy_decisions": ("task_id",),
        "state_snapshots": ("task_id",),
        "agent_reflections": ("task_id",),
        "outbox_events": ("aggregate_id",),
    }
    for table, (column,) in child_tables.items():
        _enable_force_rls(table)
        _policy(table, f"rls_{table}_task_scope", _task_policy(column))

    _enable_force_rls("tool_execution_receipts")
    receipt_scope = f"""EXISTS (
        SELECT 1
        FROM agent_steps AS rls_step
        JOIN research_tasks AS rls_task ON rls_task.id = rls_step.task_id
        WHERE rls_step.id = tool_execution_receipts.step_id
          AND rls_task.tenant_id = {tenant_id}
          AND rls_task.project_id::text = ANY({project_ids})
    )"""
    _policy("tool_execution_receipts", "rls_tool_receipts_task_scope", receipt_scope)

    _enable_force_rls("web_searches")
    search_scope = f"""EXISTS (
        SELECT 1 FROM claims AS rls_claim
        WHERE rls_claim.id = web_searches.claim_record_id
          AND rls_claim.tenant_id = {tenant_id}
          AND rls_claim.project_id::text = ANY({project_ids})
    )"""
    _policy("web_searches", "rls_web_searches_claim_scope", search_scope)

    # These are immutable/public-source cache records shared by tenants. Search
    # query strings are cleared below and no private uploaded documents are stored here.
    for table in ("web_contents", "web_sources", "retrieval_index_revision"):
        _enable_force_rls(table)
        _policy(table, f"rls_{table}_shared_cache", "true")

    # A global source cache must never retain tenant research queries.
    bind.execute(sa.text("UPDATE web_sources SET search_queries = '[]'::json"))


def downgrade() -> None:
    # Security policies are intentionally not removed automatically. Disabling
    # RLS could expose tenant data; rollback must be an explicit security change.
    pass
