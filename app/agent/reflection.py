"""Bounded failure analysis and query refinement; no extra retry budget."""
from dataclasses import asdict
from datetime import datetime, timezone
import json
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.agent.policy import normalize_query
from app.db.models import AgentReflection
from app.llm_client import LLMError


class RewrittenQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=3, max_length=600)


def analyze_failure(state, claim_id):
    status = state["claim_statuses"].get(claim_id, {})
    previous = next((item for item in reversed(state["step_summaries"]) if item["claim_id"] == claim_id), {})
    category = previous.get("error_category") or (
        "outdated_evidence" if status.get("status") == "OUTDATED" else
        "missing_required_source" if not status.get("source_requirements_met") else
        "no_direct_support" if status.get("verification_attempted") else "no_useful_evidence")
    return category, (f"Previous action: {previous.get('tool', 'unknown')}; evidence status: {status.get('status')}; "
                       f"missing_internal={bool(status.get('missing_internal'))}; missing_external={bool(status.get('missing_external'))}. "
                       "Keep the claim entities, target the missing source or fact, and change the search angle.")


def reflect_query(db, state, claim, action, query, prior, client):
    category, analysis = analyze_failure(state, claim["id"])
    row = AgentReflection(task_id=UUID(state["task_id"]), rewrite_index=state["query_rewrite_count"],
                          claim_id=claim["id"], failure_category=category, analysis=analysis,
                          original_query=prior[-1]["query"], created_at=datetime.now(timezone.utc))
    db.add(row)
    db.commit()
    try:
        if normalize_query(query) in {item["normalized"] for item in prior}:
            rewritten, trace = client.complete_json([
                {"role": "system", "content": "Return only JSON {\"query\":\"...\"}. Analyze the supplied failure before rewriting. Preserve claim entities, target the evidence gap and never repeat previous queries."},
                {"role": "user", "content": json.dumps({"claim": claim, "action": action,
                    "failure_category": category, "failure_analysis": analysis,
                    "previous_queries": [item["query"] for item in prior],
                    "evidence_status": state["claim_statuses"][claim["id"]]["status"]}, ensure_ascii=False)},
            ], RewrittenQuery, operation="query_rewrite", max_tokens=400)
            query, row.trace = rewritten.query, asdict(trace) if trace else None
        row.rewritten_query = query
        db.commit()
        return query, {"claim_id": claim["id"], "failure_category": category, "analysis": analysis[:300],
                       "rewrite_index": row.rewrite_index, "rewritten_query": query}
    except LLMError as exc:
        row.trace = {"operation": "query_rewrite", "error_category": exc.category}
        db.commit()
        raise
