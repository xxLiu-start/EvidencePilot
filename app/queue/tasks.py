"""Idempotent Celery task entry points."""
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy.exc import SQLAlchemyError

from app.agent.errors import AgentConflict
from app.db.models import AgentRun, ResearchTask
from app.db.session import SessionLocal
from app.queue.celery_app import celery_app


@celery_app.task(
    bind=True,
    name="evidencepilot.execute_research_task",
    acks_late=True,
    reject_on_worker_lost=True,
    autoretry_for=(SQLAlchemyError, ConnectionError, TimeoutError),
    retry_backoff=True,
    retry_jitter=True,
    max_retries=10,
)
def execute_research_task(self, task_id: str, tenant_id: str | None = None,
                           project_id: str | None = None, user_id: str | None = None) -> dict:
    from app.agent.graph import run_agent_task

    identifier = UUID(str(task_id))
    with SessionLocal() as db:
        if tenant_id is not None:
            db.info.update({"tenant_id": UUID(tenant_id), "user_id": UUID(user_id) if user_id else None,
                            "project_ids": (UUID(project_id),), "selected_project_id": UUID(project_id),
                            "selected_project_ids": (UUID(project_id),)})
        elif settings.environment == "production":
            raise RuntimeError("Tenant context is required for RLS-protected Celery tasks")
        task = db.get(ResearchTask, identifier)
        if task is None:
            return {"task_id": str(identifier), "status": "not_found"}
        db.info.update({"tenant_id": task.tenant_id, "user_id": task.created_by_user_id,
                        "project_ids": (task.project_id,), "selected_project_id": task.project_id,
                        "selected_project_ids": (task.project_id,)})
        try:
            state = run_agent_task(db, identifier)
            return {"task_id": str(identifier), "status": state.get("status", "unknown")}
        except AgentConflict:
            db.rollback()
            run = db.get(AgentRun, identifier)
            if run is not None and run.state.get("status") == "running":
                lease = run.lease_expires_at
                now = datetime.now(timezone.utc)
                if lease is not None and lease.replace(tzinfo=lease.tzinfo or timezone.utc) > now:
                    raise self.retry(countdown=60, max_retries=10)
                from app.agent.graph import resume_agent_task

                state = resume_agent_task(db, identifier)
                return {"task_id": str(identifier), "status": state.get("status", "unknown"),
                        "recovered": True}
            if run is not None:
                return {"task_id": str(identifier), "status": run.state.get("status", "unknown")}
            raise
