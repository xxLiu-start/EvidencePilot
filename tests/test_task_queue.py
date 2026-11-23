from datetime import datetime, timezone

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.db.models import OutboxEvent, ResearchTask
from app.db.session import Base
from app.queue.dispatcher import dispatch_once


def _session_factory(engine):
    return lambda: Session(engine, autoflush=False)


def test_outbox_dispatch_publishes_and_marks_event():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        task = ResearchTask(query="Research market structure", budget=3, domain_id="market_research")
        db.add(task)
        db.flush()
        event = OutboxEvent(
            event_type="research_task.created",
            aggregate_id=task.id,
            payload={"task_id": str(task.id)},
            status="pending",
            created_at=datetime.now(timezone.utc),
        )
        db.add(event)
        db.commit()
        event_id, task_id = event.id, task.id

    published = []
    result = dispatch_once(
        session_factory=_session_factory(engine),
        publish=lambda identifier, celery_id: published.append((identifier, celery_id)),
        batch_size=10,
    )
    assert result == {"published": 1, "dead_lettered": 0}
    assert published == [(str(task_id), str(task_id))]
    with Session(engine) as db:
        event = db.get(OutboxEvent, event_id)
        assert event.status == "published"
        assert event.published_at is not None
    # A published event is not dispatched again by a subsequent polling cycle.
    assert dispatch_once(session_factory=_session_factory(engine), publish=lambda *_: published.append(("duplicate", "duplicate"))) == {
        "published": 0, "dead_lettered": 0
    }
    engine.dispose()


def test_outbox_publish_failures_are_bounded_and_dead_lettered():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        task = ResearchTask(query="Research academic literature", budget=2)
        db.add(task)
        db.flush()
        event = OutboxEvent(
            event_type="research_task.created",
            aggregate_id=task.id,
            payload={"task_id": str(task.id)},
            status="pending",
            created_at=datetime.now(timezone.utc),
        )
        db.add(event)
        db.commit()
        event_id = event.id

    def fail_publish(_identifier, _celery_id):
        raise ConnectionError("broker unavailable")

    result = dispatch_once(
        session_factory=_session_factory(engine),
        publish=fail_publish,
        batch_size=10,
        max_attempts=1,
    )
    assert result == {"published": 0, "dead_lettered": 1}
    with Session(engine) as db:
        event = db.get(OutboxEvent, event_id)
        assert event.status == "failed"
        assert event.attempts == 1
        assert event.last_error == "ConnectionError"
    engine.dispose()
