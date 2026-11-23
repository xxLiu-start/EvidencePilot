"""Poll the transactional outbox and publish idempotent Celery messages."""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Callable

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.config import settings
from app.db.models import OutboxEvent, Tenant
from app.db.session import SessionLocal, apply_rls_context


def dispatch_once(
    *,
    session_factory=SessionLocal,
    publish: Callable[[str, str], None] | None = None,
    batch_size: int | None = None,
    max_attempts: int | None = None,
) -> dict[str, int]:
    """Publish one batch. A crash after publish but before commit may duplicate a message.

    Consumers are idempotent by ResearchTask ID and existing AgentRun state; delivery is
    at-least-once, never advertised as exactly-once.
    """
    use_default_publisher = publish is None
    if use_default_publisher:
        from app.queue.tasks import execute_research_task

    batch_size = batch_size or settings.task_queue_batch_size
    max_attempts = max_attempts or settings.task_queue_max_attempts
    published = failed = 0
    with session_factory() as db:
        statement = (
            select(OutboxEvent)
            .where(OutboxEvent.status == "pending",
                   or_(OutboxEvent.next_attempt_at.is_(None), OutboxEvent.next_attempt_at <= datetime.now(timezone.utc)))
            .order_by(OutboxEvent.created_at, OutboxEvent.id)
            .limit(batch_size)
            .with_for_update(skip_locked=True)
        )
        # PostgreSQL RLS hides outbox rows until a tenant is bound. Iterate
        # active tenants and set transaction-local context before each tenant query.
        if db.get_bind().dialect.name == "postgresql":
            tenant_ids = list(db.scalars(select(Tenant.id).where(Tenant.is_active.is_(True))))
        else:
            tenant_ids = [None]
        for tenant_id in tenant_ids:
            if published + failed >= batch_size:
                break
            if tenant_id is not None:
                db.info["tenant_id"] = tenant_id
                apply_rls_context(db, tenant_id)
            tenant_statement = statement.limit(max(1, batch_size - published - failed))
            events = list(db.scalars(tenant_statement))
            for event in events:
                try:
                    task_identifier = str(event.aggregate_id)
                    if use_default_publisher:
                        payload = event.payload or {}
                        required = ("tenant_id", "project_id", "user_id")
                        if any(not payload.get(key) for key in required):
                            raise ValueError("Outbox event is missing tenant execution context")
                        execute_research_task.apply_async(
                            args=[task_identifier, payload["tenant_id"], payload["project_id"], payload["user_id"]],
                            task_id=task_identifier,
                        )
                    else:
                        publish(task_identifier, task_identifier)
                    event.status = "published"
                    event.published_at = datetime.now(timezone.utc)
                    event.last_error = None
                    event.next_attempt_at = None
                    published += 1
                except Exception as exc:
                    event.attempts += 1
                    event.last_error = type(exc).__name__
                    if event.attempts >= max_attempts:
                        event.status = "failed"
                        event.next_attempt_at = None
                        failed += 1
                    else:
                        delay = min(2 ** event.attempts, 300)
                        event.next_attempt_at = datetime.now(timezone.utc) + timedelta(seconds=delay)
        db.commit()
    return {"published": published, "dead_lettered": failed}


def main() -> None:
    while True:
        dispatch_once()
        time.sleep(settings.task_queue_dispatch_interval_seconds)


if __name__ == "__main__":
    main()
