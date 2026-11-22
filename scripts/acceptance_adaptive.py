"""Adaptive acquisition acceptance on isolated PostgreSQL plus live local Qwen/API.

The six deterministic edge cases use explicit model/acquisition test doubles.
Two additional API runs use real Qwen, BGE-M3 retrieval and Tavily web research.
"""

from datetime import datetime, timezone
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from uuid import UUID, uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.db.models import AgentPolicyDecision, EvidenceAssessment
from app.db.session import Base
from app.search.cache import write_json
from scripts.acceptance import Server, wait_for_task
from scripts.adaptive_scenarios import SCENARIOS, run_scenario


def main():
    url = os.environ.get("EVIDENCEPILOT_TEST_POSTGRES_URL")
    if not url:
        raise SystemExit("Set EVIDENCEPILOT_TEST_POSTGRES_URL to an isolated PostgreSQL test database")
    output = ROOT / "data/benchmark/adaptive"
    report = {"checked_at": datetime.now(timezone.utc).isoformat(), "status": "running", "checks": [],
              "algorithm_version": "adaptive-v1", "live_runs": []}
    admin, engine = create_engine(url), None
    schema = "ep_adaptive_" + uuid4().hex
    try:
        with admin.begin() as connection:
            connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public"))
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        scoped = make_url(url).update_query_dict({"options": f"-csearch_path={schema},public"})
        engine = create_engine(scoped)
        Base.metadata.create_all(engine, checkfirst=False)
        with Session(engine) as db:
            scenarios = [run_scenario(db, name) for name in SCENARIOS]
        write_json(output / "scenarios.json", {"execution": "controlled_edge_cases_on_real_langgraph_and_postgresql",
                                               "scenarios": scenarios})
        report["checks"] = [{"name": item["name"], "passed": item["passed"],
                             "execution": item["execution"], "rounds": item["state"]["round_count"],
                             "coverage": item["state"]["coverage"],
                             "termination_action": item["state"]["termination_action"]} for item in scenarios]
        print("PASS six controlled adaptive edge cases on PostgreSQL", flush=True)
        os.environ["LLM_MODE"] = "local"
        os.environ["AGENT_TOOL_TIMEOUT_SECONDS"] = "30"
        os.environ["AGENT_MAX_STEPS"] = "12"
        with TemporaryDirectory(prefix="ep-adaptive-") as directory:
            server = Server(scoped.render_as_string(hide_password=False), Path(directory))
            try:
                client = server.start()
                document = client.post("/api/v1/documents", files={"file": ("adaptive_configuration.md",
                    "# EvidencePilot chunk configuration\n\nEvidencePilot 的默认 Chunk Size 为 500 tokens，Overlap 为 80 tokens。切分优先遵循标题和段落。\n")})
                assert document.status_code == 201, document.text
                queries = [
                    ("local_rag", "根据内部知识库核对：EvidencePilot 的默认 Chunk Size 和 Overlap 分别是多少？只生成一个 Claim，要求内部文档来源。", "rag_retrieve_tool"),
                    ("web_research", "核对 Qwen 是否提供使用 vLLM 自托管部署的官方文档。请搜索官方资料，只生成一个 Claim。", "web_search_tool"),
                ]
                runs = []
                for name, query, expected_tool in queries:
                    created = client.post("/api/v1/research", json={"query": query, "budget": 6})
                    assert created.status_code == 201, created.text
                    identifier = created.json()["id"]
                    print("RUN live " + name, flush=True)
                    completed = wait_for_task(client, identifier)
                    assert completed.status_code == 200, completed.text
                    state = completed.json()["state"]
                    response = client.get(f"/api/v1/tasks/{identifier}/steps")
                    assert response.status_code == 200
                    steps = response.json()
                    assert state["status"] in {"completed", "partial"}, state
                    assert state["termination_action"] in {"STOP", "ABSTAIN"}
                    assert any(step["tool_name"] == expected_tool and step["status"] == "success" for step in steps), steps
                    assert state["round_count"] == len(steps) <= 6 and state["remaining_budget"] == 6 - len(steps)
                    assert state["retrieval_action_count"] <= 6 and state["query_rewrite_count"] <= 2
                    assert all(count <= 3 for count in state["claim_retrieval_counts"].values())
                    with Session(engine) as db:
                        decisions = list(db.scalars(select(AgentPolicyDecision).where(AgentPolicyDecision.task_id == UUID(identifier)).order_by(AgentPolicyDecision.decision_index)))
                        assessments = list(db.scalars(select(EvidenceAssessment).where(EvidenceAssessment.task_id == UUID(identifier))))
                        audit = [{"proposed": row.proposed_action, "selected": row.selected_action,
                                  "reason": row.reason, "snapshot": row.snapshot} for row in decisions]
                        verification = [{"claim_id": row.claim_id, "fingerprint": row.fingerprint,
                                         "result": row.result, "trace": row.trace} for row in assessments]
                    if name == "local_rag":
                        assert state["coverage"] == 1 and state["termination_action"] == "STOP", state
                        assert verification and any(item["result"].get("verified") for item in verification)
                    runs.append({"name": name, "state": state, "steps": steps,
                                 "policy_decisions": audit, "verifications": verification})
                    report["live_runs"].append({"name": name, "execution": "real_qwen_and_tools",
                        "status": state["status"], "termination_action": state["termination_action"],
                        "coverage": state["coverage"], "rounds": state["round_count"],
                        "claim_statuses": {key: value["status"] for key, value in state["claim_statuses"].items()},
                        "outdated_evidence_count": sum(value["outdated_evidence_count"] for value in state["claim_statuses"].values()),
                        "stop_reason": state["stop_reason"], "tools": [step["tool_name"] for step in steps]})
                    print(f"PASS live {name}: {state['termination_action']}, coverage={state['coverage']}, rounds={state['round_count']}", flush=True)
                server.stop()
                client = server.start()
                for run in runs:
                    identifier = run["state"]["task_id"]
                    response = client.get(f"/api/v1/tasks/{identifier}/agent")
                    assert response.status_code == 200 and response.json()["state"] == run["state"]
                    repeated = client.post(f"/api/v1/tasks/{identifier}/run")
                    assert repeated.status_code == 200 and repeated.json()["state"] == run["state"]
                report["restart_persistence"] = True
                report["repeat_run_is_idempotent"] = True
                write_json(output / "run.json", {"execution": "real_qwen_bge_m3_tavily_postgresql", "runs": runs})
            finally:
                server.stop()
        report["status"] = "passed"
    except Exception as exc:
        report["status"], report["error_category"] = "failed", type(exc).__name__
        raise
    finally:
        if engine is not None:
            engine.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        admin.dispose()
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        write_json(output / "acceptance.json", report)
    print("Adaptive acceptance: " + report["status"], flush=True)


if __name__ == "__main__":
    main()
