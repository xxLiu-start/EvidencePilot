from types import SimpleNamespace

import pytest

from app.retrieval.hybrid import fuse_hits, hybrid_retrieve, reciprocal_rank_fusion
from app.retrieval.types import SearchHit


def hit(key, score=0, **kwargs):
    return SearchHit(SimpleNamespace(id=key, text=key), score, **kwargs)


def test_rrf_formula_dedup_and_score_scale_independence():
    dense = [dict(id="a", score=-10000), dict(id="a", score=9999), dict(id="b", score=1)]
    lexical = [dict(id="b", score=99999), dict(id="c", score=0)]
    result = reciprocal_rank_fusion([dense, lexical])
    assert [key for key, score in result] == ["b", "a", "c"]
    assert dict(result)["b"] == pytest.approx(1 / 62 + 1 / 61)
    assert dict(result)["a"] == pytest.approx(1 / 61)
    assert reciprocal_rank_fusion([[], []]) == []
    with pytest.raises(ValueError):
        reciprocal_rank_fusion([], k=0)


def test_fusion_preserves_both_stage_scores():
    result = fuse_hits([[hit("a", .8, dense_score=.8)], [hit("a", 9, bm25_score=9)]])
    assert len(result) == 1
    assert result[0].dense_score == .8 and result[0].bm25_score == 9
    assert result[0].rrf_score == pytest.approx(2 / 61)


def test_hybrid_candidate_counts_and_reranker(monkeypatch):
    calls = []

    def dense(db, query, **kwargs):
        calls.append(kwargs)
        return [hit(str(i), .8, dense_score=.8) for i in range(10)]

    class Lexical:
        def retrieve(self, db, query, **kwargs):
            assert kwargs["top_k"] == 10
            return [hit(str(i), 9, bm25_score=9) for i in range(10, 20)]

    class Reranker:
        def rerank_hits(self, query, candidates, top_k):
            assert len(candidates) == 20
            return candidates[::-1][:top_k]

    monkeypatch.setattr("app.retrieval.hybrid.search_chunks", dense)
    output = hybrid_retrieve(None, "question", bm25_store=Lexical(), reranker=Reranker(), use_reranker=True)
    assert len(output) == 5 and calls[0]["top_k"] == 10
