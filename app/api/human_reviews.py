"""Immutable, tenant-scoped human review decisions for verified Claims."""
from datetime import datetime, timezone
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.audit import record_audit_event
from app.auth.oidc import RequestContext, get_request_context, require_roles
from app.db.models import AgentRun, EvidenceAssessment, HumanReview, ResearchTask
from app.db.session import get_db

router = APIRouter()


class ReviewDecision(BaseModel):
    fingerprint: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    decision: str = Field(pattern="^(approved|rejected)$")
    comment: str = Field(min_length=1, max_length=2000)


def _task(db: Session, task_id: UUID) -> ResearchTask:
    task = db.get(ResearchTask, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Research task not found")
    return task


def _claim_in_plan(db: Session, task_id: UUID, claim_id: str) -> dict:
    run = db.get(AgentRun, task_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Research task has no plan")
    claim = next((item for item in run.state.get("claims", []) if item.get("id") == claim_id), None)
    if claim is None:
        raise HTTPException(status_code=404, detail="Claim not found")
    if not claim.get("human_review_required", False):
        raise HTTPException(status_code=409, detail="This Claim does not require human review")
    return claim


@router.get("/tasks/{task_id}/claims/{claim_id}/reviews")
def list_claim_reviews(task_id: UUID, claim_id: str,
                       db: Annotated[Session, Depends(get_db)]):
    _task(db, task_id)
    _claim_in_plan(db, task_id, claim_id)
    rows = db.scalars(select(HumanReview).where(
        HumanReview.task_id == task_id, HumanReview.claim_id == claim_id
    ).order_by(HumanReview.created_at, HumanReview.id))
    return {"items": [
        {"id": row.id, "claim_id": row.claim_id, "fingerprint": row.fingerprint,
         "reviewer_user_id": row.reviewer_user_id, "decision": row.decision,
         "comment": row.comment, "created_at": row.created_at}
        for row in rows
    ]}


@router.post("/tasks/{task_id}/claims/{claim_id}/reviews", status_code=201,
             dependencies=[Depends(require_roles("REVIEWER", "PROJECT_MANAGER", "TENANT_ADMIN", "SUPER_ADMIN"))])
def submit_claim_review(task_id: UUID, claim_id: str, payload: ReviewDecision,
                        db: Annotated[Session, Depends(get_db)],
                        context: RequestContext = Depends(get_request_context)):
    task = _task(db, task_id)
    _claim_in_plan(db, task_id, claim_id)
    if task.created_by_user_id == context.user_id:
        raise HTTPException(status_code=403, detail="Authors cannot approve their own research")
    if task.project_id != context.selected_project_id and len(context.project_ids) > 1:
        raise HTTPException(status_code=400, detail="Select the task's project with X-Project-ID")
    assessment = db.get(EvidenceAssessment, (task_id, claim_id, payload.fingerprint))
    if assessment is None:
        raise HTTPException(status_code=409, detail="Evidence fingerprint is stale or not verified")
    result = assessment.result
    if payload.decision == "approved" and not (
        result.get("verified") is True and result.get("verdict") == "SUPPORTED"
    ):
        raise HTTPException(status_code=409, detail="Only semantically supported evidence can be approved")
    existing = db.scalar(select(HumanReview).where(
        HumanReview.task_id == task_id, HumanReview.claim_id == claim_id,
        HumanReview.fingerprint == payload.fingerprint,
        HumanReview.reviewer_user_id == context.user_id,
    ))
    if existing is not None:
        raise HTTPException(status_code=409, detail="Review is immutable; this reviewer already decided")
    review = HumanReview(
        tenant_id=task.tenant_id, project_id=task.project_id,
        task_id=task_id, claim_id=claim_id, fingerprint=payload.fingerprint,
        reviewer_user_id=context.user_id, decision=payload.decision,
        comment=payload.comment, created_at=datetime.now(timezone.utc),
    )
    db.add(review)
    db.flush()
    record_audit_event(db, context, action="human_review." + payload.decision,
                       resource_type="human_review", resource_id=review.id,
                       project_id=task.project_id,
                       metadata={"task_id": str(task_id), "claim_id": claim_id,
                                 "fingerprint": payload.fingerprint})
    db.commit()
    return {"id": review.id, "task_id": task_id, "claim_id": claim_id,
            "fingerprint": review.fingerprint, "decision": review.decision,
            "reviewer_user_id": review.reviewer_user_id, "created_at": review.created_at}
