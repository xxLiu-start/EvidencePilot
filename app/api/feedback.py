from datetime import datetime, timezone
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.schemas import FeedbackRequest
from app.api.tasks import require_task
from app.db.models import TaskFeedback
from app.db.session import get_db

from app.auth.oidc import require_roles

router = APIRouter()


@router.post("/tasks/{task_id}/feedback", status_code=201, dependencies=[Depends(require_roles("RESEARCHER", "REVIEWER", "PROJECT_MANAGER", "TENANT_ADMIN", "SUPER_ADMIN"))])
def add_feedback(task_id: UUID, request: FeedbackRequest, db: Annotated[Session, Depends(get_db)]):
    require_task(db, task_id)
    row = TaskFeedback(task_id=task_id, rating=request.rating, comment=request.comment, created_at=datetime.now(timezone.utc))
    db.add(row)
    db.commit()
    return {"id": row.id, "task_id": task_id, "rating": row.rating, "comment": row.comment, "created_at": row.created_at}
