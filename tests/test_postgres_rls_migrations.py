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


def test_migrated_cache_rls_isolates_identical_hash_url_and_claim_under_runtime_role():
    from datetime import datetime, timezone
    from uuid import uuid4
    from sqlalchemy.exc import DBAPIError

    url = os.getenv("EVIDENCEPILOT_TEST_POSTGRES_URL")
    if not url:
        pytest.skip("A real PostgreSQL migration database is required")
    engine = create_engine(url)
    role = "ep_cache_" + uuid4().hex[:16]
    a, b = str(uuid4()), str(uuid4())
    content_hash = uuid4().hex + uuid4().hex
    canonical = "https://example.org/" + uuid4().hex
    try:
        with engine.begin() as conn:
            conn.execute(text(f'CREATE ROLE "{role}" NOLOGIN NOSUPERUSER NOBYPASSRLS'))
            schema = conn.scalar(text("SELECT current_schema()"))
            conn.execute(text(f'GRANT USAGE ON SCHEMA "{schema}" TO "{role}"'))
            for table in CACHE_OWNED:
                conn.execute(text(f'GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO "{role}"'))
        for tenant, label in ((a, "private-a"), (b, "private-b")):
            with engine.begin() as conn:
                conn.execute(text(f'SET LOCAL ROLE "{role}"'))
                conn.execute(text("SELECT set_config('app.tenant_id', :tenant, true)"), {"tenant": tenant})
                conn.execute(text("INSERT INTO web_contents (cache_scope,content_hash,body,cache_path) "
                                  "VALUES (:scope,:hash,:body,'')"),
                             {"scope": tenant, "hash": content_hash, "body": label})
                conn.execute(text("INSERT INTO web_sources (id,cache_scope,canonical_url,fetched_url,title,"
                                  "content_hash,retrieved_at,source_type,search_queries) "
                                  "VALUES (:id,:scope,:url,:url,:title,:hash,now(),'web','[]'::json)"),
                             {"id": uuid4(), "scope": tenant, "url": canonical, "title": label, "hash": content_hash})
                conn.execute(text("INSERT INTO claims (id,cache_scope,claim_id,claim_text) "
                                  "VALUES (:id,:scope,'C1','same claim')"),
                             {"id": uuid4(), "scope": tenant})
                conn.execute(text("INSERT INTO web_searches (id,cache_scope,claim_record_id,query,"
                                  "elapsed_ms,cache_hit,source_urls) "
                                  "SELECT :id,:scope,id,:query,1,0,'[]'::json FROM claims "
                                  "WHERE claim_id='C1' AND cache_scope=:scope"),
                             {"id": uuid4(), "scope": tenant, "query": label})
        with engine.begin() as conn:
            conn.execute(text(f'SET LOCAL ROLE "{role}"'))
            assert conn.scalar(text("SELECT count(*) FROM web_contents WHERE content_hash=:h"), {"h": content_hash}) == 0
            conn.execute(text("SELECT set_config('app.tenant_id', :tenant, true)"), {"tenant": a})
            assert conn.scalar(text("SELECT body FROM web_contents WHERE content_hash=:h"), {"h": content_hash}) == "private-a"
            assert conn.scalar(text("SELECT title FROM web_sources WHERE canonical_url=:u"), {"u": canonical}) == "private-a"
            assert conn.scalar(text("SELECT query FROM web_searches WHERE query LIKE 'private-%'")) == "private-a"
            assert conn.execute(text("UPDATE web_contents SET body='tampered' WHERE cache_scope=:b"), {"b": b}).rowcount == 0
            assert conn.execute(text("DELETE FROM claims WHERE cache_scope=:b"), {"b": b}).rowcount == 0
            with pytest.raises(DBAPIError), conn.begin_nested():
                conn.execute(text("INSERT INTO web_contents(cache_scope,content_hash,body,cache_path) "
                                  "VALUES (:b,:h,'denied','')"), {"b": b, "h": "e" * 64})
        with engine.begin() as conn:
            conn.execute(text(f'SET LOCAL ROLE "{role}"'))
            conn.execute(text("SELECT set_config('app.tenant_id', :tenant, true)"), {"tenant": b})
            assert conn.scalar(text("SELECT body FROM web_contents WHERE content_hash=:h"), {"h": content_hash}) == "private-b"
    finally:
        with engine.begin() as conn:
            for table in ("web_searches", "web_sources", "claims", "web_contents"):
                conn.execute(text(f"DELETE FROM {table} WHERE cache_scope IN (:a,:b)"), {"a": a, "b": b})
            conn.execute(text(f'DROP OWNED BY "{role}"'))
            conn.execute(text(f'DROP ROLE IF EXISTS "{role}"'))
        engine.dispose()
