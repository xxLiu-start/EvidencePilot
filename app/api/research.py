from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, status
from sqlalchemy.orm import Session

from app.api.schemas import ResearchRequest, ResearchTaskResponse
from app.db.models import ResearchTask
from app.db.session import get_db
from app.agent.background import execute_background
from app.domains.profiles import get_domain_profile
from fastapi import HTTPException

from app.auth.oidc import require_roles

router = APIRouter()


@router.post(
    "/research",
    response_model=ResearchTaskResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_roles("RESEARCHER", "PROJECT_MANAGER", "TENANT_ADMIN", "SUPER_ADMIN"))],
)
def create_research_task(
    request: ResearchRequest, background_tasks: BackgroundTasks, db: Annotated[Session, Depends(get_db)]
) -> dict:
    try:
        get_domain_profile(request.domain_id)
    except ValueError:
        raise HTTPException(status_code=422, detail="Unknown research domain") from None
    task = ResearchTask(query=request.query, budget=request.budget, domain_id=request.domain_id)
    db.add(task)
    db.commit()
    db.refresh(task)
    background_tasks.add_task(execute_background, db.get_bind(), task.id)
    return {"id": task.id, "task_id": task.id, "query": task.query, "status": task.status,
            "budget": task.budget, "created_at": task.created_at}
