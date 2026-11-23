from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, status
from sqlalchemy.orm import Session

from app.api.schemas import ResearchRequest, ResearchTaskResponse
from app.db.models import OutboxEvent, ResearchTask
from app.db.session import get_db
from app.agent.background import execute_background
from app.domains.profiles import get_domain_profile
from app.config import settings
from datetime import datetime, timezone
from fastapi import HTTPException

from app.auth.oidc import RequestContext, get_request_context, require_roles
from app.auth.audit import record_audit_event, require_selected_project

router = APIRouter()


@router.post(
    "/research",
    response_model=ResearchTaskResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_roles("RESEARCHER", "PROJECT_MANAGER", "TENANT_ADMIN", "SUPER_ADMIN"))],
)
def create_research_task(
    request: ResearchRequest, background_tasks: BackgroundTasks, db: Annotated[Session, Depends(get_db)],
    context: RequestContext = Depends(get_request_context),
) -> dict:
    try:
        get_domain_profile(request.domain_id)
    except ValueError:
        raise HTTPException(status_code=422, detail="Unknown research domain") from None
    project_id = require_selected_project(context)
    task = ResearchTask(query=request.query, budget=request.budget, domain_id=request.domain_id,
                        tenant_id=context.tenant_id, project_id=project_id,
                        created_by_user_id=context.user_id)
    db.add(task)
    db.flush()
    if settings.task_queue_enabled:
        db.add(OutboxEvent(event_type="research_task.created", aggregate_id=task.id,
                           payload={"task_id": str(task.id), "tenant_id": str(context.tenant_id),
                                    "project_id": str(project_id), "user_id": str(context.user_id)}, status="pending",
                           created_at=datetime.now(timezone.utc)))
    db.flush()
    record_audit_event(db, context, action="research_task.create", resource_type="research_task",
                       resource_id=task.id, project_id=project_id,
                       metadata={"domain_id": request.domain_id, "budget": request.budget})
    db.commit()
    db.refresh(task)
    if not settings.task_queue_enabled:
        background_tasks.add_task(execute_background, db.get_bind(), task.id, None,
                                    context.tenant_id, project_id, context.user_id)
    return {"id": task.id, "task_id": task.id, "query": task.query, "status": task.status,
            "budget": task.budget, "domain_id": task.domain_id, "created_at": task.created_at}
