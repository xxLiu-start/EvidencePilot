"""Deterministic safety/termination rules after the LLM action proposal."""

from enum import StrEnum
import re

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.evidence.coverage import all_critical_claims_supported, is_covered


class Action(StrEnum):
    SEARCH_WEB = "SEARCH_WEB"
    RETRIEVE_RAG = "RETRIEVE_RAG"
    OPEN_SOURCE = "OPEN_SOURCE"
    VERIFY = "VERIFY"
    STOP = "STOP"
    ABSTAIN = "ABSTAIN"


ACTIONS = [action.value for action in Action]
ACTION_TO_TOOL = {Action.SEARCH_WEB: "web_search_tool", Action.RETRIEVE_RAG: "rag_retrieve_tool",
                  Action.OPEN_SOURCE: "open_source_tool", Action.VERIFY: "verify_claim_tool"}
TOOL_TO_ACTION = {value: key.value for key, value in ACTION_TO_TOOL.items()}


class ActionProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Action
    target_claim_id: str | None = Field(default=None, pattern=r"^C[1-9][0-9]{0,2}$")
    reason: str = Field(default="", max_length=300)
    arguments: dict = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def legacy_action(cls, value):
        if isinstance(value, dict) and "tool" in value and "action" not in value:
            value = {**value, "action": TOOL_TO_ACTION.get(value["tool"], value["tool"]),
                     "target_claim_id": value.get("claim_id")}
            value.pop("tool", None)
            value.pop("claim_id", None)
        return value


class PolicyDecision(BaseModel):
    action: Action
    target_claim_id: str | None = None
    reason: str
    arguments: dict = Field(default_factory=dict)
    overridden: bool = False


def normalize_query(query):
    return re.sub(r"\W+", " ", query.casefold()).strip()


def limits(state):
    configured = state.get("limits", {})
    return {key: min(configured.get(key, cap), cap) for key, cap in {
        "max_retrieval_actions": 6, "max_claim_retrievals": 3, "max_query_rewrites": 2, "max_steps": 12}.items()}


def _remaining_claims(state):
    return sorted((claim for claim in state.get("claims", []) if not is_covered(
        state.get("claim_statuses", {}).get(claim["id"], {}))),
        key=lambda claim: ({"high": 0, "medium": 1, "low": 2}.get(claim.get("priority"), 1), claim["id"]))


def _can_retrieve(state, claim_id):
    cap = limits(state)
    return (state.get("retrieval_action_count", 0) < cap["max_retrieval_actions"]
            and state.get("claim_retrieval_counts", {}).get(claim_id, 0) < cap["max_claim_retrievals"])


def _pending_sources(state, claim):
    opened = set(state.get("opened_source_ids", {}).get(claim["id"], []))
    required = claim.get("required_source", "any")
    return [source for source in state.get("candidate_sources", [])
            if source["source_id"] not in opened
            and (required not in {"official", "academic"} or source.get("source_class") == required)]


def evaluate_action(state, proposal):
    if isinstance(proposal, str):
        try:
            proposal = ActionProposal(action=proposal)
        except ValueError:
            return PolicyDecision(action=Action.ABSTAIN, reason="invalid_action")
    elif not isinstance(proposal, ActionProposal):
        try:
            proposal = ActionProposal.model_validate(proposal)
        except ValueError:
            return PolicyDecision(action=Action.ABSTAIN, reason="invalid_action")

    def decide(action, target=None, reason="proposal_accepted", arguments=None):
        if action in {Action.STOP, Action.ABSTAIN}:
            arguments = {}
        return PolicyDecision(action=action, target_claim_id=target, reason=reason,
                              arguments=arguments if arguments is not None else (
                                  proposal.arguments if proposal.target_claim_id in {None, target} else {}),
                              overridden=action != proposal.action or target != proposal.target_claim_id)

    if all_critical_claims_supported(state):
        return decide(Action.STOP, reason="critical_claims_supported", arguments={})
    if state.get("remaining_budget", 0) <= 0:
        return decide(Action.ABSTAIN, reason="budget_exhausted", arguments={})
    if state.get("policy_step_count", 0) >= limits(state)["max_steps"]:
        return decide(Action.ABSTAIN, reason="max_steps", arguments={})
    remaining = _remaining_claims(state)
    if not remaining:
        return decide(Action.ABSTAIN, reason="no_verifiable_claims", arguments={})
    statuses = state.get("claim_statuses", {})
    if any(statuses.get(claim["id"], {}).get("human_review_required")
           and statuses.get(claim["id"], {}).get("verification_attempted")
           and not statuses.get(claim["id"], {}).get("human_review_approved") for claim in remaining):
        return decide(Action.ABSTAIN, reason="human_review_required", arguments={})
    if any(statuses.get(claim["id"], {}).get("verification_error") for claim in remaining):
        return decide(Action.ABSTAIN, reason="verification_unavailable", arguments={})
    conflicts = [claim for claim in remaining if statuses.get(claim["id"], {}).get("has_conflict") or statuses.get(claim["id"], {}).get("potential_conflict")
                 or statuses.get(claim["id"], {}).get("status") == "CONTRADICTED"]
    conflict = next((claim for claim in conflicts if statuses.get(claim["id"], {}).get("can_verify")), None)
    if conflict:
        return decide(Action.VERIFY, conflict["id"], "conflict_requires_verification", {"claim_id": conflict["id"]})
    if proposal.target_claim_id and proposal.target_claim_id not in {claim["id"] for claim in state["claims"]}:
        return decide(Action.ABSTAIN, reason="unknown_claim_id", arguments={})
    claim = next((item for item in remaining if item["id"] == proposal.target_claim_id), remaining[0])
    current = statuses.get(claim["id"], {})
    target = claim["id"]
    if current.get("verification_error"):
        return decide(Action.ABSTAIN, target, "verification_unavailable", {})
    if current.get("can_verify") and current.get("source_requirements_met"):
        return decide(Action.VERIFY, target, "verify_before_more_acquisition", {"claim_id": target})

    internal_missing = current.get("missing_internal", claim.get("required_source") == "internal" or claim.get("needs_internal", False))
    external_missing = current.get("missing_external", claim.get("required_source") in {"official", "academic"} or claim.get("needs_external", False))
    pending = _pending_sources(state, claim)
    can_retrieve = _can_retrieve(state, target)
    if internal_missing:
        return decide(Action.RETRIEVE_RAG if can_retrieve else Action.ABSTAIN, target,
                      "missing_internal_evidence" if can_retrieve else "claim_or_task_retrieval_limit",
                      proposal.arguments if proposal.action == Action.RETRIEVE_RAG and proposal.target_claim_id in {None, target} else {})
    if external_missing:
        if pending:
            return decide(Action.OPEN_SOURCE, target, "open_cached_external_source", {"url": pending[0]["url"]})
        return decide(Action.SEARCH_WEB if can_retrieve else Action.ABSTAIN, target,
                      "missing_external_evidence" if can_retrieve else "claim_or_task_retrieval_limit",
                      proposal.arguments if proposal.action == Action.SEARCH_WEB and proposal.target_claim_id in {None, target} else {})
    if proposal.action == Action.OPEN_SOURCE:
        selected = next((source for source in pending if source["url"] == proposal.arguments.get("url")), None)
        if selected:
            return decide(Action.OPEN_SOURCE, target)
        if pending:
            return decide(Action.OPEN_SOURCE, target, "choose_task_authorized_source", {"url": pending[0]["url"]})
    if proposal.action in {Action.SEARCH_WEB, Action.RETRIEVE_RAG} and can_retrieve:
        return decide(proposal.action, target)
    if pending:
        return decide(Action.OPEN_SOURCE, target, "use_existing_cached_evidence", {"url": pending[0]["url"]})
    if can_retrieve:
        tried = state.get("query_history", {}).get(target, [])
        action = Action.SEARCH_WEB if any(item["action"] == Action.RETRIEVE_RAG for item in tried) else Action.RETRIEVE_RAG
        return decide(action, target, "evidence_insufficient_continue", {})
    return decide(Action.ABSTAIN, target, "unable_to_verify_within_limits", {})


def validate_action(state, proposed_action):
    return evaluate_action(state, proposed_action).action.value
