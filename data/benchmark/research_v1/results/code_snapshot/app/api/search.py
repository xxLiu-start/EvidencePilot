from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.schemas import ChunkResponse, SearchHitResponse, SearchRequest, SearchResponse
from app.db.session import get_db
from app.retrieval.dense import search_chunks
from app.retrieval.embedding import EmbeddingError
from app.retrieval.bm25 import get_bm25_store
from app.retrieval.hybrid import hybrid_retrieve
from app.retrieval.reranker import RerankerError
from app.db.models import bump_index_revision


router = APIRouter()


@router.post("/search", response_model=SearchResponse)
def search(request: SearchRequest, db: Annotated[Session, Depends(get_db)]) -> SearchResponse:
    try:
        if request.method == "dense":
            hits = search_chunks(db, request.query, top_k=request.top_k, document_id=request.document_id)
        elif request.method == "bm25":
            hits = get_bm25_store(db.get_bind()).retrieve(
                db, request.query, top_k=request.top_k, document_id=request.document_id
            )
        else:
            hits = hybrid_retrieve(
                db, request.query, top_k=request.top_k, document_id=request.document_id,
                candidate_k=request.candidate_k, rrf_k=request.rrf_k,
                use_reranker=request.method == "hybrid_rerank",
            )
    except EmbeddingError:
        raise HTTPException(status_code=503, detail="Embedding service unavailable") from None
    except RerankerError:
        raise HTTPException(status_code=503, detail="Reranker service unavailable") from None
    return SearchResponse(
        query=request.query,
        top_k=request.top_k,
        method=request.method,
        score_type={"dense": "cosine_similarity", "bm25": "bm25",
                    "hybrid": "rrf", "hybrid_rerank": "reranker_sigmoid"}[request.method],
        results=[
            SearchHitResponse(
                **ChunkResponse.model_validate(hit.chunk).model_dump(), score=hit.score,
                dense_score=hit.dense_score, bm25_score=hit.bm25_score,
                rrf_score=hit.rrf_score, reranker_score=hit.reranker_score,
            )
            for hit in hits
        ],
    )


@router.post("/search/rebuild-bm25")
def rebuild_bm25(db: Annotated[Session, Depends(get_db)]):
    # Notify all API workers, including when changes came from direct SQL.
    try:
        bump_index_revision(db.connection())
        db.commit()
    except Exception:
        db.rollback()
        raise
    return {"indexed_chunks": get_bm25_store(db.get_bind()).rebuild(db)}
