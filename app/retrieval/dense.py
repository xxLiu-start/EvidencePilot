"""Search persisted chunk vectors with PostgreSQL cosine distance."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import DocumentChunk
from app.retrieval.embedding import (
    BGEEmbeddingService, get_embedding_service, validate_embeddings,
)
from app.retrieval.types import SearchHit


def search_chunks(
    db: Session,
    query: str,
    *,
    top_k: int = 5,
    document_id: UUID | None = None,
    embedder: BGEEmbeddingService | None = None,
) -> list[SearchHit]:
    if not query.strip():
        raise ValueError("query must be non-empty")
    if not 1 <= top_k <= 100:
        raise ValueError("top_k must be between 1 and 100")
    embedder = embedder if embedder is not None else get_embedding_service()
    conditions = [
        DocumentChunk.embedding.is_not(None),
        DocumentChunk.embedding_model == embedder.model_name,
    ]
    if document_id is not None:
        conditions.append(DocumentChunk.document_id == document_id)
    # An empty corpus needs no model download or inference.
    if db.scalar(select(DocumentChunk.id).where(*conditions).limit(1)) is None:
        return []
    vector = validate_embeddings([embedder.embed_query(query.strip())], 1)[0]
    distance = DocumentChunk.embedding.cosine_distance(vector).label("distance")
    rows = db.execute(
        select(DocumentChunk, distance)
        .where(*conditions)
        .order_by(distance, DocumentChunk.id)
        .limit(top_k)
    )
    hits = []
    for chunk, value in rows:
        score = max(-1.0, min(1.0, 1.0 - float(value)))
        hits.append(SearchHit(chunk=chunk, score=score, dense_score=score))
    return hits
