"""Small, JSON-serializable LangGraph state. Full source bodies stay in SQL."""

from typing import Any, TypedDict


class AgentState(TypedDict):
    task_id: str
    user_query: str
    claims: list[dict]
    evidence_ids: list[str]
    source_ids: list[str]
    selected_action: str | None
    current_claim_id: str | None
    action_args: dict[str, Any]
    pending_observation: dict | None
    search_count: int
    retrieval_count: int
    remaining_budget: int
    round_count: int
    claim_statuses: dict[str, dict]
    step_summaries: list[dict]
    final_report: str | None
    status: str
    stop_reason: str | None
    last_error: str | None
    planner_repaired: bool
    router_trace: dict | None
    algorithm_version: str
    coverage: float
    coverage_history: list[dict]
    policy_step_count: int
    retrieval_action_count: int
    claim_retrieval_counts: dict[str, int]
    query_rewrite_count: int
    query_history: dict[str, list[dict]]
    opened_source_ids: dict[str, list[str]]
    limits: dict[str, int]
    selected_tool: str | None
    proposed_action: str | None
    policy_reason: str | None
    termination_action: str | None
    reflections: list[dict]
    applied_step_indices: list[int]
    recovery_count: int
    recovery_node: str | None


def initial_state(task_id, user_query, budget, config=None) -> AgentState:
    from app.config import settings
    config = config or settings
    return dict(task_id=str(task_id), user_query=user_query, claims=[], evidence_ids=[], source_ids=[],
                selected_action=None, current_claim_id=None, action_args={}, pending_observation=None,
                search_count=0, retrieval_count=0, remaining_budget=budget, round_count=0,
                claim_statuses={}, step_summaries=[], final_report=None, status="running",
                stop_reason=None, last_error=None, planner_repaired=False, router_trace=None,
                algorithm_version="adaptive-v1", coverage=0.0, coverage_history=[], policy_step_count=0,
                retrieval_action_count=0, claim_retrieval_counts={}, query_rewrite_count=0, query_history={},
                opened_source_ids={}, selected_tool=None, proposed_action=None, policy_reason=None,
                termination_action=None, limits={"max_retrieval_actions": config.agent_max_retrieval_actions,
                    "max_claim_retrievals": config.agent_max_claim_retrievals,
                    "max_query_rewrites": config.agent_max_query_rewrites, "max_steps": config.agent_max_steps},
                reflections=[], applied_step_indices=[], recovery_count=0, recovery_node=None)
