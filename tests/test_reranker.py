from types import SimpleNamespace

import pytest

from app.config import Settings
from app.retrieval.reranker import RerankerError, RerankerService
from app.retrieval.types import SearchHit


def test_reranker_loads_once_batches_scalar_output_and_preserves_provenance(monkeypatch):
    loads, calls = [], []

    def compute(pairs, **kwargs):
        calls.append((pairs, kwargs))
        output = [.9 if text == "relevant" else .1 for query, text in pairs]
        return output[0] if len(output) == 1 else output

    def load(config):
        loads.append(config)
        return SimpleNamespace(compute_score=compute)

    monkeypatch.setattr("app.retrieval.reranker._load_reranker", load)
    service = RerankerService(Settings(database_url="sqlite://", reranker_batch_size=2))
    assert service.score("q", []) == [] and not loads
    candidates = [SearchHit(SimpleNamespace(id=str(i), text=text), .02, rrf_score=.02)
                  for i, text in enumerate(["irrelevant", "relevant", "other"])]
    ranked = service.rerank_hits("q", candidates)
    assert ranked[0].chunk.text == "relevant" and ranked[0].score == .9
    assert ranked[0].rrf_score == .02 and ranked[0].reranker_score == .9
    assert len(calls) == 2 and len(loads) == 1
    assert calls[0][1] == dict(batch_size=2, max_length=1024, normalize=True)


@pytest.mark.parametrize("output", [[], [float("nan")], [float("inf")], [2.0]])
def test_invalid_reranker_scores_rejected(monkeypatch, output):
    monkeypatch.setattr("app.retrieval.reranker._load_reranker", lambda config: SimpleNamespace(
        compute_score=lambda *args, **kwargs: output))
    with pytest.raises(RerankerError):
        RerankerService().score("q", [{"id": "a", "content": "text"}])


def test_reranker_failure_wrapped(monkeypatch):
    def fail(config):
        raise OSError("unavailable")

    monkeypatch.setattr("app.retrieval.reranker._load_reranker", fail)
    with pytest.raises(RerankerError):
        RerankerService().score("q", [{"id": "a", "content": "text"}])
