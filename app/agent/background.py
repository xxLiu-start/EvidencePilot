"""Local MVP background jobs; each job owns its Session. Recovery is manual."""
from uuid import UUID
from sqlalchemy.orm import Session

from app.agent.errors import AgentConflict
from app.agent.graph import resume_agent_task, run_agent_task
from app.db.models import ResearchTask
from app.telemetry import audit_logger

logger = audit_logger(__name__)


def execute_background(engine, task_id, recovery_token=None):
    with Session(engine, autoflush=False) as db:
        try:
            task = db.get(ResearchTask, UUID(str(task_id)))
            if task is None:
                return
            db.info.update({"tenant_id": task.tenant_id, "user_id": task.created_by_user_id,
                            "project_ids": (task.project_id,), "selected_project_id": task.project_id,
                            "selected_project_ids": (task.project_id,)})
            if recovery_token:
                resume_agent_task(db, task_id, owner_token=recovery_token)
            else:
                run_agent_task(db, task_id)
        except AgentConflict:
            db.rollback()  # A concurrent /run or recovery already owns this task.
        except Exception as exc:
            db.rollback()
            task = db.get(ResearchTask, UUID(str(task_id)))
            if task and task.status == "queued":
                task.status = "failed"
                db.commit()
            logger.error("background_task_failed task_id=%s category=%s", task_id,
                         getattr(exc, "category", type(exc).__name__))
