"""Extend tenant RLS to task-owned execution and queue state."""
from alembic import op
from sqlalchemy import text


revision = "20261123_0005"
down_revision = "20261123_0004"
branch_labels = None
depends_on = None

TASK_SCOPED_POLICIES = {
    "agent_runs": "EXISTS (SELECT 1 FROM research_tasks t WHERE t.id = agent_runs.task_id AND t.tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)",
    "agent_steps": "EXISTS (SELECT 1 FROM research_tasks t WHERE t.id = agent_steps.task_id AND t.tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)",
    "agent_allowed_sources": "EXISTS (SELECT 1 FROM research_tasks t WHERE t.id = agent_allowed_sources.task_id AND t.tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)",
    "evidence_assessments": "EXISTS (SELECT 1 FROM research_tasks t WHERE t.id = evidence_assessments.task_id AND t.tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)",
    "agent_policy_decisions": "EXISTS (SELECT 1 FROM research_tasks t WHERE t.id = agent_policy_decisions.task_id AND t.tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)",
    "state_snapshots": "EXISTS (SELECT 1 FROM research_tasks t WHERE t.id = state_snapshots.task_id AND t.tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)",
    "agent_reflections": "EXISTS (SELECT 1 FROM research_tasks t WHERE t.id = agent_reflections.task_id AND t.tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)",
    "outbox_events": "EXISTS (SELECT 1 FROM research_tasks t WHERE t.id = outbox_events.aggregate_id AND t.tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)",
    "tool_execution_receipts": "EXISTS (SELECT 1 FROM agent_steps s JOIN research_tasks t ON t.id = s.task_id WHERE s.id = tool_execution_receipts.step_id AND t.tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)",
}


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    # Upgrade pending outbox events created before worker payloads carried scope.
    # This is required because RLS-protected workers must know the tenant before
    # their first lookup of the task row.
    bind.execute(text(
        "UPDATE outbox_events AS o SET payload = "
        "COALESCE(o.payload::jsonb, '{}'::jsonb) || "
        "jsonb_build_object('tenant_id', t.tenant_id::text, "
        "'project_id', t.project_id::text, 'user_id', t.created_by_user_id::text) "
        "FROM research_tasks AS t WHERE o.aggregate_id = t.id "
        "AND (o.payload::jsonb ->> 'tenant_id') IS NULL"
    ))
    for table, predicate in TASK_SCOPED_POLICIES.items():
        policy = f"{table}_tenant_isolation"
        bind.execute(text(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY"))
        bind.execute(text(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY"))
        bind.execute(text(f"DROP POLICY IF EXISTS {policy} ON {table}"))
        bind.execute(text(
            f"CREATE POLICY {policy} ON {table} USING ({predicate}) WITH CHECK ({predicate})"
        ))


def downgrade() -> None:
    # Retain security policies on rollback; removing them is not a safe downgrade.
    pass
