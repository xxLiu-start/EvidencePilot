from datetime import datetime, timedelta, timezone
import json
from uuid import UUID, uuid4

from sqlalchemy import select, update

from app.agent.errors import AgentConflict, LeaseLost
from app.config import settings
from app.db.models import AgentRun, ResearchTask, StateSnapshot


def utc(value):
    return value.replace(tzinfo=timezone.utc) if value and value.tzinfo is None else value


def lease_deadline(config=settings):
    duration = max(config.agent_lease_seconds, config.llm_timeout_seconds * (config.llm_max_retries + 1) + 60)
    return datetime.now(timezone.utc) + timedelta(seconds=duration)


def fence_tool_commit(db, task_id, owner_token, config=settings):
    if not owner_token:
        return
    changed = db.execute(update(AgentRun).where(AgentRun.task_id == task_id,
        AgentRun.owner_token == owner_token, AgentRun.lease_expires_at > datetime.now(timezone.utc)
    ).values(lease_expires_at=lease_deadline(config)).execution_options(synchronize_session=False)).rowcount
    if changed != 1:
        raise LeaseLost("Worker no longer owns this task")
    # The row lock is retained until this tool's effects and receipt commit.


def save_checkpoint(db, state, next_node, *, owner_token=None, config=settings):
    snapshot = json.loads(json.dumps(state, ensure_ascii=False))
    if len(json.dumps(snapshot, ensure_ascii=False).encode()) > 65536:
        raise ValueError("Agent state exceeds its metadata-only size limit")
    run = db.scalar(select(AgentRun).where(AgentRun.task_id == UUID(state["task_id"])).with_for_update()
                    .execution_options(populate_existing=True))
    if owner_token and run.owner_token != owner_token:
        raise LeaseLost("Worker no longer owns this task")
    run.checkpoint_seq += 1
    run.state, run.updated_at = snapshot, datetime.now(timezone.utc)
    if state["status"] == "running" and owner_token:
        run.lease_expires_at = lease_deadline(config)
    elif next_node == "END":
        run.owner_token, run.lease_expires_at = None, None
    db.add(StateSnapshot(task_id=run.task_id, sequence=run.checkpoint_seq, state=snapshot,
                         next_node=next_node, created_at=run.updated_at))
    db.get(ResearchTask, run.task_id).status = state["status"]
    db.commit()
    return snapshot


def latest_snapshot(db, task_id):
    return db.scalar(select(StateSnapshot).where(StateSnapshot.task_id == UUID(str(task_id)))
                     .order_by(StateSnapshot.sequence.desc()).limit(1))


def reserve_recovery(db, task_id, config=settings):
    task_id = UUID(str(task_id))
    run = db.scalar(select(AgentRun).where(AgentRun.task_id == task_id).with_for_update()
                    .execution_options(populate_existing=True))
    if db.get(ResearchTask, task_id) is None:
        raise LookupError("Research task not found")
    checkpoint = latest_snapshot(db, task_id)
    if run is None or checkpoint is None:
        db.rollback()
        raise AgentConflict("Task has no resumable checkpoint")
    if checkpoint.next_node == "END":
        db.rollback()
        raise AgentConflict("Task has already finished")
    now = datetime.now(timezone.utc)
    if run.owner_token and utc(run.lease_expires_at) and utc(run.lease_expires_at) > now:
        db.rollback()
        raise AgentConflict("Task still has an active worker lease")
    token = uuid4().hex
    # Conditional update also fences concurrent recovery requests on SQLite.
    changed = db.execute(update(AgentRun).where(AgentRun.task_id == task_id,
        AgentRun.owner_token == run.owner_token,
        (AgentRun.lease_expires_at.is_(None) | (AgentRun.lease_expires_at <= now))
    ).values(owner_token=token, lease_expires_at=lease_deadline(config)).execution_options(synchronize_session=False)).rowcount
    if changed != 1:
        db.rollback()
        raise AgentConflict("Another worker claimed this recovery")
    db.get(ResearchTask, task_id).status = "running"
    db.commit()
    return token
