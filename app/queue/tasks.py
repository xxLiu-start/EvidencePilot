"""Idempotent Celery task entry points."""
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
    max_retries=5,
)
def execute_research_task(self, task_id: str) -> dict:
    from app.agent.graph import run_agent_task

    identifier = UUID(str(task_id))
    with SessionLocal() as db:
        task = db.get(ResearchTask, identifier)
        if task is None:
            return {"task_id": str(identifier), "status": "not_found"}
        try:
            state = run_agent_task(db, identifier)
            return {"task_id": str(identifier), "status": state.get("status", "unknown")}
        except AgentConflict:
            db.rollback()
            run = db.get(AgentRun, identifier)
            if run is not None:
                return {"task_id": str(identifier), "status": run.state.get("status", "running")}
            raise
