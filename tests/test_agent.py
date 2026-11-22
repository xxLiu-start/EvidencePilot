from dataclasses import dataclass
import json
from threading import Event
import time
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.agent.graph import AgentConflict, run_agent_task
from app.agent.planner import ResearchPlan, decompose_claims
from app.agent.tools import OpenSourceInput, ToolContext, ToolRegistry, ToolResult, open_source_tool
from app.config import Settings
from app.db.models import AgentAllowedSource, AgentPolicyDecision, AgentRun, AgentStep, DocumentChunk, DocumentRecord, EvidenceRecord, ResearchTask, WebContent, WebSource
from app.db.session import Base
from app.llm_client import InferenceTrace, LLMError


PLAN = {"claims": [
    {"id": "C1", "claim": "Qwen private deployment", "priority": "high", "required_source": "any"},
    {"id": "C2", "claim": "DeepSeek function calling", "priority": "high", "required_source": "any"},
]}


class FakeClient:
    def __init__(self, actions=(), plan=PLAN, planner_errors=0, mode="local"):
        self.actions, self.plan, self.planner_errors = iter(actions), plan, planner_errors
        self.operations, self.messages = [], []
        self.config = SimpleNamespace(llm_mode=mode)

    def complete_json(self, messages, output_type, *, operation, **kwargs):
        self.operations.append(operation)
        self.messages.append(messages)
        if operation.startswith("claim_decomposition"):
            if self.planner_errors:
                self.planner_errors -= 1
                raise LLMError("invalid_json", [{"loc": ["claims"], "type": "missing"}])
            value = self.plan
        elif operation == "evidence_verification":
            payload = json.loads(messages[-1]["content"])
            value = {"verdict": "SUPPORTED", "supporting_evidence_ids": [e["evidence_id"] for e in payload["evidence"]],
                     "refuting_evidence_ids": [], "reason": "Controlled test fixture verifies this claim."}
        elif operation == "query_rewrite":
            value = {"query": "refined evidence search " + str(len(self.operations))}
        else:
            value = next(self.actions, {"action": "STOP"})
            if isinstance(value, Exception):
                raise value
        return output_type.model_validate(value), InferenceTrace(operation, self.config.llm_mode, "test", "test", 1, 1, None)


@pytest.fixture
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'agent.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


def task(db, budget=4):
    row = ResearchTask(query="Compare Qwen and DeepSeek deployment and function calling", budget=budget)
    db.add(row)
    db.commit()
    return row


def seed_document(db):
    document = DocumentRecord(filename="guide.md", source="guide.md", version="1", chunk_count=1)
    db.add(document)
    db.flush()
    chunk = DocumentChunk(document_id=document.id, chunk_index=0,
                          text="Qwen and DeepSeek deployment and function calling documentation. " * 100,
                          token_count=500, source="guide.md", version="1")
    db.add(chunk)
    db.commit()
    return chunk


@pytest.mark.parametrize("mode", ["local", "remote"])
def test_graph_three_actions_logs_state_budget_and_idempotence(db, monkeypatch, mode):
    chunk = seed_document(db)
    monkeypatch.setattr("app.agent.tools.hybrid_retrieve", lambda *a, **k: [SimpleNamespace(chunk=chunk)])
    client = FakeClient([
        {"tool": "rag_retrieve_tool", "claim_id": "C1", "arguments": {"query": "Qwen deployment"}},
        {"tool": "web_search_tool", "claim_id": "C2", "arguments": {"query": "DeepSeek function calling"}},
        {"tool": "rag_retrieve_tool", "claim_id": "C2", "arguments": {"query": "DeepSeek functions"}},
    ], mode=mode)
    registry = ToolRegistry({"web_search_tool": lambda db, context, query: ToolResult(ok=True, summary="Search completed")})
    row = task(db, budget=6)
    state = run_agent_task(db, row.id, client=client, registry=registry)
    assert state["status"] == "completed", state
    assert state["round_count"] == 5 and state["remaining_budget"] == 1
    assert state["search_count"] == 1 and state["retrieval_count"] == 2
    assert len(state["evidence_ids"]) == 2 and state["pending_observation"] is None
    assert chunk.text not in str(state)
    assert "Evidence" in state["final_report"]
    steps = list(db.scalars(select(AgentStep).order_by(AgentStep.step_index)))
    assert len(steps) == 5 and all(step.status == "success" for step in steps)
    assert all(step.trace["mode"] == mode for step in steps)
    count = len(client.operations)
    assert run_agent_task(db, row.id, client=client)["round_count"] == 5
    assert len(client.operations) == count


def test_planner_repairs_once_and_failed_second_attempt_marks_task_failed(db):
    client = FakeClient(planner_errors=1)
    plan, repaired, trace = decompose_claims("Compare models", client=client)
    assert repaired and len(plan.claims) == 2
    assert client.operations == ["claim_decomposition", "claim_decomposition_repair"]
    assert "Validation error" in client.messages[-1][-1]["content"]
    bad = FakeClient(planner_errors=2)
    row = task(db)
    state = run_agent_task(db, row.id, client=bad)
    assert state["status"] == "failed" and state["stop_reason"] == "planner_failed"
    assert len(bad.operations) == 2 and state["round_count"] == 0
    assert db.scalar(select(func.count()).select_from(AgentStep)) == 0


@pytest.mark.parametrize("action", [
    {"tool": "rag_retrieve_tool", "claim_id": "C1", "arguments": {"query": "Qwen", "top_k": 999}},
    {"tool": "rag_retrieve_tool", "claim_id": "C1", "arguments": {"query": "Qwen", "api_key": "must-not-log"}},
    {"tool": "verify_claim_tool", "claim_id": "C99", "arguments": {"claim_id": "C99"}},
    LLMError("invalid_json"),
])
def test_invalid_proposals_are_audited_before_dispatch(db, action):
    calls = []
    registry = ToolRegistry({"rag_retrieve_tool": lambda *a, **k: calls.append(1)})
    row = task(db, budget=1)
    state = run_agent_task(db, row.id, client=FakeClient([action]), registry=registry)
    assert state["status"] == "partial" and state["remaining_budget"] == 1
    assert state["round_count"] == 0 and calls == []
    assert db.scalar(select(AgentStep)) is None
    decision = db.scalar(select(AgentPolicyDecision))
    assert decision.selected_action == "ABSTAIN"
    assert "must-not-log" not in str(decision.snapshot)
    assert "must-not-log" not in str(state)


def test_timeout_stops_new_actions_and_prevents_late_worker_write(db):
    release, finished = Event(), Event()

    def blocking(db, context, query, top_k=5):
        try:
            release.wait(timeout=2)
            context.check()
            db.add(DocumentRecord(filename="late", source="late", version="1", chunk_count=0))
            db.commit()
            return ToolResult(ok=True, summary="late")
        finally:
            finished.set()

    cfg = Settings(database_url="sqlite://", agent_tool_timeout_seconds=.05)
    row = task(db)
    client = FakeClient([{"tool": "rag_retrieve_tool", "claim_id": "C1", "arguments": {"query": "Qwen"}}])
    started = time.monotonic()
    state = run_agent_task(db, row.id, client=client, config=cfg,
                           registry=ToolRegistry({"rag_retrieve_tool": blocking}))
    assert time.monotonic() - started < 1
    assert state["stop_reason"] == "tool_timeout" and state["round_count"] == 1
    release.set()
    assert finished.wait(timeout=2)
    assert db.scalar(select(func.count()).select_from(DocumentRecord)) == 0
    assert db.scalar(select(AgentStep)).status == "timeout"


@pytest.mark.parametrize("url", ["file:///etc/passwd", "http://localhost/", "http://127.0.0.1/",
                                  "http://169.254.169.254/latest/meta-data/", "http://10.0.0.1/", "http://[::1]/"])
def test_open_source_schema_rejects_non_web_or_private_targets(url):
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        OpenSourceInput(url=url)


def test_open_source_requires_current_task_permission(db):
    row = task(db)
    context = ToolContext(row.id, {"id": "C1", "claim": "Qwen"}, FakeClient(), time.monotonic() + 5, Event())
    with pytest.raises(ValueError, match="not searched"):
        open_source_tool(db, context, "https://example.com/")
    from datetime import datetime, timezone
    from app.search.cache import digest

    other = task(db)
    content = WebContent(content_hash=digest("A cached public source body."), body="A cached public source body.", cache_path="")
    db.add(content)
    db.flush()
    source = WebSource(canonical_url="https://example.com/", fetched_url="https://example.com/", title="source",
                       content_hash=content.content_hash, retrieved_at=datetime.now(timezone.utc), source_type="web", search_queries=[])
    db.add(source)
    db.flush()
    db.add(AgentAllowedSource(task_id=other.id, source_id=source.id, claim_id="C1"))
    db.commit()
    with pytest.raises(ValueError, match="not searched"):
        open_source_tool(db, context, "https://example.com/")


def test_duplicate_running_task_rejected(db):
    row = task(db)
    from app.agent.state import initial_state
    from datetime import datetime, timezone
    db.add(AgentRun(task_id=row.id, state=initial_state(row.id, row.query, 4), updated_at=datetime.now(timezone.utc)))
    db.commit()
    with pytest.raises(AgentConflict):
        run_agent_task(db, row.id, client=FakeClient())


def test_remote_https_shared_client_executes_validated_actions(db, tmp_path):
    import json
    import httpx
    from app.llm_client import ReasoningClient

    secret = tmp_path / ".env"
    secret.write_text("LLM_REMOTE_API_KEY=remote-test-credential\n")
    cfg = Settings(_env_file=None, database_url="sqlite://", credentials_env_file=str(secret), llm_mode="remote",
                   llm_remote_base_url="https://inference.example/v1", llm_remote_model="remote-model")
    requests = []

    def handler(request):
        requests.append(request)
        if len(requests) == 1:
            value = PLAN
        else:
            value = {"tool": "web_search_tool", "claim_id": "C1", "arguments": {"query": "Qwen official docs " + str(len(requests))}}
        return httpx.Response(200, request=request, json={
            "id": "call", "object": "chat.completion", "created": 0, "model": "remote-model-v1",
            "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": json.dumps(value)}}]})

    client = ReasoningClient(cfg, transport=httpx.MockTransport(handler))
    registry = ToolRegistry({"web_search_tool": lambda db, context, query: ToolResult(ok=False, summary="No result")})
    row = task(db, budget=2)
    state = run_agent_task(db, row.id, client=client, registry=registry, config=cfg)
    client.close()
    assert state["round_count"] == 2 and len(requests) == 3
    assert all(request.url.scheme == "https" for request in requests)
    assert all(request.headers["authorization"] == "Bearer remote-test-credential" for request in requests)
    steps = list(db.scalars(select(AgentStep)))
    assert len(steps) == 2 and all(step.trace["mode"] == "remote" for step in steps)
    assert all(step.trace["actual_model"] == "remote-model-v1" for step in steps)
    assert "remote-test-credential" not in str(state)
