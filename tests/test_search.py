from types import SimpleNamespace
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db.session import Base, get_db
from app.main import app
from app.retrieval.dense import SearchHit
from app.retrieval.embedding import EmbeddingError


@pytest.fixture
def client():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = Session(engine)
    app.dependency_overrides[get_db] = lambda: db
    test_client = TestClient(app)
    yield test_client
    test_client.close()
    db.close()
    engine.dispose()
    app.dependency_overrides.clear()


def test_search_defaults_and_metadata_response(client, monkeypatch):
    chunk = SimpleNamespace(
        id=uuid4(), document_id=uuid4(), chunk_index=0, text="pgvector search",
        token_count=3, page=2, section="Guide > Retrieval", source="guide.pdf",
        version="v2", embedding_model="BAAI/bge-m3", embedding_dimensions=1024,
    )
    calls = []

    def search(db, query, **kwargs):
        calls.append((query, kwargs))
        return [SearchHit(chunk, 0.95)]

    monkeypatch.setattr("app.api.search.search_chunks", search)
    response = client.post("/api/v1/search", json={"query": "  pgvector  "})
    assert response.status_code == 200
    result = response.json()
    assert result["top_k"] == 5 and result["query"] == "pgvector"
    assert calls == [("pgvector", {"top_k": 5, "document_id": None})]
    hit = result["results"][0]
    assert hit["score"] == 0.95 and hit["document_id"] == str(chunk.document_id)
    assert hit["page"] == 2 and hit["section"] == "Guide > Retrieval"
    assert hit["source"] == "guide.pdf" and hit["version"] == "v2"
    assert "embedding" not in hit


def test_search_filter_and_empty_response(client, monkeypatch):
    document_id = uuid4()

    def search(db, query, **kwargs):
        assert kwargs == {"top_k": 3, "document_id": document_id}
        return []

    monkeypatch.setattr("app.api.search.search_chunks", search)
    response = client.post("/api/v1/search", json={
        "query": "question", "top_k": 3, "document_id": str(document_id)
    })
    assert response.status_code == 200 and response.json()["results"] == []


@pytest.mark.parametrize("payload", [
    {}, {"query": " "}, {"query": "q", "top_k": 0},
    {"query": "q", "top_k": 101}, {"query": "q", "document_id": "invalid"},
])
def test_search_validates_request(client, payload):
    assert client.post("/api/v1/search", json=payload).status_code == 422


def test_search_model_failure_returns_503(client, monkeypatch):
    def search(*args, **kwargs):
        raise EmbeddingError("model unavailable")

    monkeypatch.setattr("app.api.search.search_chunks", search)
    assert client.post("/api/v1/search", json={"query": "question"}).status_code == 503


@pytest.mark.parametrize("method", ["hybrid", "hybrid_rerank"])
def test_hybrid_api_dispatch(client, monkeypatch, method):
    def retrieve(db, query, **kwargs):
        assert kwargs == dict(top_k=5, document_id=None, candidate_k=10, rrf_k=60,
                              use_reranker=method == "hybrid_rerank")
        return []

    monkeypatch.setattr("app.api.search.hybrid_retrieve", retrieve)
    response = client.post("/api/v1/search", json={"query": "q", "method": method})
    assert response.status_code == 200 and response.json()["method"] == method


def test_reranker_failure_does_not_silently_fall_back(client, monkeypatch):
    from app.retrieval.reranker import RerankerError

    def fail(*args, **kwargs):
        raise RerankerError("unavailable")

    monkeypatch.setattr("app.api.search.hybrid_retrieve", fail)
    assert client.post("/api/v1/search", json={"query": "q", "method": "hybrid_rerank"}).status_code == 503
