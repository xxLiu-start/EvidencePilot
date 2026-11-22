from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from pydantic import BaseModel

from app.agent.policy import Action, ActionProposal
from app.evaluation.baselines import StateBlindedClient, no_stopping_policy
from app.evaluation.research_dataset import load_research_dataset
from app.evaluation.research_metrics import calculate_metrics, grade_answer
from app.llm_client import ReasoningClient
from app.config import Settings
from app.agent.state import initial_state
from app.evaluation.research_benchmark import identity_matches
from uuid import uuid4


def fixture():
    ref = {"source_id": "source", "supporting_text": "The model emits exactly 1024 vector dimensions."}
    task = {"allowed_source_ids": ["source"], "required_claims": [{"id": "C1", "expected_status": "SUPPORTED",
        "answer_patterns": ["1024"], "reference_evidence": [ref], "required_source_groups": [["source"]]}]}
    pages = [{"source_id": "source"}]
    chunks = [{"id": "chunk", "source_id": "source", "text": ref["supporting_text"] + " A related but unhelpful paragraph."}]
    answer = {"claims": [{"claim_id": "C1", "status": "SUPPORTED", "answer": "1024 dimensions",
                          "citations": [{"evidence_id": "chunk", "supporting_text": ref["supporting_text"]}]}]}
    return task, pages, chunks, answer


def test_frozen_twenty_tasks_and_all_reference_bindings():
    manifest, sources, tasks, chunks = load_research_dataset("data/benchmark/research_v1")
    assert len(tasks) == 20 and len(sources) >= 10 and chunks
    assert manifest["human_reviewed"] == (manifest["annotation_status"] == "human_reviewed")


@pytest.mark.parametrize("mutation", ["fake_id", "fake_quote", "unrelated_quote", "wrong_number", "missing_citation", "wrong_status"])
def test_grader_rejects_unsupported_assertions(mutation):
    task, pages, chunks, answer = fixture()
    item = answer["claims"][0]
    if mutation == "fake_id":
        item["citations"][0]["evidence_id"] = "invented"
    elif mutation == "fake_quote":
        item["citations"][0]["supporting_text"] = "The model emits 2048 vector dimensions."
    elif mutation == "unrelated_quote":
        item["citations"][0]["supporting_text"] = "A related but unhelpful paragraph."
    elif mutation == "wrong_number":
        item["answer"] = "2048 dimensions"
    elif mutation == "missing_citation":
        item["citations"] = []
    else:
        item["status"] = "INSUFFICIENT"
    graded = grade_answer(task, answer, pages, chunks)
    assert not graded["task_correct"]
    if mutation != "wrong_status":
        assert graded["counts"]["unsupported_claims"] == 1


def test_metrics_aggregate_actual_counts_and_unknown_token_usage():
    task, pages, chunks, answer = fixture()
    grade = grade_answer(task, answer, pages, chunks)
    row = {"grading": grade, "retrieval_calls": 2, "latency_seconds": 3, "token_cost": 100}
    metrics = calculate_metrics([row])
    assert metrics["answer_accuracy"] == metrics["citation_precision"] == metrics["evidence_coverage"] == 1
    assert metrics["unsupported_claim_rate"] == 0 and metrics["avg_token_cost"] == 100
    assert metrics["avg_latency"] == 3 and metrics["avg_retrieval_calls"] == 2
    assert calculate_metrics([row, {**row, "token_cost": None}])["avg_token_cost"] is None
    with pytest.raises(ValueError):
        calculate_metrics([])


def test_extra_assertions_count_and_missing_answers_do_not_get_abstention_credit():
    task, pages, chunks, answer = fixture()
    answer["claims"].append({"claim_id": "C99", "status": "SUPPORTED", "answer": "Invented", "citations": []})
    result = grade_answer(task, answer, pages, chunks)
    assert result["counts"]["unsupported_claims"] == 1
    assert not result["task_correct"]
    assert not grade_answer(task, {"claims": []}, pages, chunks)["task_correct"]


def test_no_state_ablation_removes_state_and_coverage_from_llm_input():
    captured = []
    class Client:
        config = SimpleNamespace(llm_mode="local")
        def complete_json(self, messages, output_type, **kwargs):
            captured.extend(messages)
            return None, None
    client = StateBlindedClient(Client())
    client.complete_json([{"role": "user", "content": json.dumps({"user_query": "test", "claim_statuses": {"C1": "SUPPORTED"},
        "coverage": 1, "claims": [], "remaining_budget": 3})}], BaseModel, operation="agent_router")
    assert "claim_statuses" not in captured[-1]["content"] and '"coverage"' not in captured[-1]["content"]


def test_stopping_ablation_keeps_hard_budget_but_removes_sufficiency_stop():
    state = initial_state(uuid4(), "test", 12)
    state["claims"] = [{"id": "C1", "claim": "A claim", "priority": "high"}]
    state["claim_statuses"] = {"C1": {"status": "SUPPORTED", "verified": True, "source_requirements_met": True, "fresh": True}}
    assert no_stopping_policy(state, ActionProposal(action="STOP")).action == Action.SEARCH_WEB
    state["remaining_budget"] = 0
    assert no_stopping_policy(state, ActionProposal(action="STOP")).action == Action.ABSTAIN


def test_token_accounting_uses_api_usage_not_an_estimate():
    class Answer(BaseModel):
        ok: bool
    cfg = Settings(_env_file=None, database_url="sqlite://", llm_local_base_url="http://127.0.0.1:8010/v1", llm_local_model="test", credentials_env_file="/tmp/no-ep-credential")
    def handler(request):
        return httpx.Response(200, request=request, json={"id": "test", "object": "chat.completion", "created": 0,
            "model": "test", "system_fingerprint": "server-fingerprint", "usage": {"prompt_tokens": 17, "completion_tokens": 11, "total_tokens": 28},
            "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": '{"ok":true}'}}]})
    traces = []
    client = ReasoningClient(cfg, transport=httpx.MockTransport(handler), trace_sink=traces.append)
    try:
        answer, trace = client.complete_json([{"role": "user", "content": "test"}], Answer, operation="token_test")
    finally:
        client.close()
    assert trace.total_tokens == traces[0]["total_tokens"] == 28
    assert trace.prompt_tokens == 17 and trace.completion_tokens == 11 and trace.system_fingerprint == "server-fingerprint"


def test_inference_epoch_guard_rejects_same_name_with_changed_fingerprint():
    assert identity_matches([{"actual_model": "model", "system_fingerprint": "epoch1"}], "model", "epoch1")
    assert not identity_matches([{"actual_model": "model", "system_fingerprint": "epoch2"}], "model", "epoch1")
    assert not identity_matches([{"actual_model": "other", "system_fingerprint": "epoch1"}], "model", "epoch1")
