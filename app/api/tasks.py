from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.schemas import ResearchTaskResponse
from app.db.models import (ResearchTask, AgentRun, AgentStep, AgentPolicyDecision, AgentReflection,
                           StateSnapshot, EvidenceAssessment)
from app.db.session import get_db
from app.agent.background import execute_background
from app.agent.errors import AgentConflict
from app.evidence.coverage import calculate_coverage, coverage_items
from app.verification.verifier import assess_claim
from app.memory.checkpoints import reserve_recovery

from app.auth.oidc import RequestContext, get_request_context, require_roles
from app.auth.audit import record_audit_event

router = APIRouter()


def require_task(db, task_id):
    task = db.get(ResearchTask, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Research task not found")
    return task


@router.get("/tasks")
def list_tasks(db: Annotated[Session, Depends(get_db)], limit: int = Query(20, ge=1, le=100),
               offset: int = Query(0, ge=0)):
    rows = db.scalars(select(ResearchTask).order_by(ResearchTask.created_at.desc(), ResearchTask.id).offset(offset).limit(limit))
    return {"items": [ResearchTaskResponse.model_validate(row) for row in rows],
            "total": db.scalar(select(func.count()).select_from(ResearchTask)), "offset": offset, "limit": limit}


@router.get("/tasks/{task_id}/trace")
def get_trace(task_id: UUID, db: Annotated[Session, Depends(get_db)]):
    task = require_task(db, task_id)
    def records(model, order, fields):
        rows = db.scalars(select(model).where(model.task_id == task_id).order_by(order).limit(200))
        return [{field: getattr(row, field) for field in fields} for row in rows]
    return {"task_id": task_id, "status": task.status,
            "steps": records(AgentStep, AgentStep.step_index, ["id", "step_index", "claim_id", "tool_name", "arguments", "observation", "status", "elapsed_ms", "error_category", "trace"]),
            "policy_decisions": records(AgentPolicyDecision, AgentPolicyDecision.decision_index, ["decision_index", "proposed_action", "selected_action", "target_claim_id", "reason", "snapshot"]),
            "reflections": records(AgentReflection, AgentReflection.rewrite_index, ["rewrite_index", "claim_id", "failure_category", "analysis", "original_query", "rewritten_query", "trace"]),
            "checkpoints": records(StateSnapshot, StateSnapshot.sequence, ["sequence", "next_node", "created_at"])}


@router.get("/tasks/{task_id}/report")
def get_report(task_id: UUID, db: Annotated[Session, Depends(get_db)]):
    task = require_task(db, task_id)
    run = db.get(AgentRun, task_id)
    state = run.state if run else {}
    statuses = state.get("claim_statuses", {})
    # A reviewer may approve evidence after the Agent produced its terminal report.
    # Recompute the display status without modifying historical checkpoints or
    # silently re-running a model/tool.
    if run and task.status in {"completed", "partial", "failed"} and any(
        claim.get("human_review_required") for claim in state.get("claims", [])
    ):
        statuses = {
            claim["id"]: assess_claim(
                db, claim, state.get("claim_statuses", {}).get(claim["id"], {}).get("evidence_ids", []),
                task_id=task_id,
                retrieval_attempts=state.get("claim_retrieval_counts", {}).get(claim["id"], 0),
            ) for claim in state["claims"]
        }
    current_state = {**state, "claim_statuses": statuses}
    payload = {"task_id": task_id, "status": task.status, "report": state.get("final_report"),
               "coverage": calculate_coverage(coverage_items(current_state)) if run and statuses else 0,
               "claim_statuses": statuses,
               "coverage_reflects_post_run_reviews": bool(run and statuses != state.get("claim_statuses", {}))}
    return JSONResponse(status_code=200 if payload["report"] else 202, content=jsonable_encoder(payload))


@router.get("/tasks/{task_id}/verifications")
def get_verifications(task_id: UUID, db: Annotated[Session, Depends(get_db)]):
    require_task(db, task_id)
    rows = db.scalars(select(EvidenceAssessment).where(EvidenceAssessment.task_id == task_id)
                      .order_by(EvidenceAssessment.created_at).limit(200))
    return {"task_id": task_id, "items": [{"claim_id": row.claim_id, "fingerprint": row.fingerprint,
        "result": row.result, "trace": row.trace, "created_at": row.created_at} for row in rows]}


@router.post("/tasks/{task_id}/recover", status_code=202, dependencies=[Depends(require_roles("RESEARCHER", "PROJECT_MANAGER", "TENANT_ADMIN", "SUPER_ADMIN"))])
def recover_task(task_id: UUID, background_tasks: BackgroundTasks, db: Annotated[Session, Depends(get_db)],
                 context: RequestContext = Depends(get_request_context)):
    task = require_task(db, task_id)
    try:
        token = reserve_recovery(db, task_id)
    except AgentConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    record_audit_event(db, context, action="research_task.recover", resource_type="research_task",
                       resource_id=task_id, project_id=task.project_id)
    db.commit()
    background_tasks.add_task(execute_background, db.get_bind(), task_id, token,
                                context.tenant_id, task.project_id, context.user_id)
    return {"task_id": task_id, "status": "recovery_scheduled"}


@router.get("/tasks/{task_id}", response_model=ResearchTaskResponse)
def get_research_task(
    task_id: UUID, db: Annotated[Session, Depends(get_db)]
) -> ResearchTask:
    task = db.get(ResearchTask, task_id)
    if task is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Research task not found"
        )
    return task
