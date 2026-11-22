from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent.graph import AgentConflict, run_agent_task
from app.db.models import AgentRun, AgentStep, ResearchTask
from app.db.session import get_db
from app.llm_client import LLMConfigurationError


router = APIRouter()


@router.post("/tasks/{task_id}/run")
def run_task(task_id: UUID, db: Annotated[Session, Depends(get_db)]):
    try:
        state = run_agent_task(db, task_id)
        return {"task_id": task_id, "status": state["status"], "state": state}
    except LookupError:
        raise HTTPException(status_code=404, detail="Research task not found") from None
    except AgentConflict:
        raise HTTPException(status_code=409, detail="Task is not startable or is already running") from None
    except LLMConfigurationError:
        raise HTTPException(status_code=503, detail="Selected inference branch is not configured") from None


@router.get("/tasks/{task_id}/agent")
def get_agent_state(task_id: UUID, db: Annotated[Session, Depends(get_db)]):
    if db.get(ResearchTask, task_id) is None:
        raise HTTPException(status_code=404, detail="Research task not found")
    run = db.get(AgentRun, task_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Task has not run")
    return {"task_id": task_id, "status": run.state["status"], "state": run.state}


@router.get("/tasks/{task_id}/steps")
def get_agent_steps(task_id: UUID, db: Annotated[Session, Depends(get_db)]):
    if db.get(ResearchTask, task_id) is None:
        raise HTTPException(status_code=404, detail="Research task not found")
    rows = db.scalars(select(AgentStep).where(AgentStep.task_id == task_id).order_by(AgentStep.step_index))
    return [{"id": row.id, "task_id": row.task_id, "step_index": row.step_index,
             "claim_id": row.claim_id, "tool_name": row.tool_name, "arguments": row.arguments,
             "observation": row.observation, "status": row.status, "elapsed_ms": row.elapsed_ms,
             "error_category": row.error_category, "trace": row.trace, "created_at": row.created_at} for row in rows]
