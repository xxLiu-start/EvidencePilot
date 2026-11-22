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
