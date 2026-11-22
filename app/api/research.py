from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, status
from sqlalchemy.orm import Session

from app.api.schemas import ResearchRequest, ResearchTaskResponse
from app.db.models import ResearchTask
from app.db.session import get_db
from app.agent.background import execute_background

router = APIRouter()


@router.post(
    "/research",
    response_model=ResearchTaskResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_research_task(
    request: ResearchRequest, background_tasks: BackgroundTasks, db: Annotated[Session, Depends(get_db)]
) -> dict:
    task = ResearchTask(query=request.query, budget=request.budget)
    db.add(task)
    db.commit()
    db.refresh(task)
    background_tasks.add_task(execute_background, db.get_bind(), task.id)
    return {"id": task.id, "task_id": task.id, "query": task.query, "status": task.status,
            "budget": task.budget, "created_at": task.created_at}
