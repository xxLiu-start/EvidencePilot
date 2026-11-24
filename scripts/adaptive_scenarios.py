"""Reproducible control-flow fixtures, explicitly using a model/tool test double."""

from datetime import datetime, timezone
import json
from types import SimpleNamespace
from uuid import uuid4

from sqlalchemy import select

from app.agent.graph import run_agent_task
from app.agent.tools import ToolRegistry, ToolResult
from app.db.models import (AgentPolicyDecision, AgentStep, DocumentChunk, DocumentRecord,
                           ResearchTask, WebContent, WebSource)
from app.evidence.extraction import save_evidences
from app.evidence.schema import Evidence
from app.llm_client import InferenceTrace, LLMError
from app.search.cache import digest
from app.db.session import get_cached_content


SCENARIOS = ("missing_evidence", "sufficient_evidence", "conflicting_evidence",
             "budget_exhausted", "unverifiable_evidence", "bounded_loop")


class ControlledClient:
    def __init__(self, scenario):
        self.scenario, self.operations = scenario, []
        self.config = SimpleNamespace(llm_mode="fixture")

    def complete_json(self, messages, output_type, *, operation, **kwargs):
        self.operations.append(operation)
        if operation.startswith("claim_decomposition"):
            value = {"claims": [{"id": "C1", "claim": "The fixture service supports private deployment.",
                     "priority": "high", "required_source": "any",
                     "needs_external": self.scenario == "missing_evidence"}]}
        elif operation == "agent_router":
            # Deliberately propose STOP early and SEARCH after sufficient evidence.
            value = {"action": "STOP" if self.scenario == "missing_evidence" else "RETRIEVE_RAG",
                     "target_claim_id": "C1", "arguments": {"query": "fixture deployment capability"}}
        elif operation == "query_rewrite":
            value = {"query": "fixture deployment evidence angle " + str(len(self.operations))}
        elif operation == "evidence_verification":
            evidence = json.loads(messages[-1]["content"])["evidence"]
            support = [item["evidence_id"] for item in evidence if "does not support" not in item["quote"]]
            refute = [item["evidence_id"] for item in evidence if "does not support" in item["quote"]]
            if self.scenario == "unverifiable_evidence":
                raise LLMError("unverifiable_verdict")
            value = {"verdict": "CONTRADICTED" if refute else "SUPPORTED",
                     "supporting_evidence_ids": support, "refuting_evidence_ids": refute,
                     "reason": "Explicit controlled fixture verdict; not a live inference result."}
        else:
            raise AssertionError("Unexpected fixture operation: " + operation)
        return output_type.model_validate(value), InferenceTrace(operation, "fixture", "fixture", "fixture", 1, 1, None)


def emit_fixture(db, context, *, relation="context", web=False, stale_at=None):
    body = ("The fixture service does not support private deployment." if relation == "refutes"
            else "The fixture service supports private deployment.")
    content_hash = digest(body)
    if get_cached_content(db, content_hash) is None:
        db.add(WebContent(content_hash=content_hash, body=body, cache_path=""))
        db.flush()
    document_id = chunk_id = url = None
    if web:
        url = "https://qwen.readthedocs.io/en/latest/fixture/" + str(context.task_id)
        source = db.scalar(select(WebSource).where(WebSource.canonical_url == url))
        if source is None:
            db.add(WebSource(canonical_url=url, fetched_url=url, title="Synthetic acceptance fixture",
                             content_hash=content_hash, retrieved_at=datetime.now(timezone.utc),
                             source_type="web", search_queries=["fixture"]))
            db.flush()
    else:
        document = DocumentRecord(filename="synthetic-fixture.md", source="acceptance-fixture", version="1", chunk_count=1)
        db.add(document)
        db.flush()
        chunk = DocumentChunk(document_id=document.id, chunk_index=0, text=body, token_count=12,
                              source="acceptance-fixture", version="1")
        db.add(chunk)
        db.flush()
        document_id, chunk_id = document.id, chunk.id
    evidence = Evidence(evidence_id=uuid4(), claim_id=context.claim["id"], claim_text=context.claim["claim"],
                        source_type="web" if web else "document", source_url=url, document_id=document_id,
                        chunk_id=chunk_id, retrieved_at=datetime.now(timezone.utc), published_at=stale_at,
                        supporting_text=body, relation=relation, content_hash=content_hash,
                        scope={"model_name": "fixture", "model_version": "1", "deployment_mode": "local"},
                        quote_start=0, quote_end=len(body))
    save_evidences(db, [evidence], before_commit=context.check)
    return str(evidence.evidence_id)


def run_scenario(db, name, *, config=None):
    if name not in SCENARIOS:
        raise ValueError("Unknown adaptive scenario")
    calls = []
    model = ControlledClient(name)

    def acquire(db, context, query, top_k=5, web=False):
        calls.append("SEARCH_WEB" if web else "RETRIEVE_RAG")
        identifiers = []
        if name == "missing_evidence" and len(calls) == 2:
            identifiers = [emit_fixture(db, context, web=True)]
        elif name in {"sufficient_evidence", "unverifiable_evidence"} and len(calls) == 1:
            identifiers = [emit_fixture(db, context)]
        elif name == "conflicting_evidence" and len(calls) == 1:
            identifiers = [emit_fixture(db, context, relation="supports"), emit_fixture(db, context, relation="refutes")]
        return ToolResult(ok=bool(identifiers), evidence_ids=identifiers, summary="Controlled acquisition fixture")

    registry = ToolRegistry({"rag_retrieve_tool": acquire,
                             "web_search_tool": lambda db, context, query: acquire(db, context, query, web=True)})
    task = ResearchTask(query="Check fixture private deployment support", budget=1 if name == "budget_exhausted" else 12)
    db.add(task)
    db.commit()
    state = run_agent_task(db, task.id, client=model, registry=registry, **({"config": config} if config else {}))
    steps = list(db.scalars(select(AgentStep).where(AgentStep.task_id == task.id).order_by(AgentStep.step_index)))
    decisions = list(db.scalars(select(AgentPolicyDecision).where(AgentPolicyDecision.task_id == task.id).order_by(AgentPolicyDecision.decision_index)))
    assert state["status"] != "failed", state
    assert state["round_count"] == len(steps) <= 12
    assert state["retrieval_action_count"] <= 6 and max(state["claim_retrieval_counts"].values(), default=0) <= 3
    assert state["query_rewrite_count"] <= 2 and state["remaining_budget"] >= 0
    if name == "missing_evidence":
        assert calls == ["SEARCH_WEB", "SEARCH_WEB"] and state["termination_action"] == "STOP"
        assert decisions[0].proposed_action == "STOP" and decisions[0].selected_action == "SEARCH_WEB"
    elif name == "sufficient_evidence":
        assert len(steps) == 2 and state["coverage"] == 1 and state["termination_action"] == "STOP"
        assert [step.tool_name for step in steps] == ["rag_retrieve_tool", "verify_claim_tool"]
        assert state["coverage_history"][1]["coverage"] == 0  # Retrieved is still unverified.
    elif name == "conflicting_evidence":
        assert steps[1].tool_name == "verify_claim_tool" and decisions[1].reason == "conflict_requires_verification"
        assert state["coverage"] == 0 and state["claim_statuses"]["C1"]["status"] == "CONTRADICTED"
    elif name == "budget_exhausted":
        assert len(steps) == 1 and state["remaining_budget"] == 0 and len(calls) == 1
        assert state["termination_action"] == "ABSTAIN" and state["stop_reason"] == "budget_exhausted"
    elif name == "unverifiable_evidence":
        assert len(steps) == 2 and len(calls) == 1 and state["coverage"] == 0
        assert state["termination_action"] == "ABSTAIN" and state["stop_reason"] == "verification_unavailable"
    else:
        cap = state["limits"]
        expected = min(cap["max_claim_retrievals"], cap["max_retrieval_actions"], cap["max_steps"], 1 + cap["max_query_rewrites"])
        assert state["termination_action"] == "ABSTAIN" and len(steps) == expected and len(calls) == expected
    return {"name": name, "passed": True, "execution": "real_langgraph_and_database_with_controlled_model_and_acquisition",
            "state": state, "operations": model.operations,
            "steps": [{"index": step.step_index, "tool": step.tool_name, "status": step.status} for step in steps],
            "decisions": [{"index": item.decision_index, "proposed": item.proposed_action,
                           "selected": item.selected_action, "reason": item.reason} for item in decisions]}
