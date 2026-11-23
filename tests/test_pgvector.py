"""Optional real PostgreSQL checks; every test uses a disposable schema."""

import os
import math
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import StatementError
from sqlalchemy.orm import Session

from app.db.models import DocumentChunk, DocumentRecord
from app.db.repository import backfill_embeddings, ingest_document
from app.db.schema import initialize_database
from app.db.session import Base
from app.retrieval.dense import search_chunks


@pytest.fixture
def postgres():
    url = os.environ.get("EVIDENCEPILOT_TEST_POSTGRES_URL")
    if not url:
        pytest.skip("Set EVIDENCEPILOT_TEST_POSTGRES_URL for pgvector integration tests")
    schema = "ep_test_" + uuid4().hex
    admin = create_engine(url)
    scoped = None
    try:
        with admin.begin() as connection:
            connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public"))
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        scoped = create_engine(url, connect_args={"options": f"-csearch_path={schema},public"})
        # Explicit creation prevents reflection from finding a table in public.
        with scoped.begin() as connection:
            Base.metadata.create_all(connection, checkfirst=False)
        initialize_database(scoped)
        yield scoped
    finally:
        if scoped is not None:
            scoped.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        admin.dispose()


def test_real_pgvector_storage_cosine_and_hnsw(postgres, tmp_path, fake_embedder):
    path = tmp_path / "guide.md"
    path.write_text("# Guide\n\n中文文档入库验证。", encoding="utf-8")
    with Session(postgres) as db:
        document = ingest_document(db, str(path), version="v2")
        db.expire_all()
        chunk = db.scalar(select(DocumentChunk).where(DocumentChunk.document_id == document.id))
        assert len(chunk.embedding) == 1024
        assert chunk.embedding_model == "BAAI/bge-m3"
        assert chunk.section == "Guide" and chunk.version == "v2"
        distance = db.scalar(select(DocumentChunk.embedding.cosine_distance(chunk.embedding)))
        assert distance == pytest.approx(0, abs=1e-6)
        assert db.scalar(text("SELECT vector_dims(embedding) FROM document_chunks")) == 1024
        index = db.scalar(text(
            "SELECT indexdef FROM pg_indexes "
            "WHERE schemaname = current_schema() AND indexname = 'ix_document_chunks_embedding_hnsw'"
        ))
        assert "USING hnsw" in index and "vector_cosine_ops" in index


def test_existing_table_upgrade_and_backfill_preserve_rows(postgres, tmp_path, fake_embedder):
    path = tmp_path / "guide.txt"
    path.write_text("legacy content", encoding="utf-8")
    with Session(postgres) as db:
        document = ingest_document(db, str(path), source="legacy", version="v1")
        document_id = document.id
    with postgres.begin() as connection:
        connection.execute(text("ALTER TABLE document_chunks DROP COLUMN embedding"))
        connection.execute(text("ALTER TABLE document_chunks DROP COLUMN embedding_model"))
    initialize_database(postgres)
    initialize_database(postgres)
    with Session(postgres) as db:
        chunk = db.scalar(select(DocumentChunk))
        assert chunk.embedding is None
        assert (chunk.document_id, chunk.text, chunk.source, chunk.version) == (
            document_id, "legacy content", "legacy", "v1"
        )
        assert backfill_embeddings(db) == 1
        assert backfill_embeddings(db) == 0
        assert len(chunk.embedding) == 1024
        assert db.get(DocumentRecord, document_id).chunk_count == 1


def test_pgvector_enforces_dimension(postgres, tmp_path, fake_embedder):
    path = tmp_path / "guide.txt"
    path.write_text("body", encoding="utf-8")
    with Session(postgres) as db:
        ingest_document(db, str(path))
        chunk = db.scalar(select(DocumentChunk))
        chunk.embedding = [1.0, 2.0, 3.0]
        with pytest.raises(StatementError):
            db.flush()
        db.rollback()
        assert len(db.scalar(select(DocumentChunk)).embedding) == 1024


def test_top_five_ranking_filters_and_reconnect(postgres, fake_embedder):
    with Session(postgres) as db:
        first = DocumentRecord(filename="a.md", source="a.md", version="1", chunk_count=7)
        second = DocumentRecord(filename="b.md", source="b.md", version="1", chunk_count=1)
        db.add_all([first, second])
        db.flush()
        first_id = first.id
        for i, score in enumerate([0.9, 0.8, 0.7, 0.6, 0.5, 0.4, 0.3, 0.99]):
            document = first if i < 7 else second
            db.add(DocumentChunk(
                document_id=document.id, chunk_index=i, text=f"candidate {i}", token_count=3,
                source=document.source, version="1", embedding_model="BAAI/bge-m3",
                embedding=[score, math.sqrt(1 - score * score)] + [0.0] * 1022,
            ))
        db.add_all([
            DocumentChunk(document_id=first.id, chunk_index=20, text="legacy", token_count=1,
                          source="a.md", version="1"),
            DocumentChunk(document_id=first.id, chunk_index=21, text="wrong model", token_count=2,
                          source="a.md", version="1", embedding=[1.0] + [0.0] * 1023,
                          embedding_model="other-model"),
        ])
        db.commit()
        hits = search_chunks(db, "query")
        assert [hit.chunk.text for hit in hits] == [
            "candidate 7", "candidate 0", "candidate 1", "candidate 2", "candidate 3"
        ]
        assert [hit.score for hit in hits] == pytest.approx([0.99, 0.9, 0.8, 0.7, 0.6])
        original_ids = [hit.chunk.id for hit in hits]
        filtered = search_chunks(db, "query", document_id=first_id)
        assert len(filtered) == 5 and all(hit.chunk.document_id == first_id for hit in filtered)
        assert len(search_chunks(db, "query", top_k=100)) == 8
        assert search_chunks(db, "query", document_id=uuid4()) == []
    postgres.dispose()
    initialize_database(postgres)
    with Session(postgres) as db:
        assert [hit.chunk.id for hit in search_chunks(db, "query")] == original_ids


def test_empty_search_does_not_load_model(postgres, fake_embedder):
    with Session(postgres) as db:
        assert search_chunks(db, "query") == []
    assert fake_embedder.calls == []



def test_postgres_rls_blocks_cross_tenant_raw_sql(postgres):
    """Exercise RLS as a non-owner role so table-owner bypass cannot hide policy bugs."""
    from uuid import uuid4

    from app.db.models import Project, ResearchTask, Tenant

    role = "ep_rls_" + uuid4().hex[:12]
    tenant_a_id, tenant_b_id = uuid4(), uuid4()
    project_a_id, project_b_id = uuid4(), uuid4()
    with postgres.begin() as connection:
        connection.execute(text(
            "INSERT INTO tenants (id, slug, name, is_active, created_at) VALUES "
            "(:a, :slug_a, 'Tenant A', true, now()), (:b, :slug_b, 'Tenant B', true, now())"
        ), {"a": tenant_a_id, "b": tenant_b_id,
            "slug_a": "rls-a-" + uuid4().hex[:8], "slug_b": "rls-b-" + uuid4().hex[:8]})
        connection.execute(text(
            "INSERT INTO projects (id, tenant_id, slug, name, is_active, created_at) VALUES "
            "(:pa, :a, 'default', 'Project A', true, now()), (:pb, :b, 'default', 'Project B', true, now())"
        ), {"pa": project_a_id, "pb": project_b_id, "a": tenant_a_id, "b": tenant_b_id})
        task_a_id, task_b_id = uuid4(), uuid4()
        connection.execute(text(
            "INSERT INTO research_tasks (id, tenant_id, project_id, query, status, budget, domain_id, created_at) "
            "VALUES (:ta, :a, :pa, 'tenant-a-secret', 'queued', 2, 'general_research', now()), "
            "(:tb, :b, :pb, 'tenant-b-secret', 'queued', 2, 'general_research', now())"
        ), {"ta": task_a_id, "tb": task_b_id, "a": tenant_a_id, "b": tenant_b_id,
            "pa": project_a_id, "pb": project_b_id})
        connection.execute(text(
            "INSERT INTO agent_runs (task_id, state, updated_at, checkpoint_seq) "
            "VALUES (:ta, '{}'::json, now(), 0), (:tb, '{\"private\": true}'::json, now(), 0)"
        ), {"ta": task_a_id, "tb": task_b_id})
        evidence_hash = __import__("hashlib").sha256(b"tenant A evidence").hexdigest()
        claim_id, evidence_id = uuid4(), uuid4()
        connection.execute(text(
            "INSERT INTO claims (id, claim_id, claim_text) VALUES (:id, 'C1', 'Tenant A private claim')"
        ), {"id": claim_id})
        connection.execute(text(
            "INSERT INTO web_contents (content_hash, body, cache_path) "
            "VALUES (:hash, 'tenant A evidence', '')"
        ), {"hash": evidence_hash})
        connection.execute(text(
            "INSERT INTO evidences (id, tenant_id, project_id, claim_record_id, source_type, "
            "content_hash, retrieved_at, supporting_text, relation, scope, quote_start, quote_end) "
            "VALUES (:id, :tenant, :project, :claim, 'document', :hash, now(), "
            "'tenant A evidence', 'context', '{}'::json, 0, 18)"
        ), {"id": evidence_id, "tenant": tenant_a_id, "project": project_a_id,
            "claim": claim_id, "hash": evidence_hash})
        connection.execute(text("ALTER TABLE research_tasks ENABLE ROW LEVEL SECURITY"))
        connection.execute(text("ALTER TABLE research_tasks FORCE ROW LEVEL SECURITY"))
        connection.execute(text("DROP POLICY IF EXISTS research_tasks_tenant_isolation ON research_tasks"))
        connection.execute(text(
            "CREATE POLICY research_tasks_tenant_isolation ON research_tasks "
            "USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid) "
            "WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)"
        ))
        connection.execute(text(f'CREATE ROLE "{role}" NOLOGIN'))
        schema_name = connection.scalar(text("SELECT current_schema()"))
        connection.execute(text(f'GRANT USAGE ON SCHEMA "{schema_name}" TO "{role}"'))
        connection.execute(text("ALTER TABLE evidences ENABLE ROW LEVEL SECURITY"))
        connection.execute(text("ALTER TABLE evidences FORCE ROW LEVEL SECURITY"))
        connection.execute(text(
            "CREATE POLICY evidences_tenant_isolation ON evidences "
            "USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid) "
            "WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)"
        ))
        connection.execute(text(f'GRANT SELECT ON evidences TO "{role}"'))
        connection.execute(text("ALTER TABLE agent_runs ENABLE ROW LEVEL SECURITY"))
        connection.execute(text("ALTER TABLE agent_runs FORCE ROW LEVEL SECURITY"))
        connection.execute(text(
            "CREATE POLICY agent_runs_tenant_isolation ON agent_runs "
            "USING (EXISTS (SELECT 1 FROM research_tasks t WHERE t.id = agent_runs.task_id "
            "AND t.tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)) "
            "WITH CHECK (EXISTS (SELECT 1 FROM research_tasks t WHERE t.id = agent_runs.task_id "
            "AND t.tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid))"
        ))
        connection.execute(text(f'GRANT SELECT ON agent_runs TO "{role}"'))
        connection.execute(text(f'GRANT SELECT, INSERT ON research_tasks TO "{role}"'))

    try:
        with postgres.connect() as connection:
            with connection.begin():
                connection.execute(text(f'SET LOCAL ROLE "{role}"'))
                connection.execute(text("SELECT set_config('app.tenant_id', :tenant, true)"),
                                   {"tenant": str(tenant_a_id)})
                visible = connection.execute(text(
                    "SELECT query FROM research_tasks ORDER BY query"
                )).scalars().all()
                assert visible == ["tenant-a-secret"]
                assert connection.execute(text("SELECT COUNT(*) FROM agent_runs")).scalar_one() == 1
                assert connection.execute(text("SELECT supporting_text FROM evidences")).scalars().all() == ["tenant A evidence"]
                connection.execute(text("SELECT set_config('app.tenant_id', '', true)"))
                assert connection.execute(text("SELECT COUNT(*) FROM research_tasks")).scalar_one() == 0

        # Verify the SQLAlchemy Session hook binds tenant context transaction-locally.
        with Session(postgres) as scoped:
            scoped.info["tenant_id"] = tenant_a_id
            scoped.execute(text(f'SET LOCAL ROLE "{role}"'))
            assert scoped.execute(text("SELECT query FROM research_tasks ORDER BY query")).scalars().all() == ["tenant-a-secret"]

        # WITH CHECK must reject a cross-tenant insert even when raw SQL bypasses ORM guards.
        from sqlalchemy.exc import DBAPIError
        with Session(postgres) as scoped:
            scoped.info["tenant_id"] = tenant_a_id
            scoped.execute(text(f'SET LOCAL ROLE "{role}"'))
            with pytest.raises(DBAPIError):
                scoped.execute(text(
                    "INSERT INTO research_tasks (id, tenant_id, project_id, query, status, budget, domain_id, created_at) "
                    "VALUES (:id, :tenant, :project, 'forbidden-cross-tenant', 'queued', 2, 'general_research', now())"
                ), {"id": uuid4(), "tenant": tenant_b_id, "project": project_b_id})
            scoped.rollback()
    finally:
        with postgres.begin() as connection:
            connection.execute(text(f'DROP OWNED BY "{role}"'))
            connection.execute(text(f'DROP ROLE IF EXISTS "{role}"'))
