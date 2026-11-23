from datetime import datetime, timedelta, timezone
from threading import Event
import time
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine, delete, select
from sqlalchemy.orm import Session

from app.agent.errors import AgentConflict, LeaseLost
from app.agent.graph import AgentRuntime, resume_agent_task, run_agent_task
from app.agent.tools import ToolContext, ToolRegistry, ToolResult
from app.db.models import (AgentRun, AgentStep, EvidenceAssessment, ResearchTask, StateSnapshot,
                           ToolExecutionReceipt, DocumentChunk, ClaimRecord)
from app.db.session import Base, get_db
from app.main import app
from app.memory.checkpoints import fence_tool_commit, reserve_recovery
from app.verification.contradiction import detect_contradiction
from app.verification.freshness import detect_outdated
from app.verification.verifier import perform_verification, VerificationResult
from scripts.adaptive_scenarios import ControlledClient, emit_fixture, run_scenario


@pytest.fixture
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'memory.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        yield db
    engine.dispose()


def rewind_to_reserved_tool(db):
    result = run_scenario(db, "sufficient_evidence")
    task_id = UUID(result["state"]["task_id"])
    point = db.scalar(select(StateSnapshot).where(StateSnapshot.task_id == task_id,
        StateSnapshot.next_node == "recover_tool").order_by(StateSnapshot.sequence).limit(1))
    later = list(db.scalars(select(AgentStep.id).where(AgentStep.task_id == task_id, AgentStep.step_index > 1)))
    db.execute(delete(ToolExecutionReceipt).where(ToolExecutionReceipt.step_id.in_(later)))
    db.execute(delete(AgentStep).where(AgentStep.id.in_(later)))
    db.execute(delete(StateSnapshot).where(StateSnapshot.task_id == task_id, StateSnapshot.sequence > point.sequence))
    db.execute(delete(EvidenceAssessment).where(EvidenceAssessment.task_id == task_id))
    run = db.get(AgentRun, task_id)
    run.state, run.checkpoint_seq = point.state, point.sequence
    run.owner_token, run.lease_expires_at = "crashed-worker", datetime.now(timezone.utc) - timedelta(seconds=1)
    db.get(ResearchTask, task_id).status = "running"
    step = db.scalar(select(AgentStep).where(AgentStep.task_id == task_id))
    step.status, step.observation = "running", None
    db.commit()
    return task_id, step


def test_recovery_uses_receipt_without_repeating_successful_tool(db):
    task_id, step = rewind_to_reserved_tool(db)
    model = ControlledClient("sufficient_evidence")
    def forbidden(*args, **kwargs):
        pytest.fail("A receipted acquisition must not be repeated")
    state = resume_agent_task(db, task_id, client=model, registry=ToolRegistry({"rag_retrieve_tool": forbidden}))
    assert state["status"] == "completed" and state["coverage"] == 1
    assert state["round_count"] == 2 and state["recovery_count"] == 1
    assert model.operations == ["agent_router", "evidence_verification"]
    assert state["retrieval_count"] == 1 and state["applied_step_indices"] == [1, 2]


def test_unknown_tool_outcome_is_not_blindly_retried(db):
    task_id, step = rewind_to_reserved_tool(db)
    db.execute(delete(ToolExecutionReceipt).where(ToolExecutionReceipt.step_id == step.id))
    db.commit()
    model = ControlledClient("sufficient_evidence")
    state = resume_agent_task(db, task_id, client=model)
    assert state["termination_action"] == "ABSTAIN" and state["stop_reason"] == "uncertain_tool_outcome"
    assert state["round_count"] == 1 and not model.operations


def test_live_worker_lease_prevents_concurrent_recovery_and_stale_writes(db):
    task_id, step = rewind_to_reserved_tool(db)
    token = reserve_recovery(db, task_id)
    with pytest.raises(AgentConflict):
        reserve_recovery(db, task_id)
    with pytest.raises(LeaseLost):
        fence_tool_commit(db, task_id, "crashed-worker")
    db.rollback()
    assert db.get(AgentRun, task_id).owner_token == token


def test_failed_node_resumes_from_checkpoint_without_repeating_acquisition(db, monkeypatch):
    original = AgentRuntime.state_updater
    def crash(self, state):
        raise RuntimeError("controlled node failure")
    def acquire(db, context, query, top_k=5):
        return ToolResult(ok=True, summary="Committed evidence", evidence_ids=[emit_fixture(db, context)])
    row = ResearchTask(query="Check fixture deployment", budget=4)
    db.add(row)
    db.commit()
    monkeypatch.setattr(AgentRuntime, "state_updater", crash)
    state = run_agent_task(db, row.id, client=ControlledClient("sufficient_evidence"),
                           registry=ToolRegistry({"rag_retrieve_tool": acquire}))
    assert state["status"] == "failed" and state["recovery_node"] == "state_updater"
    assert db.get(AgentRun, row.id).owner_token is None
    monkeypatch.setattr(AgentRuntime, "state_updater", original)
    def forbidden(*args, **kwargs):
        pytest.fail("Committed acquisition must not run again")
    state = resume_agent_task(db, row.id, client=ControlledClient("sufficient_evidence"),
                              registry=ToolRegistry({"rag_retrieve_tool": forbidden}))
    assert state["status"] == "completed" and state["round_count"] == 2 and state["retrieval_count"] == 1


@pytest.mark.parametrize("field,value", [("model_name", "Other"), ("model_version", "2"), ("deployment_mode", "api")])
def test_different_scope_is_not_a_confirmed_conflict(field, value):
    a = {"evidence_id": "E1", "relation": "supports", "supporting_text": "Model X supports 128K context.",
         "scope": {"model_name": "X", "model_version": "1", "deployment_mode": "local"}}
    b = {**a, "evidence_id": "E2", "relation": "refutes", "supporting_text": "Model X supports 32K context.",
         "scope": {**a["scope"], field: value}}
    result = detect_contradiction("context", [a, b])
    assert not result["has_conflict"] and not result["requires_verification"]
    assert result["pairs"][0]["classification"] == "DIFFERENT_SCOPE"


def test_unknown_scope_and_different_dates_require_qualification():
    a = {"evidence_id": "E1", "supporting_text": "Model X supports 128K context.", "published_at": "2026-01-01"}
    b = {"evidence_id": "E2", "supporting_text": "Model X supports 32K context.", "published_at": "2026-02-01"}
    result = detect_contradiction("context", [a, b])
    assert not result["has_conflict"] and result["requires_verification"]
    assert result["pairs"][0]["classification"] == "POSSIBLE_TEMPORAL_UPDATE"


def test_version_freshness_and_missing_date():
    now = datetime.now(timezone.utc)
    evidence = {"retrieved_at": now, "scope": {"model_version": "1"}}
    assert detect_outdated(evidence, "2")["version_mismatch"]
    assert not detect_outdated(evidence, "1")["outdated"]
    assert detect_outdated({})["outdated"]
    assert detect_outdated({"published_at": "not-a-date"})["outdated"]


def test_outdated_verification_result_is_persisted_with_quote_relations(db):
    task = ResearchTask(query="fixture", budget=2)
    claim = {"id": "C1", "claim": "The fixture service supports private deployment.", "required_source": "any"}
    db.add_all([task, ClaimRecord(claim_id="C1", claim_text=claim["claim"])])
    db.commit()
    context = ToolContext(task.id, claim, ControlledClient("sufficient_evidence"), time.monotonic() + 10, Event())
    identifier = emit_fixture(db, context, stale_at=datetime.now(timezone.utc) - timedelta(days=400))
    context.claim = {**claim, "evidence_ids": [identifier]}
    assert perform_verification(db, context)["status"] == "OUTDATED"
    result = db.scalar(select(EvidenceAssessment)).result
    checked = VerificationResult.model_validate({key: result[key] for key in VerificationResult.model_fields})
    assert checked.status == "OUTDATED" and checked.evidence_relations[0]["relation"] == "outdated"


def test_core_fastapi_background_history_trace_report_feedback_upload(db, fake_embedder, monkeypatch):
    monkeypatch.setattr("app.agent.graph.get_llm_client", lambda: ControlledClient("sufficient_evidence"))
    monkeypatch.setattr("app.agent.tools.hybrid_retrieve", lambda session, *a, **k:
                        [type("Hit", (), {"chunk": chunk}) for chunk in session.scalars(select(DocumentChunk))])
    app.dependency_overrides[get_db] = lambda: db
    client = TestClient(app)
    try:
        assert client.get("/health").status_code == 200
        uploaded = client.post("/api/v1/documents/upload", files={"file": ("guide.md", "The fixture service supports private deployment.")})
        assert uploaded.status_code == 201, uploaded.text
        assert client.get("/api/v1/documents").json()["total"] == 1
        created = client.post("/api/v1/research", json={"query": "Check fixture deployment", "budget": 4})
        assert created.status_code == 201, created.text
        task_id = created.json()["task_id"]
        assert task_id == created.json()["id"]
        task_response = client.get(f"/api/v1/tasks/{task_id}").json()
        run_state = db.get(AgentRun, UUID(task_id)).state
        assert task_response["status"] == "completed", run_state
        assert client.get("/api/v1/tasks").json()["total"] == 1
        trace = client.get(f"/api/v1/tasks/{task_id}/trace").json()
        assert len(trace["steps"]) == 2 and trace["checkpoints"]
        assert client.get(f"/api/v1/tasks/{task_id}/report").status_code == 200
        assert client.get(f"/api/v1/tasks/{task_id}/verifications").json()["items"][0]["result"]["status"] == "SUPPORTED"
        assert client.post(f"/api/v1/tasks/{task_id}/feedback", json={"rating": 5, "comment": "Useful"}).status_code == 201
        assert client.post(f"/api/v1/tasks/{task_id}/feedback", json={"rating": 6}).status_code == 422
        assert client.get("/api/v1/evaluations").status_code == 200
        assert client.post(f"/api/v1/tasks/{task_id}/recover").status_code == 409
        assert client.get(f"/api/v1/tasks/{uuid4()}/trace").status_code == 404
        assert client.post("/api/v1/research", json={"query": " "}).status_code == 422
    finally:
        client.close()
        app.dependency_overrides.clear()
