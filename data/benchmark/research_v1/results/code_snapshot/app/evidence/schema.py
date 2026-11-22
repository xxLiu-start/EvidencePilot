from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from app.search.parser import normalize_url
from app.verification.scope import EvidenceScope


class Evidence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    evidence_id: UUID
    claim_id: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
    claim_text: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
    source_type: Literal["web", "document"]
    source_url: str | None = None
    document_id: UUID | None = None
    chunk_id: UUID | None = None
    published_at: datetime | None = None
    retrieved_at: datetime
    supporting_text: Annotated[str, StringConstraints(min_length=12, max_length=3000)]
    relation: Literal["supports", "refutes", "context"]
    scope: EvidenceScope | None = None
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    quote_start: int = Field(ge=0)
    quote_end: int = Field(gt=0)

    @model_validator(mode="after")
    def validate_provenance(self):
        if self.source_type == "web" and (not self.source_url or normalize_url(self.source_url) != self.source_url):
            raise ValueError("Web evidence requires a normalized source URL")
        if self.source_type == "document" and self.document_id is None:
            raise ValueError("Document evidence requires document_id")
        if self.quote_end - self.quote_start != len(self.supporting_text):
            raise ValueError("Quote offsets do not match the supporting text")
        return self
