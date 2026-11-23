"""Real PostgreSQL RLS penetration tests.

Run after migrations and deploy/postgres/grant_runtime.sql with the dedicated
non-owner runtime and outbox-dispatcher roles.
"""
from concurrent.futures import ThreadPoolExecutor
import os
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session
from sqlalchemy.pool import QueuePool

from app.db.models import (
    AgentRun,
    AgentStep,
    ClaimRecord,
    DocumentChunk,
    DocumentRecord,
    EvidenceRecord,
    OutboxEvent,
    Project,
    ProjectMembership,
    ResearchTask,
    Tenant,
    TenantMembership,
    User,
    WebContent,
)
from app.db.rls import set_session_scope
from app.db.session import Base, load_task_with_scope


OWNER_URL = os.getenv("EVIDENCEPILOT_TEST_POSTGRES_URL")
APP_URL = os.getenv("EVIDENCEPILOT_TEST_POSTGRES_APP_URL")
DISPATCHER_URL = os.getenv("EVIDENCEPILOT_TEST_POSTGRES_DISPATCHER_URL")
pytestmark = pytest.mark.skipif(
    not OWNER_URL or not APP_URL or not DISPATCHER_URL,
    reason="Dedicated PostgreSQL owner/app/dispatcher URLs are required",
)


@pytest.fixture(scope="module")
def rls_database():
    owner = create_engine(OWNER_URL, pool_pre_ping=True)
    app_engine = create_engine(APP_URL, poolclass=QueuePool, pool_size=4, max_overflow=0,
                               pool_pre_ping=True)
    dispatcher = create_engine(DISPATCHER_URL, pool_pre_ping=True)

    suffix = uuid4().hex[:10]
    tenant_a = Tenant(slug=f"rls-a-{suffix}", name="RLS Tenant A", external_id=f"rls-a-{suffix}")
    tenant_b = Tenant(slug=f"rls-b-{suffix}", name="RLS Tenant B", external_id=f"rls-b-{suffix}")
    user_a = User(issuer="https://rls.example.test", subject=f"user-a-{suffix}", email="a@example.test")
    user_b = User(issuer="https://rls.example.test", subject=f"user-b-{suffix}", email="b@example.test")
    project_a = Project(tenant_id=tenant_a.id, slug=f"project-a-{suffix}", name="Project A")
    project_b = Project(tenant_id=tenant_b.id, slug=f"project-b-{suffix}", name="Project B")
    task_a = ResearchTask(tenant_id=tenant_a.id, project_id=project_a.id, created_by_user_id=user_a.id,
                          query="private tenant A query", budget=2)
    task_b = ResearchTask(tenant_id=tenant_b.id, project_id=project_b.id, created_by_user_id=user_b.id,
                          query="private tenant B query", budget=2)
    document_a = DocumentRecord(tenant_id=tenant_a.id, project_id=project_a.id, created_by_user_id=user_a.id,
                                filename="a.txt", source="a.txt", version="1", chunk_count=1)
    document_b = DocumentRecord(tenant_id=tenant_b.id, project_id=project_b.id, created_by_user_id=user_b.id,
                                filename="b.txt", source="b.txt", version="1", chunk_count=1)

    # IDs are generated before flush and can safely be used by dependent records.
    chunk_a = DocumentChunk(tenant_id=tenant_a.id, project_id=project_a.id, document_id=document_a.id,
                            chunk_index=0, text="tenant A confidential evidence passage", token_count=6,
                            source="a.txt", version="1", embedding=None)
    chunk_b = DocumentChunk(tenant_id=tenant_b.id, project_id=project_b.id, document_id=document_b.id,
                            chunk_index=0, text="tenant B confidential evidence passage", token_count=6,
                            source="b.txt", version="1", embedding=None)
    claim_a = ClaimRecord(tenant_id=tenant_a.id, project_id=project_a.id,
                          claim_id="C1", claim_text="Tenant A claim")
    claim_b = ClaimRecord(tenant_id=tenant_b.id, project_id=project_b.id,
                          claim_id="C1", claim_text="Tenant B claim")
    content_a = WebContent(content_hash=("a" * 64), body="tenant A immutable snapshot", cache_path="")
    content_b = WebContent(content_hash=("b" * 64), body="tenant B immutable snapshot", cache_path="")
    evidence_a = EvidenceRecord(
        id=uuid4(), tenant_id=tenant_a.id, project_id=project_a.id, claim_record_id=claim_a.id,
        source_type="document", content_hash=content_a.content_hash, document_id=document_a.id,
        chunk_id=chunk_a.id, retrieved_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
        supporting_text="tenant A confidential evidence passage", relation="context",
        scope={}, quote_start=0, quote_end=37,
    )
    evidence_b = EvidenceRecord(
        id=uuid4(), tenant_id=tenant_b.id, project_id=project_b.id, claim_record_id=claim_b.id,
        source_type="document", content_hash=content_b.content_hash, document_id=document_b.id,
        chunk_id=chunk_b.id, retrieved_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
        supporting_text="tenant B confidential evidence passage", relation="context",
        scope={}, quote_start=0, quote_end=37,
    )
    run_a = AgentRun(task_id=task_a.id, state={"status": "queued"}, updated_at=task_a.created_at)
    run_b = AgentRun(task_id=task_b.id, state={"status": "queued"}, updated_at=task_b.created_at)
    step_a = AgentStep(task_id=task_a.id, step_index=1, tool_name="rag_retrieve_tool",
                       arguments={}, status="success", elapsed_ms=1, created_at=task_a.created_at)
    step_b = AgentStep(task_id=task_b.id, step_index=1, tool_name="rag_retrieve_tool",
                       arguments={}, status="success", elapsed_ms=1, created_at=task_b.created_at)
    outbox_a = OutboxEvent(event_type="research_task.created", aggregate_id=task_a.id,
                           payload={"task_id": str(task_a.id)}, status="pending")
    outbox_b = OutboxEvent(event_type="research_task.created", aggregate_id=task_b.id,
                           payload={"task_id": str(task_b.id)}, status="pending")

    with Session(owner) as db:
        db.add_all([tenant_a, tenant_b, user_a, user_b])
        db.flush()
        db.add_all([project_a, project_b])
        db.flush()
        db.add_all([
            TenantMembership(tenant_id=tenant_a.id, user_id=user_a.id, role="RESEARCHER"),
            TenantMembership(tenant_id=tenant_b.id, user_id=user_b.id, role="RESEARCHER"),
            ProjectMembership(project_id=project_a.id, user_id=user_a.id, role="RESEARCHER"),
            ProjectMembership(project_id=project_b.id, user_id=user_b.id, role="RESEARCHER"),
            task_a, task_b, document_a, document_b, claim_a, claim_b, content_a, content_b,
        ])
        db.flush()
        db.add_all([chunk_a, chunk_b, evidence_a, evidence_b, run_a, run_b, step_a, step_b, outbox_a, outbox_b])
        db.commit()

    data = {
        "tenant_a": tenant_a.id, "tenant_b": tenant_b.id,
        "project_a": project_a.id, "project_b": project_b.id,
        "user_a": user_a.id, "user_b": user_b.id,
        "task_a": task_a.id, "task_b": task_b.id,
        "evidence_a": evidence_a.id, "evidence_b": evidence_b.id,
        "query_a": task_a.query, "query_b": task_b.query,
    }
    try:
        yield {"owner": owner, "app": app_engine, "dispatcher": dispatcher, "data": data}
    finally:
        with Session(owner) as db:
            db.execute(text("DELETE FROM outbox_events WHERE aggregate_id IN (:a, :b)"),
                       {"a": str(task_a.id), "b": str(task_b.id)})
            db.execute(text("DELETE FROM evidences WHERE id IN (:a, :b)"),
                       {"a": str(evidence_a.id), "b": str(evidence_b.id)})
            db.execute(text("DELETE FROM tool_execution_receipts WHERE step_id IN (:a, :b)"),
                       {"a": str(step_a.id), "b": str(step_b.id)})
            db.execute(text("DELETE FROM agent_steps WHERE task_id IN (:a, :b)"),
                       {"a": str(task_a.id), "b": str(task_b.id)})
            db.execute(text("DELETE FROM agent_runs WHERE task_id IN (:a, :b)"),
                       {"a": str(task_a.id), "b": str(task_b.id)})
            db.execute(text("DELETE FROM document_chunks WHERE document_id IN (:a, :b)"),
                       {"a": str(document_a.id), "b": str(document_b.id)})
            db.execute(text("DELETE FROM documents WHERE id IN (:a, :b)"),
                       {"a": str(document_a.id), "b": str(document_b.id)})
            db.execute(text("DELETE FROM claims WHERE id IN (:a, :b)"),
                       {"a": str(claim_a.id), "b": str(claim_b.id)})
            db.execute(text("DELETE FROM web_contents WHERE content_hash IN (:a, :b)"),
                       {"a": content_a.content_hash, "b": content_b.content_hash})
            db.execute(text("DELETE FROM research_tasks WHERE id IN (:a, :b)"),
                       {"a": str(task_a.id), "b": str(task_b.id)})
            db.execute(text("DELETE FROM project_memberships WHERE project_id IN (:a, :b)"),
                       {"a": str(project_a.id), "b": str(project_b.id)})
            db.execute(text("DELETE FROM tenant_memberships WHERE tenant_id IN (:a, :b)"),
                       {"a": str(tenant_a.id), "b": str(tenant_b.id)})
            db.execute(text("DELETE FROM projects WHERE id IN (:a, :b)"),
                       {"a": str(project_a.id), "b": str(project_b.id)})
            db.execute(text("DELETE FROM users WHERE id IN (:a, :b)"),
                       {"a": str(user_a.id), "b": str(user_b.id)})
            db.execute(text("DELETE FROM tenants WHERE id IN (:a, :b)"),
                       {"a": str(tenant_a.id), "b": str(tenant_b.id)})
            db.commit()
        owner.dispose()
        app_engine.dispose()
        dispatcher.dispose()


def _scope(db, data, suffix="a"):
    set_session_scope(
        db,
        tenant_id=data[f"tenant_{suffix}"],
        user_id=data[f"user_{suffix}"],
        project_ids=(data[f"project_{suffix}"],),
        selected_project_id=data[f"project_{suffix}"],
        tenant_role="RESEARCHER",
    )


def test_rls_filters_raw_sql_and_evidence_without_orm_helpers(rls_database):
    data, engine = rls_database["data"], rls_database["app"]
    with Session(engine) as db:
        _scope(db, data, "a")
        tasks = db.execute(text("SELECT query FROM research_tasks ORDER BY query")).scalars().all()
        assert tasks == [data["query_a"]]
        evidence = db.execute(text("SELECT supporting_text FROM evidences")).scalars().all()
        assert evidence == ["tenant A confidential evidence passage"]
        assert db.execute(text("SELECT claim_text FROM claims")).scalars().all() == ["Tenant A claim"]
        assert db.execute(text("SELECT query FROM web_searches")).scalars().all() == []


def test_rls_rejects_cross_tenant_raw_sql_writes(rls_database):
    data, engine = rls_database["data"], rls_database["app"]
    with Session(engine) as db:
        _scope(db, data, "a")
        with pytest.raises(DBAPIError):
            db.execute(text("""
                INSERT INTO research_tasks
                    (id, tenant_id, project_id, created_by_user_id, domain_id, query, status, budget, created_at)
                VALUES
                    (:id, :tenant_id, :project_id, :user_id, 'general_research', 'forged cross-tenant write',
                     'queued', 2, now())
            """), {
                "id": str(uuid4()), "tenant_id": str(data["tenant_b"]),
                "project_id": str(data["project_b"]), "user_id": str(data["user_b"]),
            })
        db.rollback()


def test_worker_bootstrap_can_read_only_one_task_then_switches_to_tenant_scope(rls_database):
    data, engine = rls_database["data"], rls_database["app"]
    with Session(engine) as db:
        task = load_task_with_scope(db, data["task_b"])
        assert task is not None and task.query == data["query_b"]
        visible = db.execute(text("SELECT query FROM research_tasks")).scalars().all()
        assert visible == [data["query_b"]]
        evidence = db.execute(text("SELECT supporting_text FROM evidences")).scalars().all()
        assert evidence == ["tenant B confidential evidence passage"]


def test_missing_scope_fails_closed_and_pool_state_never_leaks(rls_database):
    engine, data = rls_database["app"], rls_database["data"]
    with Session(engine) as db:
        assert db.execute(text("SELECT query FROM research_tasks")).scalars().all() == []
    with Session(engine) as db:
        _scope(db, data, "a")
        assert db.execute(text("SELECT query FROM research_tasks")).scalars().all() == [data["query_a"]]
        db.commit()
    with Session(engine) as db:
        assert db.execute(text("SELECT query FROM research_tasks")).scalars().all() == []
    with Session(engine) as db:
        _scope(db, data, "b")
        assert db.execute(text("SELECT query FROM research_tasks")).scalars().all() == [data["query_b"]]


def test_concurrent_tenant_sessions_and_dispatcher_least_privilege(rls_database):
    data, app_engine, dispatcher = rls_database["data"], rls_database["app"], rls_database["dispatcher"]

    def read_tenant(suffix):
        with Session(app_engine) as db:
            _scope(db, data, suffix)
            return db.execute(text("SELECT query FROM research_tasks")).scalars().all()

    with ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(read_tenant, "a")
        b = pool.submit(read_tenant, "b")
        assert a.result() == [data["query_a"]]
        assert b.result() == [data["query_b"]]

    with Session(dispatcher) as db:
        assert db.execute(text("SELECT count(*) FROM outbox_events")).scalar_one() >= 2
        with pytest.raises(DBAPIError):
            db.execute(text("SELECT query FROM research_tasks"))
        db.rollback()
