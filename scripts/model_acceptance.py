"""Run actual dense embedding and reranking on reviewed local model bundles."""
from types import SimpleNamespace
import numpy as np
from app.config import settings
from app.retrieval.embedding import BGEEmbeddingService
from app.retrieval.reranker import RerankerService
from app.retrieval.model_integrity import verified_model_path

def main():
    if settings.environment != "production":
        raise RuntimeError("Model acceptance requires production model integrity policy")
    for kind in ("embedding", "reranker"):
        verified_model_path(settings, kind)
    embedding = BGEEmbeddingService(settings)
    query = "How does PostgreSQL row level security isolate tenants?"
    texts = ["PostgreSQL row level security restricts rows by tenant policies.",
             "Bananas are a tropical fruit."]
    vectors = np.asarray(embedding.embed_documents(texts))
    q = np.asarray(embedding.embed_query(query))
    assert vectors.shape == (2, 1024) and q.shape == (1024,)
    assert np.isfinite(vectors).all() and np.isfinite(q).all()
    np.testing.assert_allclose(np.linalg.norm(vectors, axis=1), 1, atol=1e-5)
    np.testing.assert_allclose(np.linalg.norm(q), 1, atol=1e-5)
    assert float(vectors[0] @ q) > float(vectors[1] @ q)
    candidates = [SimpleNamespace(id=str(i), text=t) for i, t in enumerate(texts)]
    scores = RerankerService(settings).score(query, candidates)
    assert len(scores) == 2 and all(np.isfinite(scores))
    assert 0 <= scores[1] < scores[0] <= 1
    print("PASS: verified local BGE dense embedding and reranking smoke test")

if __name__ == "__main__":
    main()
