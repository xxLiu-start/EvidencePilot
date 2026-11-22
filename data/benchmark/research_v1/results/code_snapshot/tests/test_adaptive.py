from datetime import datetime, timedelta, timezone
from threading import Event
import time
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.agent.graph import AgentRuntime
from app.agent.policy import Action, ActionProposal, evaluate_action, validate_action
from app.agent.state import initial_state
from app.agent.tools import ToolContext
from app.config import Settings
from app.db.models import AgentRun, AgentStep, ClaimRecord, EvidenceAssessment, EvidenceRecord, ResearchTask
from app.db.session import Base
from app.evidence.coverage import calculate_coverage
from app.verification.verifier import assess_claim, perform_verification
from scripts.adaptive_scenarios import ControlledClient, SCENARIOS, emit_fixture, run_scenario


@pytest.fixture
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'adaptive.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


def claim_context(db, model=None, **overrides):
    claim = {"id": "C1", "claim": "The fixture service supports private deployment.",
             "priority": "high", "required_source": "any", "weight": 3., **overrides}
    task = ResearchTask(query="Check fixture", budget=12)
    record = ClaimRecord(claim_id=claim["id"], claim_text=claim["claim"])
    db.add_all([task, record])
    db.commit()
    return ToolContext(task.id, claim, model or ControlledClient("sufficient_evidence"),
                       time.monotonic() + 20, Event())


def state_with(status=None, **updates):
    state = initial_state(uuid4(), "test", 12)
    state["claims"] = [{"id": "C1", "claim": "Test claim", "priority": "high", "required_source": "any", "weight": 3.}]
    state["claim_statuses"] = {"C1": status or {"status": "NOT_RETRIEVED"}}
    state.update(updates)
    return state


def covered(**changes):
    return {"status": "SUPPORTED", "verified": True, "fresh": True,
            "source_requirements_met": True, "has_conflict": False, **changes}


def test_weighted_coverage_requires_verified_qualified_fresh_support():
    assert calculate_coverage([{**covered(), "weight": 3}, {"status": "RETRIEVED", "weight": 2}]) == .6
    assert calculate_coverage([]) == 0
    assert calculate_coverage([{**covered(), "weight": 0}]) == 0
    for changes in ({"verified": False}, {"source_requirements_met": False}, {"fresh": False},
                    {"has_conflict": True}, {"status": "CONTRADICTED"}, {"status": "OUTDATED"}):
        assert calculate_coverage([{**covered(**changes), "weight": 1}]) == 0
    assert calculate_coverage([{"status": "SUPPORTED", "weight": 1}]) == 0
    for weight in (-1, float("nan"), float("inf")):
        with pytest.raises(ValueError):
            calculate_coverage([{**covered(), "weight": weight}])


@pytest.mark.parametrize("name", SCENARIOS)
def test_six_adaptive_acceptance_scenarios(db, name):
    assert run_scenario(db, name)["passed"]


def test_retrieved_text_requires_explicit_verification_and_reuses_fingerprint(db):
    context = claim_context(db)
    claim = dict(context.claim)
    assert assess_claim(db, claim, [])["status"] == "NOT_RETRIEVED"
    assert assess_claim(db, claim, [], retrieval_attempts=1)["status"] == "INSUFFICIENT"
    identifier = emit_fixture(db, context, relation="supports")
    before = assess_claim(db, claim, [identifier], task_id=context.task_id)
    assert before["status"] == "RETRIEVED" and not before["verified"] and before["can_verify"]
    context.claim = {**claim, "evidence_ids": [identifier]}
    after = perform_verification(db, context)
    assert after["status"] == "SUPPORTED" and after["verified"] and not after["can_verify"]
    assert calculate_coverage([{**after, "weight": 3}]) == 1
    operations = len(context.client.operations)
    assert perform_verification(db, context)["status"] == "SUPPORTED"
    assert len(context.client.operations) == operations
    # A changed evidence set invalidates the old semantic verdict.
    new = emit_fixture(db, context, relation="refutes")
    refreshed = assess_claim(db, claim, [identifier, new], task_id=context.task_id)
    assert refreshed["potential_conflict"] and refreshed["can_verify"] and not refreshed["verified"]


def test_stale_and_future_evidence_never_counts_as_coverage(db):
    context = claim_context(db)
    for stamp in (datetime.now(timezone.utc) - timedelta(days=400), datetime.now(timezone.utc) + timedelta(days=3)):
        identifier = emit_fixture(db, context, stale_at=stamp)
        status = assess_claim(db, context.claim, [identifier], task_id=context.task_id)
        assert status["status"] == "OUTDATED" and not status["can_verify"]
        assert calculate_coverage([{**status, "weight": 1}]) == 0


def test_source_requirements_need_both_internal_and_external(db):
    context = claim_context(db, required_source="official", needs_internal=True)
    doc = emit_fixture(db, context)
    partial = assess_claim(db, context.claim, [doc])
    assert partial["status"] == "INSUFFICIENT" and partial["missing_external"] and not partial["missing_internal"]
    web = emit_fixture(db, context, web=True)
    context.claim = {**context.claim, "evidence_ids": [doc, web]}
    after = perform_verification(db, context)
    assert after["status"] == "SUPPORTED" and after["source_requirements_met"]


def test_model_cannot_override_wrong_source_requirement(db):
    context = claim_context(db, required_source="official")
    doc = emit_fixture(db, context)
    context.claim = {**context.claim, "evidence_ids": [doc]}
    result = perform_verification(db, context)
    assert result["status"] == "INSUFFICIENT" and not result["enough"]
    assert result["verification_error"] == "source_requirement_not_met"


def test_fabricated_verification_ids_are_rejected(db):
    class BadVerifier(ControlledClient):
        def complete_json(self, messages, output_type, **kwargs):
            if kwargs["operation"] == "evidence_verification":
                return output_type(verdict="SUPPORTED", supporting_evidence_ids=[uuid4()], reason="fake ID"), None
            return super().complete_json(messages, output_type, **kwargs)

    context = claim_context(db, BadVerifier("sufficient_evidence"))
    context.claim = {**context.claim, "evidence_ids": [emit_fixture(db, context)]}
    status = perform_verification(db, context)
    assert status["verification_error"] == "unverifiable_verdict" and not status["enough"]


def test_wrong_claim_or_changed_original_quote_is_not_valid_evidence(db):
    context = claim_context(db)
    identifier = emit_fixture(db, context)
    assert assess_claim(db, {**context.claim, "claim": "An unrelated claim"}, [identifier])["evidence_ids"] == []
    row = db.get(EvidenceRecord, UUID(identifier))
    row.supporting_text = "A fabricated quote outside the immutable original source."
    db.commit()
    assert assess_claim(db, context.claim, [identifier])["evidence_ids"] == []


@pytest.mark.parametrize("proposal", ["SEARCH_WEB", "RETRIEVE_RAG", "VERIFY", "STOP", "ABSTAIN"])
def test_hard_stop_and_budget_override_every_proposal(proposal):
    assert validate_action(state_with(covered()), proposal) == "STOP"
    assert validate_action(state_with(remaining_budget=0), proposal) == "ABSTAIN"


def test_conflict_verification_takes_precedence_over_proposed_retrieval():
    state = state_with({"status": "CONTRADICTED", "has_conflict": True, "can_verify": True})
    decision = evaluate_action(state, "SEARCH_WEB")
    assert decision.action == Action.VERIFY and decision.target_claim_id == "C1"


@pytest.mark.parametrize("counter", [{"retrieval_action_count": 6}, {"claim_retrieval_counts": {"C1": 3}},
                                    {"policy_step_count": 12}])
def test_rule_caps_cannot_be_bypassed_by_model(counter):
    assert validate_action(state_with(**counter), "SEARCH_WEB") == "ABSTAIN"


def test_missing_source_routes_to_required_tool_and_cached_source():
    state = state_with({"missing_internal": True})
    assert validate_action(state, "SEARCH_WEB") == "RETRIEVE_RAG"
    state = state_with({"missing_external": True})
    assert validate_action(state, "RETRIEVE_RAG") == "SEARCH_WEB"
    state["candidate_sources"] = [{"source_id": "known", "url": "https://example.com/", "source_class": "web"}]
    decision = evaluate_action(state, "STOP")
    assert decision.action == Action.OPEN_SOURCE and decision.arguments == {"url": "https://example.com/"}
    assert validate_action(state, "DELETE_DATABASE") == "ABSTAIN"


@pytest.mark.parametrize("settings,expected", [({"agent_max_query_rewrites": 0}, 1), ({"agent_max_steps": 2}, 2)])
def test_query_rewrite_and_max_step_limits_can_be_lowered(db, settings, expected):
    result = run_scenario(db, "bounded_loop", config=Settings(_env_file=None, **settings))
    assert result["state"]["round_count"] == expected


def test_executor_rechecks_budget_before_any_dispatch(db):
    context = claim_context(db)
    state = state_with(task_id=str(context.task_id), remaining_budget=0, selected_action="SEARCH_WEB",
                       selected_tool="web_search_tool", current_claim_id="C1", action_args={"query": "fixture evidence"})
    state["claims"] = [context.claim]
    db.add(AgentRun(task_id=context.task_id, state=state, updated_at=datetime.now(timezone.utc)))
    db.commit()
    after = AgentRuntime(db, context.client).tool_executor(state)
    assert after["termination_action"] == "ABSTAIN" and after["round_count"] == 0
    assert db.scalar(select(func.count()).select_from(AgentStep)) == 0
