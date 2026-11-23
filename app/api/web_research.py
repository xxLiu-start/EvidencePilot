from dataclasses import asdict
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import ClaimRecord, EvidenceRecord
from app.db.session import get_db
from app.llm_client import LLMConfigurationError, LLMError, get_llm_client
from app.search.cache import CacheMiss
from app.search.query_generator import ClaimInput, generate_queries
from app.search.research import replay_snapshot, run_web_research
from app.search.web_search import SearchError


from app.auth.oidc import require_roles

router = APIRouter()


class WebResearchRequest(BaseModel):
    claims: list[ClaimInput] = Field(min_length=1, max_length=5)
    max_queries: int = Field(default=4, ge=1, le=10)
    max_results: int = Field(default=3, ge=1, le=5)
    max_pages: int = Field(default=4, ge=1, le=10)


@router.post("/web-research/queries", dependencies=[Depends(require_roles("RESEARCHER", "PROJECT_MANAGER", "TENANT_ADMIN", "SUPER_ADMIN"))])
def create_queries(request: WebResearchRequest):
    try:
        plan, trace = generate_queries(request.claims, max_queries=request.max_queries)
        return {**plan.model_dump(), "trace": asdict(trace)}
    except (LLMError, LLMConfigurationError) as exc:
        raise HTTPException(status_code=503, detail=getattr(exc, "category", "llm_configuration")) from None
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid claim/query configuration") from None


@router.post("/web-research", dependencies=[Depends(require_roles("RESEARCHER", "PROJECT_MANAGER", "TENANT_ADMIN", "SUPER_ADMIN"))])
def web_research(request: WebResearchRequest, db: Annotated[Session, Depends(get_db)]):
    try:
        return run_web_research(db, request.claims, max_queries=request.max_queries,
                                max_results=request.max_results, max_pages=request.max_pages)
    except (LLMError, LLMConfigurationError, SearchError) as exc:
        raise HTTPException(status_code=503, detail=getattr(exc, "category", "service_configuration")) from None
    except CacheMiss:
        raise HTTPException(status_code=409, detail="Required research snapshot missing") from None
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid claim/query configuration") from None


@router.post("/web-research/{run_id}/replay", dependencies=[Depends(require_roles("RESEARCHER", "PROJECT_MANAGER", "TENANT_ADMIN", "SUPER_ADMIN"))])
def replay(run_id: UUID, db: Annotated[Session, Depends(get_db)]):
    try:
        return replay_snapshot(db, str(run_id))
    except CacheMiss:
        raise HTTPException(status_code=404, detail="Research snapshot not found") from None
    except ValueError:
        raise HTTPException(status_code=409, detail="Stored evidence provenance no longer matches") from None


@router.get("/claims/{claim_id}/evidences")
def list_evidence(claim_id: str, db: Annotated[Session, Depends(get_db)]):
    rows = db.execute(select(EvidenceRecord, ClaimRecord).join(
        ClaimRecord, EvidenceRecord.claim_record_id == ClaimRecord.id
    ).where(ClaimRecord.claim_id == claim_id).order_by(EvidenceRecord.id))
    return [{"evidence_id": row.id, "claim_id": claim.claim_id, "claim_text": claim.claim_text,
             "source_type": row.source_type, "source_url": row.source_url, "document_id": row.document_id, "chunk_id": row.chunk_id,
             "published_at": row.published_at, "retrieved_at": row.retrieved_at,
             "supporting_text": row.supporting_text, "relation": row.relation, "content_hash": row.content_hash,
             "quote_start": row.quote_start, "quote_end": row.quote_end} for row, claim in rows]


@router.post("/llm/probe", dependencies=[Depends(require_roles("TENANT_ADMIN", "SUPER_ADMIN"))])
def probe():
    try:
        plan, trace = generate_queries([ClaimInput(claim_id="PROBE", claim_text="Qwen official documentation")], max_queries=2)
        return {"queries": plan.model_dump()["queries"], "trace": asdict(trace)}
    except (LLMError, LLMConfigurationError) as exc:
        raise HTTPException(status_code=503, detail=getattr(exc, "category", "llm_configuration")) from None
