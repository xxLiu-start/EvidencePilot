"""A bounded LangGraph agent with persisted state and audited tool attempts."""

from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import asdict
from datetime import datetime, timezone
import json
from threading import Event
import time
from uuid import UUID, uuid4

from langgraph.graph import END, START, StateGraph
from pydantic import ValidationError
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.agent.planner import decompose_claims
from app.agent.reflection import reflect_query
from app.agent.errors import AgentConflict, LeaseLost
from app.agent.router import candidate_sources, route_action
from app.agent.policy import ACTION_TO_TOOL, Action, ActionProposal, PolicyDecision, evaluate_action, normalize_query
from app.agent.state import AgentState, initial_state
from app.agent.tools import ToolContext, ToolDeadline, ToolRegistry, ToolResult
from app.config import settings
from app.domains.profiles import get_domain_profile
from app.db.models import AgentAllowedSource, AgentPolicyDecision, AgentRun, AgentStep, ClaimRecord, EvidenceRecord, ResearchTask, ToolExecutionReceipt, WebSource
from app.evidence.coverage import PRIORITY_WEIGHTS, calculate_coverage, coverage_items, is_covered
from app.llm_client import LLMError, get_llm_client
from app.telemetry import audit_logger
from app.verification.verifier import assess_claim
from app.memory.checkpoints import latest_snapshot, lease_deadline, reserve_recovery, save_checkpoint


logger = audit_logger(__name__)


class AgentRuntime:
    def __init__(self, db, client, *, registry=None, config=settings, owner_token=None, policy=None, reference_time=None):
        self.db, self.client, self.config = db, client, config
        self.owner_token, self.cursor = owner_token, "planner"
        self.policy, self.reference_time = policy or evaluate_action, reference_time
        bind = db.get_bind()
        self.engine = getattr(bind, "engine", bind)
        self.registry = registry if registry is not None else ToolRegistry()

    def save(self, state, next_node=None):
        self.cursor = next_node or self.cursor
        return save_checkpoint(self.db, state, self.cursor, owner_token=self.owner_token, config=self.config)

    def planner(self, state):
        try:
            profile = get_domain_profile(state.get("domain_id", "general_research"))
            plan, repaired, _ = decompose_claims(state["user_query"], client=self.client, profile=profile)
            claims = [{**claim.model_dump(mode="json"), "weight": PRIORITY_WEIGHTS[claim.priority.value]} for claim in plan.claims]
            for claim in claims:
                record = self.db.scalar(select(ClaimRecord).where(
                    ClaimRecord.claim_id == claim["id"], ClaimRecord.claim_text == claim["claim"]
                ))
                if record is None:
                    self.db.add(ClaimRecord(claim_id=claim["id"], claim_text=claim["claim"]))
            self.db.commit()
            state = {**state, "claims": claims, "planner_repaired": repaired,
                     "claim_statuses": {c["id"]: {"evidence_ids": []} for c in claims}}
            state = self.refresh(state)
        except LLMError as exc:
            state = {**state, "status": "failed", "last_error": exc.category,
                     "stop_reason": "planner_failed", "termination_action": "ABSTAIN"}
        return self.save(state, "reporter" if state["status"] == "failed" else "router")

    def refresh(self, state):
        statuses = {claim["id"]: assess_claim(
            self.db, claim, state["claim_statuses"].get(claim["id"], {}).get("evidence_ids", []),
            task_id=state["task_id"], retrieval_attempts=state["claim_retrieval_counts"].get(claim["id"], 0),
            config=self.config, now=self.reference_time) for claim in state["claims"]}
        updated = {**state, "claim_statuses": statuses}
        coverage = calculate_coverage(coverage_items(updated))
        point = {"round": state["round_count"], "coverage": coverage,
                 "statuses": {key: value["status"] for key, value in statuses.items()}}
        history = state["coverage_history"]
        if not history or history[-1] != point:
            history = [*history, point][-14:]
        return {**updated, "coverage": coverage, "coverage_history": history}

    def materialize(self, state, decision):
        claim = next(c for c in state["claims"] if c["id"] == decision.target_claim_id)
        arguments = dict(decision.arguments)
        if decision.action in {Action.SEARCH_WEB, Action.RETRIEVE_RAG}:
            default = claim["claim"]
            if decision.action == Action.SEARCH_WEB and claim["required_source"] == "official":
                default = "official documentation " + default
            arguments.setdefault("query", default[:600])
            prior = [item for item in state["query_history"].get(claim["id"], []) if item["action"] == decision.action]
            if prior:
                if state["query_rewrite_count"] >= state["limits"]["max_query_rewrites"]:
                    return state, PolicyDecision(action=Action.ABSTAIN, target_claim_id=claim["id"], reason="query_rewrite_limit")
                state = {**state, "query_rewrite_count": state["query_rewrite_count"] + 1}
                self.save(state)  # Reserve a rewrite before a possible model call.
                arguments["query"], reflection = reflect_query(self.db, state, claim, decision.action.value,
                                                               arguments["query"], prior, self.client)
                state = {**state, "reflections": [*state.get("reflections", []), reflection][-2:]}
                if normalize_query(arguments["query"]) in {item["normalized"] for item in prior}:
                    return state, PolicyDecision(action=Action.ABSTAIN, target_claim_id=claim["id"], reason="query_rewrite_failed")
        if decision.action == Action.VERIFY:
            arguments = {"claim_id": claim["id"]}
        params = self.registry.validate(ACTION_TO_TOOL[decision.action], arguments)
        return state, decision.model_copy(update={"arguments": params.model_dump(mode="json")})

    def router(self, state):
        state = self.refresh(state)
        snapshot = {**state, "candidate_sources": candidate_sources(self.db, state)}
        trace, proposal = None, ActionProposal(action=Action.STOP)
        preflight = self.policy(snapshot, proposal)
        try:
            if preflight.action in {Action.STOP, Action.ABSTAIN}:
                decision = preflight
            else:
                proposal, trace = route_action(self.db, state, self.client)
                decision = self.policy(snapshot, proposal)
            if state["policy_step_count"] < state["limits"]["max_steps"]:
                state = {**state, "policy_step_count": state["policy_step_count"] + 1}
            if decision.action not in {Action.STOP, Action.ABSTAIN}:
                state, decision = self.materialize(state, decision)
        except (LLMError, ValidationError, ValueError, TypeError, KeyError) as exc:
            decision = PolicyDecision(action=Action.ABSTAIN, reason="invalid_proposal_or_arguments")
            state = {**state, "last_error": getattr(exc, "category", "invalid_action_schema")}
        terminal = decision.action in {Action.STOP, Action.ABSTAIN}
        index = state["policy_step_count"] or 1
        record = self.db.get(AgentPolicyDecision, (UUID(state["task_id"]), index))
        if record is None:
            self.db.add(AgentPolicyDecision(
                task_id=UUID(state["task_id"]), decision_index=index, proposed_action=proposal.action.value,
                selected_action=decision.action.value, target_claim_id=decision.target_claim_id, reason=decision.reason,
                snapshot={"remaining_budget": state["remaining_budget"], "coverage": state["coverage"],
                          "retrieval_action_count": state["retrieval_action_count"],
                          "claim_retrieval_counts": state["claim_retrieval_counts"],
                          "query_rewrite_count": state["query_rewrite_count"],
                          "statuses": {key: value["status"] for key, value in state["claim_statuses"].items()}},
                created_at=datetime.now(timezone.utc)))
        return self.save({**state, "selected_action": decision.action.value,
                          "selected_tool": ACTION_TO_TOOL.get(decision.action), "current_claim_id": decision.target_claim_id,
                          "action_args": {} if terminal else decision.arguments, "proposed_action": proposal.action.value,
                          "policy_reason": decision.reason, "router_trace": asdict(trace) if trace else None,
                          "termination_action": decision.action.value if terminal else None,
                          "stop_reason": decision.reason if terminal else None}, "reporter" if terminal else "tool_executor")

    def work(self, name, params, context):
        # No Session is shared with the parent or with another tool thread.
        with Session(self.engine, autoflush=False) as db:
            try:
                result = self.registry.call(name, params, db, context)
                # Generic tools may commit intermediate effects; missing receipts are
                # deliberately treated as unknown on recovery, never blindly replayed.
                context.record_result(db, result)
                db.commit()
                return result
            except Exception:
                db.rollback()
                raise

    def tool_executor(self, state):
        # Recheck immediately before dispatch; persisted/model state cannot bypass policy.
        snapshot = {**self.refresh(state), "candidate_sources": candidate_sources(self.db, state),
                    "policy_step_count": max(0, state["policy_step_count"] - 1)}
        checked = self.policy(snapshot, ActionProposal(action=state["selected_action"],
            target_claim_id=state["current_claim_id"], arguments=state["action_args"]))
        if checked.action.value != state["selected_action"] or checked.target_claim_id != state["current_claim_id"]:
            terminal = checked.action if checked.action in {Action.STOP, Action.ABSTAIN} else Action.ABSTAIN
            return self.save({**state, "stop_reason": checked.reason, "termination_action": terminal.value,
                              "selected_action": terminal.value, "selected_tool": None, "action_args": {}})
        state = {**state, "remaining_budget": state["remaining_budget"] - 1,
                 "round_count": state["round_count"] + 1}
        name, claim_id = state["selected_tool"], state["current_claim_id"]
        if state["selected_action"] in {Action.SEARCH_WEB, Action.RETRIEVE_RAG}:
            counts = dict(state["claim_retrieval_counts"])
            counts[claim_id] = counts.get(claim_id, 0) + 1
            history = json.loads(json.dumps(state["query_history"]))
            history.setdefault(claim_id, []).append({"action": state["selected_action"], "query": state["action_args"]["query"],
                                                      "normalized": normalize_query(state["action_args"]["query"])})
            state = {**state, "retrieval_action_count": state["retrieval_action_count"] + 1,
                     "claim_retrieval_counts": counts, "query_history": history}
        step = AgentStep(task_id=UUID(state["task_id"]), step_index=state["round_count"],
                         claim_id=claim_id, tool_name=name if name in self.registry.schemas else "invalid_action",
                         arguments={}, status="validating", elapsed_ms=0, created_at=datetime.now(timezone.utc),
                         trace={"inference": state["router_trace"], "mode": state["router_trace"].get("mode") if state["router_trace"] else None,
                                "actual_model": state["router_trace"].get("actual_model") if state["router_trace"] else None,
                                "policy": {"proposed_action": state["proposed_action"], "selected_action": state["selected_action"], "reason": state["policy_reason"]}})
        self.db.add(step)
        self.save(state, "recover_tool")  # Budget + attempt + checkpoint commit atomically.
        started, executed, stop_reason = time.perf_counter(), False, state["stop_reason"]
        status, context = "rejected", None
        try:
            params = self.registry.validate(name, state["action_args"])
            claim = next((c for c in state["claims"] if c["id"] == claim_id), None)
            if claim is None:
                raise ValueError("Unknown selected claim")
            if name == "verify_claim_tool" and params.claim_id != claim_id:
                raise ValueError("Claim mismatch")
            step.arguments = params.model_dump(mode="json")
            step.status = "running"
            self.db.commit()
            context = ToolContext(UUID(state["task_id"]),
                                  {**claim, "evidence_ids": list(state["claim_statuses"][claim_id]["evidence_ids"])},
                                  self.client, time.monotonic() + self.config.agent_tool_timeout_seconds,
                                  Event(), self.config, step_id=step.id, owner_token=self.owner_token,
                                  reference_time=self.reference_time)
            executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="evidencepilot-tool")
            future = executor.submit(self.work, name, params, context)
            executed = True
            try:
                result = future.result(timeout=self.config.agent_tool_timeout_seconds)
                status = "success" if result.ok else "empty"
            finally:
                executor.shutdown(wait=False, cancel_futures=True)
        except (FutureTimeout, ToolDeadline):
            if context is not None:
                context.cancelled.set()
            result = ToolResult(ok=False, summary="Tool timed out; no further actions will be started.", error_category="timeout")
            status, stop_reason = "timeout", "tool_timeout"
        except (ValidationError, ValueError):
            result = ToolResult(ok=False, summary="Tool arguments, selected claim or source permission were rejected.",
                                error_category="invalid_tool_arguments")
            status = "rejected"
        except Exception as exc:
            result = ToolResult(ok=False, summary="Tool failed; inspect its sanitized error category.",
                                error_category=getattr(exc, "category", type(exc).__name__))
            status = "failed"
        elapsed = (time.perf_counter() - started) * 1000
        step.status, step.elapsed_ms, step.error_category = status, elapsed, result.error_category
        step.observation = result.model_dump(mode="json")
        observation = {"tool": step.tool_name, "step_index": step.step_index,
                       "executed": executed, "result": step.observation}
        logger.info("agent_tool_call %s", json.dumps({"task_id": state["task_id"], "step_index": step.step_index,
                                                     "tool": step.tool_name, "status": status,
                                                     "elapsed_ms": elapsed, "error_category": result.error_category}))
        return self.save({**state, "pending_observation": observation, "stop_reason": stop_reason,
                          "last_error": result.error_category}, "state_updater")

    def state_updater(self, state):
        observation = state["pending_observation"]
        if observation is None:
            return state
        if observation["step_index"] in state.get("applied_step_indices", []):
            return self.save({**state, "pending_observation": None}, "verifier")
        result = observation["result"]
        claim_statuses = json.loads(json.dumps(state["claim_statuses"]))
        evidence_ids, source_ids = list(state["evidence_ids"]), list(state["source_ids"])
        claim_id = state["current_claim_id"]
        if result["ok"] and claim_id in claim_statuses:
            claim = next(c for c in state["claims"] if c["id"] == claim_id)
            for identifier in result["evidence_ids"]:
                row = self.db.get(EvidenceRecord, UUID(identifier))
                record = self.db.get(ClaimRecord, row.claim_record_id) if row else None
                if record and record.claim_id == claim_id and record.claim_text == claim["claim"]:
                    evidence_ids.append(identifier)
                    claim_statuses[claim_id]["evidence_ids"].append(identifier)
            for identifier in result["source_ids"]:
                if self.db.get(AgentAllowedSource, (UUID(state["task_id"]), UUID(identifier))) is not None:
                    source_ids.append(identifier)
        for status in claim_statuses.values():
            status["evidence_ids"] = list(dict.fromkeys(status["evidence_ids"]))
        summary = {"round": state["round_count"], "claim_id": claim_id, "tool": observation["tool"],
                   "ok": result["ok"], "summary": result["summary"][:400], "error_category": result["error_category"],
                   "source_ids": result["source_ids"], "query": state["action_args"].get("query", "")[:160]}
        executed = observation["executed"]
        opened = json.loads(json.dumps(state["opened_source_ids"]))
        if observation["tool"] == "open_source_tool" and claim_id:
            source = self.db.scalar(select(WebSource).where(WebSource.canonical_url == state["action_args"].get("url")))
            if source:
                opened[claim_id] = list(dict.fromkeys([*opened.get(claim_id, []), str(source.id)]))
        return self.save({**state, "claim_statuses": claim_statuses,
                          "opened_source_ids": opened,
                          "evidence_ids": list(dict.fromkeys(evidence_ids)), "source_ids": list(dict.fromkeys(source_ids)),
                          "search_count": state["search_count"] + int(executed and observation["tool"] == "web_search_tool"),
                          "retrieval_count": state["retrieval_count"] + int(executed and observation["tool"] == "rag_retrieve_tool"),
                          "step_summaries": [*state["step_summaries"], summary][-12:], "pending_observation": None,
                          "applied_step_indices": [*state.get("applied_step_indices", []), observation["step_index"]][-12:]}, "verifier")

    def verifier(self, state):
        state = self.refresh(state)
        decision = self.policy({**state, "candidate_sources": candidate_sources(self.db, state)}, ActionProposal(action=Action.STOP))
        if state["stop_reason"] in {"tool_timeout", "uncertain_tool_outcome"}:
            decision = PolicyDecision(action=Action.ABSTAIN, reason=state["stop_reason"])
        if decision.action in {Action.STOP, Action.ABSTAIN}:
            state = {**state, "termination_action": decision.action.value, "selected_action": decision.action.value,
                     "selected_tool": None, "stop_reason": decision.reason, "policy_reason": decision.reason}
        return self.save(state, "reporter" if state["stop_reason"] else "router")

    def reporter(self, state):
        status = "failed" if state["status"] == "failed" else (
            "completed" if state["termination_action"] == "STOP" and all(is_covered(item) for item in coverage_items(state)) else "partial")
        lines = [f"研究领域：{state.get('domain_id', 'general_research')}", f"研究问题：{state['user_query']}", f"状态：{status}；工具调用：{state['round_count']}；剩余预算：{state['remaining_budget']}。",
                 f"终止动作：{state['termination_action'] or 'ABSTAIN'}；加权 Coverage：{state['coverage']:.1%}。",
                 "只有显式验证通过且满足来源和时效要求的 Claim 才标为 SUPPORTED；未覆盖部分弃答，不给出确定结论。"]
        for claim in state["claims"]:
            coverage = state["claim_statuses"].get(claim["id"], {})
            lines.extend(["", f"{claim['id']} [{coverage.get('status', 'NOT_RETRIEVED')}]：{claim['claim']}", coverage.get("summary", "尚无证据。")])
            for identifier in coverage.get("evidence_ids", [])[:3]:
                row = self.db.get(EvidenceRecord, UUID(identifier))
                source = row.source_url or f"document:{row.document_id}#chunk={row.chunk_id}"
                lines.extend([f"- Evidence {row.id}；关系：{row.relation}；来源：{source}",
                              "> " + row.supporting_text[:400].replace("\n", "\n> ")])
            if not coverage.get("enough"):
                lines.append(f"待补充：满足 required_source={claim['required_source']} 的证据。")
        if state["last_error"]:
            lines.append(f"最后一次错误类别：{state['last_error']}。")
        return self.save({**state, "status": status, "final_report": "\n".join(lines)[:20000]},
                         state.get("recovery_node") if status == "failed" and state.get("recovery_node") else "END")

    def compile(self, entry_node="planner"):
        graph = StateGraph(AgentState)
        for name in ["planner", "router", "tool_executor", "state_updater", "verifier", "reporter"]:
            graph.add_node(name, getattr(self, name))
        if entry_node not in {"planner", "router", "tool_executor", "state_updater", "verifier", "reporter"}:
            raise ValueError("Invalid checkpoint continuation node")
        graph.add_edge(START, entry_node)
        graph.add_conditional_edges("planner", lambda state: "reporter" if state["status"] == "failed" else "router")
        graph.add_conditional_edges("router", lambda state: "reporter" if state["termination_action"] else "tool_executor")
        graph.add_edge("tool_executor", "state_updater")
        graph.add_edge("state_updater", "verifier")
        graph.add_conditional_edges("verifier", lambda state: "reporter" if state["stop_reason"] else "router")
        graph.add_edge("reporter", END)
        return graph.compile()


def run_agent_task(db, task_id, *, client=None, registry=None, config=settings, policy=None, reference_time=None):
    task_id = UUID(str(task_id))
    task = db.get(ResearchTask, task_id)
    if task is None:
        raise LookupError("Research task not found")
    if task.budget <= 0:
        raise AgentConflict("Task budget must be positive")
    existing = db.get(AgentRun, task_id)
    if existing is not None:
        if existing.state["status"] == "running":
            raise AgentConflict("Research task is already running")
        return existing.state
    inference = client if client is not None else get_llm_client()
    changed = db.execute(update(ResearchTask).where(
        ResearchTask.id == task_id, ResearchTask.status == "queued"
    ).values(status="running")).rowcount
    if changed != 1:
        db.rollback()
        raise AgentConflict("Only a queued task can be started")
    state = initial_state(task_id, task.query, min(task.budget, config.agent_max_steps), config,
                          domain_id=getattr(task, "domain_id", "general_research"))
    token = uuid4().hex
    db.add(AgentRun(task_id=task_id, state=state, updated_at=datetime.now(timezone.utc),
                    owner_token=token, lease_expires_at=lease_deadline(config)))
    db.commit()
    runtime = AgentRuntime(db, inference, registry=registry, config=config, owner_token=token,
                           policy=policy, reference_time=reference_time)
    runtime.save(state, "planner")
    return invoke_runtime(runtime, state)


def invoke_runtime(runtime, state, entry_node="planner"):
    try:
        return runtime.compile(entry_node).invoke(state, config={"recursion_limit": 5 * state["remaining_budget"] + 10})
    except LeaseLost:
        runtime.db.rollback()
        raise AgentConflict("Worker ownership changed") from None
    except Exception as exc:
        runtime.db.rollback()
        state = runtime.db.get(AgentRun, UUID(state["task_id"])).state
        state = {**state, "status": "failed", "last_error": getattr(exc, "category", type(exc).__name__),
                 "stop_reason": "workflow_failed", "termination_action": "ABSTAIN", "recovery_node": runtime.cursor}
        return runtime.reporter(state)
    finally:
        current = runtime.db.get(AgentRun, UUID(state["task_id"]), populate_existing=True)
        if current and current.owner_token == runtime.owner_token and current.state["status"] != "running":
            current.owner_token, current.lease_expires_at = None, None
            runtime.db.commit()


def resume_agent_task(db, task_id, *, client=None, registry=None, config=settings, owner_token=None):
    task_id = UUID(str(task_id))
    token = owner_token or reserve_recovery(db, task_id, config)
    run = db.get(AgentRun, task_id, populate_existing=True)
    if run is None or run.owner_token != token:
        raise AgentConflict("Recovery worker no longer owns task")
    checkpoint = latest_snapshot(db, task_id)
    if checkpoint is None or checkpoint.next_node == "END":
        raise AgentConflict("No unfinished checkpoint")
    state = {**initial_state(task_id, run.state["user_query"], run.state["remaining_budget"], config,
                              domain_id=run.state.get("domain_id", "general_research")),
             **json.loads(json.dumps(checkpoint.state))}
    state.update(status="running", stop_reason=None, termination_action=None, final_report=None,
                 recovery_node=None, recovery_count=state.get("recovery_count", 0) + 1)
    runtime = AgentRuntime(db, client or get_llm_client(), registry=registry, config=config, owner_token=token)
    node = checkpoint.next_node
    if node == "recover_tool":
        step = db.scalar(select(AgentStep).where(AgentStep.task_id == task_id, AgentStep.step_index == state["round_count"]))
        if step is None:
            raise AgentConflict("Reserved tool attempt is missing")
        receipt = db.get(ToolExecutionReceipt, step.id)
        if step.observation is not None:
            result, executed = ToolResult.model_validate(step.observation), step.status != "rejected"
        elif receipt is not None:
            result, executed = ToolResult.model_validate(receipt.result), True
            step.status = "success" if result.ok else "empty"
        elif step.status == "validating":
            result, executed = ToolResult(ok=False, summary="Interrupted before dispatch; reservation retained.", error_category="interrupted_before_dispatch"), False
            step.status = "rejected"
        else:
            result, executed = ToolResult(ok=False, summary="Tool outcome unknown; inspect existing effects before any retry.", error_category="uncertain_tool_outcome"), True
            step.status = "unknown"
            state.update(stop_reason="uncertain_tool_outcome", termination_action="ABSTAIN")
        step.observation, step.error_category = result.model_dump(mode="json"), result.error_category
        state.update(current_claim_id=step.claim_id, selected_tool=step.tool_name, action_args=step.arguments,
                     pending_observation={"tool": step.tool_name, "step_index": step.step_index,
                                          "executed": executed, "result": step.observation})
        node = "state_updater"
    runtime.save(state, node)
    return invoke_runtime(runtime, state, node)
