from uuid import uuid4

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine, select, func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db.models import DocumentChunk, DocumentRecord
from app.db.repository import backfill_embeddings, ingest_document
from app.retrieval.embedding import EmbeddingError
from app.db.session import Base, get_db
from app.main import app


@pytest.fixture
def db(fake_embedder):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


@pytest.fixture
def client(db):
    app.dependency_overrides[get_db] = lambda: db
    # No lifespan: tests deliberately use isolated SQLite instead of live PostgreSQL.
    client = TestClient(app)
    yield client
    client.close()
    app.dependency_overrides.clear()


def test_upload_and_read_chunks_persists_all_metadata(client, db):
    response = client.post(
        "/api/v1/documents", files={"file": ("guide.md", "# Guide\n\n" + "证据。" * 600)},
        data={"source": "https://example.com/guide", "version": "v2"},
    )
    assert response.status_code == 201, response.text
    document = response.json()
    assert document["chunk_count"] > 1
    response = client.get(f"/api/v1/documents/{document['id']}/chunks")
    assert response.status_code == 200
    chunks = response.json()
    assert len(chunks) == document["chunk_count"]
    for index, chunk in enumerate(chunks):
        assert chunk["document_id"] == document["id"]
        assert chunk["chunk_index"] == index
        assert chunk["page"] is None
        assert chunk["section"] == "Guide"
        assert chunk["source"] == "https://example.com/guide"
        assert chunk["version"] == "v2"
        assert 0 < chunk["token_count"] <= 500
        assert chunk["embedding_model"] == "BAAI/bge-m3"
        assert chunk["embedding_dimensions"] == 1024
    assert db.scalar(select(func.count()).select_from(DocumentChunk)) == len(chunks)
    stored = list(db.scalars(select(DocumentChunk).order_by(DocumentChunk.chunk_index)))
    assert all(len(chunk.embedding) == 1024 and chunk.embedding[0] == 1.0 for chunk in stored)


def test_defaults_and_upload_filename_does_not_leak_temporary_path(client):
    response = client.post(
        "/api/v1/documents", files={"file": ("../../guide.TXT", "small document")}
    )
    assert response.status_code == 201
    document = response.json()
    assert document["filename"] == document["source"] == "guide.TXT"
    assert document["version"] == "1"


@pytest.mark.parametrize("filename,content,data,status", [
    ("bad.html", "body", {}, 415),
    ("empty.txt", " \n ", {}, 422),
    ("scan.pdf", b"invalid pdf", {}, 422),
    ("bad.docx", b"not a zip file", {}, 422),
    ("invalid.txt", b"\xff", {}, 422),
    ("guide.txt", "body", {"chunk_size": 40, "chunk_overlap": 40}, 422),
    ("guide.txt", "body", {"chunk_size": 0}, 422),
    ("guide.txt", "body", {"chunk_overlap": -1}, 422),
    ("guide.txt", "body", {"source": " "}, 422),
    ("guide.txt", "body", {"version": " "}, 422),
])
def test_invalid_uploads_do_not_save_partial_documents(client, db, filename, content, data, status):
    response = client.post("/api/v1/documents", files={"file": (filename, content)}, data=data)
    assert response.status_code == status, response.text
    assert db.scalar(select(func.count()).select_from(DocumentRecord)) == 0
    assert db.scalar(select(func.count()).select_from(DocumentChunk)) == 0


def test_missing_document(client):
    assert client.get(f"/api/v1/documents/{uuid4()}/chunks").status_code == 404
    assert client.get("/api/v1/documents/not-a-uuid/chunks").status_code == 422


def test_custom_chunk_parameters(client):
    response = client.post(
        "/api/v1/documents", files={"file": ("guide.txt", "word " * 100)},
        data={"chunk_size": 40, "chunk_overlap": 5},
    )
    assert response.status_code == 201
    chunks = client.get(f"/api/v1/documents/{response.json()['id']}/chunks").json()
    assert len(chunks) > 1
    assert all(chunk["token_count"] <= 40 for chunk in chunks)


def test_save_failure_rolls_back_document_and_chunks(db, tmp_path, monkeypatch):
    path = tmp_path / "document.txt"
    path.write_text("document body", encoding="utf-8")
    original_commit = db.commit

    def fail_commit():
        db.flush()
        raise IntegrityError("insert", {}, Exception("simulated storage failure"))

    monkeypatch.setattr(db, "commit", fail_commit)
    with pytest.raises(IntegrityError):
        ingest_document(db, str(path))
    monkeypatch.setattr(db, "commit", original_commit)
    assert db.scalar(select(func.count()).select_from(DocumentRecord)) == 0
    assert db.scalar(select(func.count()).select_from(DocumentChunk)) == 0


@pytest.mark.parametrize("failure", ["unavailable", "wrong_count", "wrong_dimension"])
def test_embedding_failure_saves_nothing(client, db, fake_embedder, monkeypatch, failure):
    def fail(texts):
        if failure == "unavailable":
            raise EmbeddingError("download failed")
        if failure == "wrong_count":
            return []
        return [[1.0, 2.0]]

    monkeypatch.setattr(fake_embedder, "embed_documents", fail)
    response = client.post("/api/v1/documents", files={"file": ("guide.txt", "body")})
    assert response.status_code == 503
    assert db.scalar(select(func.count()).select_from(DocumentRecord)) == 0
    assert db.scalar(select(func.count()).select_from(DocumentChunk)) == 0


def test_backfill_only_missing_vectors_preserves_metadata_and_is_repeatable(db, tmp_path, fake_embedder):
    path = tmp_path / "guide.txt"
    path.write_text("word " * 1200, encoding="utf-8")
    document = ingest_document(db, str(path), source="manual", version="v3")
    chunks = list(db.scalars(select(DocumentChunk).order_by(DocumentChunk.chunk_index)))
    assert len(chunks) >= 3
    chunks[0].embedding = None
    chunks[0].embedding_model = None
    chunks[1].embedding_model = None
    db.commit()
    fake_embedder.calls.clear()
    assert backfill_embeddings(db, batch_size=1) == 2
    assert len(fake_embedder.calls) == 2
    assert backfill_embeddings(db) == 0
    assert len(fake_embedder.calls) == 2
    assert all(len(c.embedding) == 1024 and c.embedding_model == "BAAI/bge-m3" for c in chunks)
    assert all(c.document_id == document.id and c.source == "manual" and c.version == "v3" for c in chunks)


def test_backfill_failure_rolls_back_current_batch(db, tmp_path, fake_embedder, monkeypatch):
    path = tmp_path / "guide.txt"
    path.write_text("body", encoding="utf-8")
    ingest_document(db, str(path))
    chunk = db.scalar(select(DocumentChunk))
    chunk.embedding = None
    chunk.embedding_model = None
    db.commit()
    monkeypatch.setattr(fake_embedder, "embed_documents", lambda texts: [])
    with pytest.raises(EmbeddingError):
        backfill_embeddings(db)
    db.refresh(chunk)
    assert chunk.embedding is None and chunk.embedding_model is None


def test_cli_directory_import_and_backfill(db, tmp_path, monkeypatch, capsys):
    import json
    import sys

    from app.retrieval import index_documents

    (tmp_path / "guide.md").write_text("# Guide\n\nCLI import", encoding="utf-8")
    (tmp_path / "ignored.html").write_text("ignored", encoding="utf-8")
    monkeypatch.setattr(index_documents, "engine", db.bind)
    monkeypatch.setattr(index_documents, "SessionLocal", lambda: Session(db.bind))
    monkeypatch.setattr(sys, "argv", ["index_documents", str(tmp_path), "--version=v4"])
    index_documents.main()
    output = json.loads(capsys.readouterr().out)
    assert output["chunk_count"] == 1
    chunk = db.scalar(select(DocumentChunk))
    assert chunk.version == "v4" and len(chunk.embedding) == 1024
    chunk.embedding = None
    chunk.embedding_model = None
    db.commit()
    monkeypatch.setattr(sys, "argv", ["index_documents", "--backfill"])
    index_documents.main()
    assert json.loads(capsys.readouterr().out) == {"embedded_chunks": 1}
    db.refresh(chunk)
    assert len(chunk.embedding) == 1024


def test_bm25_api_automatic_refresh_and_explicit_rebuild(client, db):
    first = client.post("/api/v1/search", json={"query": "newword", "method": "bm25"})
    assert first.status_code == 200 and first.json()["results"] == []
    uploaded = client.post("/api/v1/documents", files={"file": ("a.txt", "newword database")})
    assert uploaded.status_code == 201
    response = client.post("/api/v1/search", json={"query": "newword", "method": "bm25"})
    assert response.status_code == 200 and len(response.json()["results"]) == 1
    assert response.json()["score_type"] == "bm25"
    assert response.json()["results"][0]["bm25_score"] is not None
    rebuilt = client.post("/api/v1/search/rebuild-bm25")
    assert rebuilt.status_code == 200 and rebuilt.json()["indexed_chunks"] == 1



def test_upload_enforces_streamed_byte_limit(client, monkeypatch):
    from app.api.documents import settings

    monkeypatch.setattr(settings, "document_max_upload_bytes", 4)
    response = client.post("/api/v1/documents", files={"file": ("oversized.txt", b"12345")})
    assert response.status_code == 413
    assert response.json()["detail"] == "Uploaded document exceeds configured size limit"


def test_upload_enforces_configured_chunk_size_limit(client, monkeypatch):
    from app.api.documents import settings

    monkeypatch.setattr(settings, "document_max_chunk_size", 100)
    response = client.post("/api/v1/documents", files={"file": ("small.txt", b"short text")},
                           data={"chunk_size": "101", "chunk_overlap": "0"})
    assert response.status_code == 422
    assert response.json()["detail"] == "chunk_size exceeds configured maximum"
