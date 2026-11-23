"""Poll the transactional outbox and publish idempotent Celery messages."""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.db.models import OutboxEvent
from app.db.session import SessionLocal


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
    if publish is None:
        from app.queue.tasks import execute_research_task

        def publish(task_identifier: str, celery_task_id: str) -> None:
            execute_research_task.apply_async(args=[task_identifier], task_id=celery_task_id)

    batch_size = batch_size or settings.task_queue_batch_size
    max_attempts = max_attempts or settings.task_queue_max_attempts
    published = failed = 0
    with session_factory() as db:
        statement = (
            select(OutboxEvent)
            .where(OutboxEvent.status == "pending")
            .order_by(OutboxEvent.created_at, OutboxEvent.id)
            .limit(batch_size)
            .with_for_update(skip_locked=True)
        )
        events = list(db.scalars(statement))
        for event in events:
            try:
                task_identifier = str(event.aggregate_id)
                publish(task_identifier, task_identifier)
                event.status = "published"
                event.published_at = datetime.now(timezone.utc)
                event.last_error = None
                published += 1
            except Exception as exc:
                event.attempts += 1
                event.last_error = type(exc).__name__
                if event.attempts >= max_attempts:
                    event.status = "failed"
                    failed += 1
        db.commit()
    return {"published": published, "dead_lettered": failed}


def main() -> None:
    while True:
        dispatch_once()
        time.sleep(settings.task_queue_dispatch_interval_seconds)


if __name__ == "__main__":
    main()
