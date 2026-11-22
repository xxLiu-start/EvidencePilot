import os
from types import SimpleNamespace

import pytest

# Test collection must work without a local .env or a PostgreSQL server.
os.environ.setdefault("DATABASE_URL", "sqlite://")

from app.retrieval.embedding import BGE_M3_DIMENSIONS


@pytest.fixture
def fake_embedder(monkeypatch):
    calls = []

    def embed(texts):
        calls.append(list(texts))
        return [[1.0] + [0.0] * (BGE_M3_DIMENSIONS - 1) for _ in texts]

    embedder = SimpleNamespace(
        model_name="BAAI/bge-m3", embed_documents=embed,
        embed_query=lambda text: embed([text])[0], calls=calls,
    )
    monkeypatch.setattr("app.db.repository.get_embedding_service", lambda: embedder)
    monkeypatch.setattr("app.retrieval.dense.get_embedding_service", lambda: embedder)
    return embedder
