"""Rank-only reciprocal rank fusion and Dense + BM25 retrieval."""

from dataclasses import replace

from app.retrieval.bm25 import get_bm25_store
from app.retrieval.dense import search_chunks
from app.retrieval.types import item_id


def reciprocal_rank_fusion(result_lists, k=60):
    if k <= 0:
        raise ValueError("RRF k must be positive")
    scores = {}
    for results in result_lists:
        seen = set()
        for item in results:
            chunk_id = item_id(item)
            if chunk_id in seen:
                continue
            seen.add(chunk_id)
            scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (k + len(seen))
    return sorted(scores.items(), key=lambda item: (-item[1], item[0]))


def fuse_hits(result_lists, k=60, top_k=20):
    if top_k <= 0:
        raise ValueError("top_k must be positive")
    by_id = {}
    for results in result_lists:
        for hit in results:
            key = item_id(hit)
            if key not in by_id:
                by_id[key] = hit
            else:
                old = by_id[key]
                by_id[key] = replace(
                    old, dense_score=hit.dense_score if hit.dense_score is not None else old.dense_score,
                    bm25_score=hit.bm25_score if hit.bm25_score is not None else old.bm25_score,
                )
    return [replace(by_id[key], score=score, rrf_score=score)
            for key, score in reciprocal_rank_fusion(result_lists, k)[:top_k]]


def hybrid_retrieve(
    db, query, *, top_k=5, document_id=None, candidate_k=10, rrf_k=60,
    use_reranker=False, embedder=None, bm25_store=None, reranker=None,
):
    if not 1 <= top_k <= 100 or not 1 <= candidate_k <= 100:
        raise ValueError("top_k and candidate_k must be between 1 and 100")
    dense = search_chunks(db, query, top_k=candidate_k, document_id=document_id, embedder=embedder)
    store = bm25_store if bm25_store is not None else get_bm25_store(db.get_bind())
    lexical = store.retrieve(db, query, top_k=candidate_k, document_id=document_id)
    fused = fuse_hits([dense, lexical], k=rrf_k, top_k=2 * candidate_k)
    if use_reranker and fused:
        from app.retrieval.reranker import get_reranker_service

        service = reranker if reranker is not None else get_reranker_service()
        return service.rerank_hits(query, fused, top_k=top_k)
    return fused[:top_k]
