"""Validate installed Alembic RLS policies, not just hand-built test policies."""
import os

import pytest
from sqlalchemy import create_engine, text


TENANT_OWNED = (
    "audit_events", "research_tasks", "documents", "document_chunks",
    "evidences", "task_feedback", "evaluation_results", "human_reviews",
)
CACHE_OWNED = ("claims", "web_contents", "web_sources", "web_searches")
TASK_OWNED = (
    "agent_runs", "agent_steps", "agent_allowed_sources",
    "evidence_assessments", "agent_policy_decisions", "state_snapshots",
    "agent_reflections", "outbox_events", "tool_execution_receipts",
)


def test_migrated_postgres_rls_is_enabled_for_all_protected_tables():
    url = os.getenv("EVIDENCEPILOT_TEST_POSTGRES_URL")
    if not url:
        pytest.skip("A real PostgreSQL migration database is required")
    engine = create_engine(url)
    try:
        with engine.connect() as connection:
            version = connection.scalar(text("SELECT version_num FROM alembic_version"))
            assert version == "20261124_0007"
            rows = connection.execute(text(
                "SELECT relname, relrowsecurity, relforcerowsecurity "
                "FROM pg_class JOIN pg_namespace ON pg_namespace.oid=pg_class.relnamespace "
                "WHERE nspname=current_schema() "
                "AND relname = ANY(:tables)"
            ), {"tables": list(TENANT_OWNED + TASK_OWNED + CACHE_OWNED)}).all()
            status = {row[0]: (row[1], row[2]) for row in rows}
            assert set(status) == set(TENANT_OWNED + TASK_OWNED + CACHE_OWNED)
            assert all(rls_enabled and rls_forced for rls_enabled, rls_forced in status.values())

            policies = connection.execute(text(
                "SELECT tablename, policyname, cmd, qual, with_check "
                "FROM pg_policies WHERE schemaname=current_schema() "
                "AND tablename = ANY(:tables)"
            ), {"tables": list(TENANT_OWNED + TASK_OWNED + CACHE_OWNED)}).all()
            by_table = {row[0]: row for row in policies}
            assert set(by_table) == set(TENANT_OWNED + TASK_OWNED + CACHE_OWNED)
            for table in TENANT_OWNED:
                policy = by_table[table]
                assert policy[1] == f"{table}_tenant_isolation"
                assert policy[2] == "ALL"
                assert "tenant_id" in policy[3] and "app.tenant_id" in policy[3]
                assert "tenant_id" in policy[4] and "app.tenant_id" in policy[4]
            for table in CACHE_OWNED:
                policy = by_table[table]
                assert policy[1] == f"{table}_tenant_isolation"
                assert policy[2] == "ALL"
                assert "cache_scope" in policy[3] and "app.tenant_id" in policy[3]
                assert "cache_scope" in policy[4] and "app.tenant_id" in policy[4]
            for table in TASK_OWNED:
                policy = by_table[table]
                assert policy[1] == f"{table}_tenant_isolation"
                assert policy[2] == "ALL"
                assert "research_tasks" in policy[3] or "research_tasks" in policy[4]
                assert "app.tenant_id" in policy[3]
    finally:
        engine.dispose()
