"""Task-bound evidence inspection. Reading an original never fetches a URL."""
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.tasks import require_task
from app.db.session import cache_scope, get_cached_content
from app.db.models import AgentRun, ClaimRecord, DocumentChunk, DocumentRecord, EvidenceRecord, WebContent, WebSource
from app.db.session import get_db
from app.search.cache import digest
from app.verification.verifier import load_claim_evidence

router = APIRouter()


def task_evidences(db, task_id):
    require_task(db, task_id)
    run = db.get(AgentRun, task_id)
    state = run.state if run else {}
    ids = [UUID(value) for value in state.get("evidence_ids", [])]
    claims = {claim["id"]: claim for claim in state.get("claims", [])}
    if not ids:
        return state, []
    rows = db.execute(select(EvidenceRecord, ClaimRecord).join(
        ClaimRecord, EvidenceRecord.claim_record_id == ClaimRecord.id
    ).where(EvidenceRecord.id.in_(ids)).order_by(EvidenceRecord.id))
    # C1 is local to a plan; matching only C1 could leak another task's evidence.
    return state, [(row, claim) for row, claim in rows
        if claim.claim_id in claims and claim.claim_text == claims[claim.claim_id]["claim"]]


def serialize(db, row, claim, *, include_source=False):
    valid = bool(load_claim_evidence(db, {"id": claim.claim_id, "claim": claim.claim_text}, [str(row.id)]))
    chunk = db.get(DocumentChunk, row.chunk_id) if row.chunk_id else None
    document = db.get(DocumentRecord, row.document_id) if row.document_id else None
    value = {"evidence_id": row.id, "claim_id": claim.claim_id, "claim_text": claim.claim_text,
        "source_type": row.source_type, "source_url": row.source_url, "document_id": row.document_id,
        "chunk_id": row.chunk_id, "published_at": row.published_at, "retrieved_at": row.retrieved_at,
        "supporting_text": row.supporting_text, "relation": row.relation, "scope": row.scope,
        "content_hash": row.content_hash, "quote_start": row.quote_start, "quote_end": row.quote_end,
        "provenance_valid": valid, "filename": document.filename if document else None,
        "page": chunk.page if chunk else None, "section": chunk.section if chunk else None,
        "version": chunk.version if chunk else None}
    if include_source:
        content = get_cached_content(db, row.content_hash)
        source = db.scalar(select(WebSource).where(WebSource.canonical_url == row.source_url)) if row.source_url else None
        intact = bool(content and digest(content.body) == row.content_hash)
        value["original"] = {"kind": "cached_webpage" if row.source_type == "web" else "document_chunk",
            "title": source.title if source else document.filename if document else None,
            "body": content.body if intact else None, "integrity_valid": intact,
            "note": "上传文件保留解析后的 Chunk；原始二进制文件仅在上传解析期间临时保存。" if row.source_type == "document" else "保存的网页正文快照；此操作不会重新抓取网页。"}
    return value


@router.get("/tasks/{task_id}/evidences")
def list_task_evidences(task_id: UUID, db: Annotated[Session, Depends(get_db)]):
    state, rows = task_evidences(db, task_id)
    return {"task_id": task_id, "claims": state.get("claims", []),
        "claim_statuses": state.get("claim_statuses", {}),
        "items": [serialize(db, row, claim) for row, claim in rows]}


@router.get("/tasks/{task_id}/evidences/{evidence_id}")
def get_task_evidence(task_id: UUID, evidence_id: UUID, db: Annotated[Session, Depends(get_db)]):
    _, rows = task_evidences(db, task_id)
    for row, claim in rows:
        if row.id == evidence_id:
            return serialize(db, row, claim, include_source=True)
    raise HTTPException(status_code=404, detail="Evidence not found in this task")
