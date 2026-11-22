"""Live LangGraph acceptance: Qwen, Web Search, RAG and PostgreSQL audit steps."""

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy import create_engine, func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.agent.planner import ResearchPlan
from app.agent.tools import ToolRegistry
from app.config import Settings
from app.db.models import AgentRun, AgentStep, EvidenceRecord
from app.db.session import Base
from app.llm_client import LLMConfigurationError, selected_branch
from app.search.cache import write_json
from scripts.acceptance import Server, wait_for_task


def main():
    url = os.environ.get("EVIDENCEPILOT_TEST_POSTGRES_URL")
    if not url:
        raise SystemExit("Set EVIDENCEPILOT_TEST_POSTGRES_URL to an isolated test database")
    output = ROOT / "data/benchmark/agent"
    report = {"checked_at": datetime.now(timezone(timedelta(hours=8))).isoformat(), "status": "running",
              "checks": [], "tool_call_mode": "structured_action"}

    def passed(name, details):
        report["checks"].append({"name": name, "passed": True, "details": details})
        print(f"PASS {name}", flush=True)

    admin, engine = create_engine(url), None
    schema = "ep_agent_" + uuid4().hex
    try:
        with admin.begin() as connection:
            connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public"))
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        scoped_url = make_url(url).update_query_dict({"options": f"-csearch_path={schema},public"})
        engine = create_engine(scoped_url)
        with engine.begin() as connection:
            Base.metadata.create_all(connection, checkfirst=False)
        os.environ["LLM_MODE"] = "local"
        os.environ["AGENT_TOOL_TIMEOUT_SECONDS"] = "30"
        with TemporaryDirectory(prefix="ep-agent-") as directory:
            server = Server(scoped_url.render_as_string(hide_password=False), Path(directory))
            try:
                client = server.start()
                for name, content in [
                    ("qwen_notes.md", "# Qwen\n\nQwen provides open model weights, self-hosted deployment documentation and function calling examples.\n\nThese are local reading notes; official capability claims need primary-source validation."),
                    ("deepseek_notes.md", "# DeepSeek\n\nDeepSeek API documentation describes function calling, thinking mode and JSON Schema tool arguments.\n\nDeployment options and model licenses must be checked against current official documentation."),
                ]:
                    response = client.post("/api/v1/documents", files={"file": (name, content)})
                    assert response.status_code == 201, response.text
                response = client.post("/api/v1/research", json={
                    "query": "比较 Qwen 和 DeepSeek 的企业私有部署与 Function Calling 能力。先查询本地知识库，再搜索并打开官方文档核对。", "budget": 8})
                assert response.status_code == 201, response.text
                task_id = response.json()["id"]
                response = wait_for_task(client, task_id)
                assert response.status_code == 200, response.text
                state = response.json()["state"]
                ResearchPlan(claims=[{k: v for k, v in c.items() if k != "weight"} for c in state["claims"]])
                assert len(state["claims"]) >= 2 and state["status"] in {"completed", "partial"}, state
                passed("complex_question_to_claims", {"claims": state["claims"], "planner_repaired": state["planner_repaired"]})
                response = client.get(f"/api/v1/tasks/{task_id}/steps")
                assert response.status_code == 200
                steps = response.json()
                assert any(step["tool_name"] == "web_search_tool" and step["status"] == "success" for step in steps), steps
                passed("web_search_tool", {"search_count": state["search_count"]})
                assert any(step["tool_name"] == "rag_retrieve_tool" and step["status"] == "success" for step in steps), steps
                passed("rag_retrieve_tool", {"retrieval_count": state["retrieval_count"]})
                registry = ToolRegistry()
                for step in steps:
                    if step["status"] not in {"rejected", "validating"}:
                        registry.validate(step["tool_name"], step["arguments"])
                passed("tool_arguments_validated", {"tools": sorted({step["tool_name"] for step in steps}), "mode": "structured_action"})
                from uuid import UUID
                with Session(engine) as db:
                    count = db.scalar(select(func.count()).select_from(AgentStep).where(AgentStep.task_id == UUID(task_id)))
                    persisted = db.get(AgentRun, UUID(task_id)).state
                    assert count == len(steps) == state["round_count"]
                    assert persisted["remaining_budget"] == 8 - count >= 0
                    assert db.scalar(select(func.count()).select_from(EvidenceRecord)) >= 1
                passed("all_tool_calls_in_agent_steps", {"rows": count, "remaining_budget": state["remaining_budget"]})
                assert count >= 2 and len(state["step_summaries"]) >= 2 and state["final_report"]
                assert len(json.dumps(state).encode()) <= 65536
                assert all("body" not in step["observation"] for step in steps)
                server.stop()
                client = server.start()
                response = client.get(f"/api/v1/tasks/{task_id}/agent")
                assert response.status_code == 200 and response.json()["state"] == state
                response = client.post(f"/api/v1/tasks/{task_id}/run")
                assert response.status_code == 200 and response.json()["state"]["round_count"] == count
                passed("langgraph_multiple_action_observation_rounds", {"rounds": count, "state_bytes": len(json.dumps(state).encode()),
                                                                       "restart_persistence": True, "repeat_run_is_idempotent": True})
                write_json(output / "run.json", {"state": state, "steps": steps})
            finally:
                server.stop()
        remote = Settings().model_copy(update={"llm_mode": "remote"})
        try:
            selected_branch(remote)
        except LLMConfigurationError:
            report["remote_tool_validation"] = {"live_status": "pending_remote_configuration",
                                                 "reason": "Remote HTTPS URL, model ID and its own credential are not configured."}
        else:
            os.environ["LLM_MODE"] = "remote"
            with TemporaryDirectory(prefix="ep-agent-remote-") as directory:
                remote_server = Server(scoped_url.render_as_string(hide_password=False), Path(directory))
                try:
                    remote_client = remote_server.start()
                    created = remote_client.post("/api/v1/research", json={
                        "query": "Research Qwen deployment documentation. First use local RAG, then Web Search for official sources.",
                        "budget": 2})
                    assert created.status_code == 201, created.text
                    remote_id = created.json()["id"]
                    completed = wait_for_task(remote_client, remote_id)
                    assert completed.status_code == 200, completed.text
                    remote_state = completed.json()["state"]
                    assert remote_state["status"] in {"completed", "partial"}
                    remote_steps_response = remote_client.get(f"/api/v1/tasks/{remote_id}/steps")
                    assert remote_steps_response.status_code == 200
                    remote_steps = remote_steps_response.json()
                    assert remote_steps and any(step["status"] == "success" for step in remote_steps)
                    for step in remote_steps:
                        assert step["trace"]["mode"] == "remote" and step["trace"]["actual_model"]
                        if step["status"] != "rejected":
                            registry.validate(step["tool_name"], step["arguments"])
                    report["remote_tool_validation"] = {
                        "live_status": "passed", "actions": len(remote_steps),
                        "actual_models": sorted({step["trace"]["actual_model"] for step in remote_steps}),
                    }
                    write_json(output / "remote_run.json", {"state": remote_state, "steps": remote_steps})
                finally:
                    remote_server.stop()
                    os.environ["LLM_MODE"] = "local"
        report["acceptance_status"] = "passed"
        report["status"] = "passed" if report["remote_tool_validation"]["live_status"] == "passed" else "partial"
    except Exception as exc:
        report["status"] = "failed"
        report["error_category"] = type(exc).__name__
        raise
    finally:
        if engine is not None:
            engine.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        admin.dispose()
        write_json(output / "acceptance.json", report)
    print(f"Agent acceptance: {report['acceptance_status']}; report: {output / 'acceptance.json'}", flush=True)


if __name__ == "__main__":
    main()
