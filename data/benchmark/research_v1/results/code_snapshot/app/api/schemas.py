from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, computed_field


class ResearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4000)]
    budget: int = Field(default=6, gt=0, le=20)


class ResearchTaskResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    query: str
    status: str
    budget: int
    created_at: datetime

    @computed_field
    @property
    def task_id(self) -> UUID:
        return self.id


class FeedbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rating: int = Field(ge=1, le=5)
    comment: Annotated[str, StringConstraints(strip_whitespace=True, max_length=2000)] | None = None


class DocumentResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    filename: str
    source: str
    version: str
    chunk_count: int
    created_at: datetime


class ChunkResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

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


class SearchRequest(BaseModel):
    query: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
    top_k: int = Field(default=5, ge=1, le=100)
    document_id: UUID | None = None
    method: Literal["dense", "bm25", "hybrid", "hybrid_rerank"] = "dense"
    candidate_k: int = Field(default=10, ge=1, le=100)
    rrf_k: int = Field(default=60, gt=0)


class SearchHitResponse(ChunkResponse):
    score: float
    dense_score: float | None = None
    bm25_score: float | None = None
    rrf_score: float | None = None
    reranker_score: float | None = None


class SearchResponse(BaseModel):
    query: str
    top_k: int
    results: list[SearchHitResponse]
    method: str = "dense"
    score_type: str = "cosine_similarity"
