"""Persist a parsed document and all of its chunks in a single transaction."""

from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.db.models import DocumentChunk, DocumentRecord
from app.retrieval.chunking import DEFAULT_CHUNK_OVERLAP, DEFAULT_CHUNK_SIZE
from app.retrieval.ingestion import chunk_document
from app.retrieval.embedding import (
    BGEEmbeddingService, get_embedding_service, validate_embeddings,
)


def ingest_document(
    db: Session,
    path: str,
    *,
    filename: str | None = None,
    source: str | None = None,
    version: str = "1",
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    chunk_overlap: int = DEFAULT_CHUNK_OVERLAP,
    embedder: BGEEmbeddingService | None = None,
) -> DocumentRecord:
    document_id = uuid4()
    source = source if source is not None else str(path)
    chunks = chunk_document(
        path, document_id=str(document_id), source=source, version=version,
        chunk_size=chunk_size, chunk_overlap=chunk_overlap,
    )
    if not chunks:
        raise ValueError("Document contains no extractable text")
    embedder = embedder if embedder is not None else get_embedding_service()
    vectors = validate_embeddings(
        embedder.embed_documents([chunk.text for chunk in chunks]), len(chunks)
    )
    document = DocumentRecord(
        id=document_id, filename=filename or Path(path).name,
        source=source, version=version, chunk_count=len(chunks),
    )
    try:
        db.add(document)
        db.flush()
        db.add_all([
            DocumentChunk(
                document_id=document_id, chunk_index=chunk.chunk_index,
                text=chunk.text, token_count=chunk.token_count,
                page=chunk.page, section=chunk.section,
                source=chunk.source, version=chunk.version,
                embedding=vector, embedding_model=embedder.model_name,
            )
            for chunk, vector in zip(chunks, vectors, strict=True)
        ])
        db.commit()
    except Exception:
        db.rollback()
        raise
    db.refresh(document)
    return document


def backfill_embeddings(
    db: Session,
    *,
    embedder: BGEEmbeddingService | None = None,
    batch_size: int = 16,
) -> int:
    """Fill legacy chunks in atomic batches; reruns skip completed rows."""
    if batch_size <= 0:
        raise ValueError("batch_size must be positive")
    embedder = embedder if embedder is not None else get_embedding_service()
    updated = 0
    while True:
        try:
            chunks = list(db.scalars(
                select(DocumentChunk)
                .where(or_(
                    DocumentChunk.embedding.is_(None), DocumentChunk.embedding_model.is_(None)
                ))
                .order_by(DocumentChunk.document_id, DocumentChunk.chunk_index)
                .limit(batch_size)
            ))
            if not chunks:
                return updated
            vectors = validate_embeddings(
                embedder.embed_documents([chunk.text for chunk in chunks]), len(chunks)
            )
            for chunk, vector in zip(chunks, vectors, strict=True):
                chunk.embedding = vector
                chunk.embedding_model = embedder.model_name
            db.commit()
            updated += len(chunks)
        except Exception:
            db.rollback()
            raise


def list_document_chunks(db: Session, document_id: UUID) -> list[DocumentChunk]:
    return list(db.scalars(
        select(DocumentChunk)
        .where(DocumentChunk.document_id == document_id)
        .order_by(DocumentChunk.chunk_index)
    ))
