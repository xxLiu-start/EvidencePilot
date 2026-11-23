"""Retrieval snapshots and stage scores, independent of ORM session lifetime."""

from dataclasses import dataclass
from typing import Any
from uuid import UUID


@dataclass(frozen=True)
class ChunkSnapshot:
    id: UUID
    document_id: UUID
    chunk_index: int
    text: str
    token_count: int
    page: int | None
    section: str | None
    source: str
    version: str
    embedding_model: str | None
    embedding_dimensions: int | None
    tenant_id: UUID | None = None
    project_id: UUID | None = None


@dataclass(frozen=True)
class SearchHit:
    chunk: Any
    score: float
    dense_score: float | None = None
    bm25_score: float | None = None
    rrf_score: float | None = None
    reranker_score: float | None = None


def item_id(item) -> str:
    if isinstance(item, dict):
        return str(item["id"])
    return str(item.chunk.id if isinstance(item, SearchHit) else item.id)


def item_text(item) -> str:
    if isinstance(item, dict):
        return item.get("content", item.get("text", ""))
    return item.chunk.text if isinstance(item, SearchHit) else item.text
