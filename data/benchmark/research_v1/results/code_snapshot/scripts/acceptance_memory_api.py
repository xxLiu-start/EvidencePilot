"""Live background-task, verification, checkpoint recovery and core API acceptance."""
from datetime import datetime, timezone
import json
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
from app.agent.policy import normalize_query
from app.agent.reflection import reflect_query
from app.agent.state import initial_state
from app.db.models import AgentRun, AgentStep, ResearchTask, StateSnapshot, ToolExecutionReceipt
from app.db.session import Base
from app.llm_client import ReasoningClient
from app.search.cache import write_json
from app.verification.verifier import VerificationResult
from scripts.acceptance import Server, wait_for_task


def main():
    url = os.environ.get("EVIDENCEPILOT_TEST_POSTGRES_URL")
    if not url:
        raise SystemExit("Set EVIDENCEPILOT_TEST_POSTGRES_URL to an isolated PostgreSQL database")
    output = ROOT/"data/benchmark/memory_api"
    report = {"status": "running", "checked_at": datetime.now(timezone.utc).isoformat(), "checks": []}
    def passed(name, details):
        report["checks"].append({"name": name, "passed": True, "details": details})
        print("PASS " + name, flush=True)
    admin, engine = create_engine(url), None
    schema = "ep_memory_api_" + uuid4().hex
    try:
        with admin.begin() as connection:
            connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public"))
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        scoped = make_url(url).update_query_dict({"options": f"-csearch_path={schema},public"})
        engine = create_engine(scoped)
        Base.metadata.create_all(engine)
        with TemporaryDirectory(prefix="ep-memory-api-") as directory:
            server = Server(scoped.render_as_string(hide_password=False), Path(directory))
            try:
                client = server.start()
                assert client.get("/health").json()["database"] == "ok"
                response = client.post("/api/v1/documents/upload", files={"file": ("memory_configuration.md",
                    "# EvidencePilot defaults\n\nEvidencePilot 的默认 Chunk Size 为 500 tokens，Overlap 为 80 tokens。\n")})
                assert response.status_code == 201, response.text
                created = client.post("/api/v1/research", json={"query": "根据内部文档，仅核对一个 Claim：EvidencePilot 默认 Chunk Size 和 Overlap 各是多少？", "budget": 4})
                assert created.status_code == 201 and created.json()["task_id"] == created.json()["id"]
                identifier = created.json()["task_id"]
                completed = wait_for_task(client, identifier)
                state = completed.json()["state"]
                assert state["status"] == "completed" and state["coverage"] == 1, state
                verifications = client.get(f"/api/v1/tasks/{identifier}/verifications").json()["items"]
                for item in verifications:
                    value = item["result"]
                    VerificationResult.model_validate({key: value[key] for key in VerificationResult.model_fields})
                    known = set(state["evidence_ids"])
                    assert set(value["supporting_evidence_ids"]) <= known and set(value["contradicting_evidence_ids"]) <= known
                    assert value["evidence_relations"]
                passed("claim_verification", {"actual_model": verifications[0]["trace"]["actual_model"], "coverage": state["coverage"]})
                passed("evidence_id_and_quote_relationship_validation", {"verifications": len(verifications)})
                # The scope/freshness adversarial cases run in the regression suite.
                from app.verification.contradiction import detect_contradiction
                from app.verification.freshness import detect_outdated
                a = {"evidence_id": "A", "supporting_text": "Model X supports 128K context.", "relation": "supports",
                     "scope": {"model_name": "X", "model_version": "1", "deployment_mode": "local"}}
                b = {**a, "evidence_id": "B", "supporting_text": "Model X supports 32K context.", "relation": "refutes",
                     "scope": {**a["scope"], "deployment_mode": "api"}}
                assert not detect_contradiction("context", [a,b])["requires_verification"]
                passed("scope_aware_conflict_detection", {"different_deployment_is_not_conflict": True})
                assert detect_outdated({"published_at": "2024-01-01T00:00:00+00:00"})["outdated"]
                assert detect_outdated({"retrieved_at": datetime.now(timezone.utc), "scope": {"model_version": "1"}}, "2")["version_mismatch"]
                passed("freshness_and_version_checks", {"expired_date_and_old_model_version_rejected": True})
                with Session(engine) as db:
                    row = ResearchTask(query="Qwen deployment research reflection", budget=6)
                    db.add(row)
                    db.commit()
                    rs = initial_state(row.id, row.query, 6)
                    claim = {"id": "C1", "claim": "Qwen self-hosted deployment documentation"}
                    rs.update(query_rewrite_count=1, claim_statuses={"C1": {"status": "INSUFFICIENT", "source_requirements_met": False,
                        "missing_external": True}}, step_summaries=[{"claim_id": "C1", "tool": "web_search_tool", "error_category": "no_parseable_sources"}])
                    prior = [{"query": "Qwen deployment", "normalized": normalize_query("Qwen deployment")}]
                    inference = ReasoningClient()
                    try:
                        query, reflection = reflect_query(db, rs, claim, "SEARCH_WEB", "Qwen deployment", prior, inference)
                    finally:
                        inference.close()
                    assert normalize_query(query) != prior[0]["normalized"]
                    write_json(output/"reflection.json", reflection)
                    # Controlled crash-window injection: reuse a real committed tool
                    # receipt in a new task, then restart the server before recovery.
                    original = db.scalar(select(StateSnapshot).where(StateSnapshot.task_id == UUID(identifier),
                        StateSnapshot.next_node == "recover_tool").order_by(StateSnapshot.sequence).limit(1))
                    first = db.scalar(select(AgentStep).where(AgentStep.task_id == UUID(identifier)).order_by(AgentStep.step_index).limit(1))
                    receipt = db.get(ToolExecutionReceipt, first.id)
                    assert receipt is not None
                    recovery = ResearchTask(query=state["user_query"], budget=4, status="running")
                    db.add(recovery)
                    db.flush()
                    resume_state = {**original.state, "task_id": str(recovery.id)}
                    db.add(AgentRun(task_id=recovery.id, state=resume_state, updated_at=datetime.now(timezone.utc), checkpoint_seq=1))
                    db.add(StateSnapshot(task_id=recovery.id, sequence=1, next_node="recover_tool", state=resume_state, created_at=datetime.now(timezone.utc)))
                    step = AgentStep(task_id=recovery.id, step_index=1, claim_id=first.claim_id, tool_name=first.tool_name,
                        arguments=first.arguments, observation=None, status="running", elapsed_ms=first.elapsed_ms,
                        trace={"fixture": "simulated_crash_after_real_tool_commit"}, created_at=datetime.now(timezone.utc))
                    db.add(step)
                    db.flush()
                    db.add(ToolExecutionReceipt(step_id=step.id, result=receipt.result, created_at=datetime.now(timezone.utc)))
                    db.commit()
                    recovery_id = str(recovery.id)
                passed("reflection_rewrites_query", {"failure_analyzed": True, "rewritten_query": query})
                server.stop()
                client = server.start()
                recovered = client.post(f"/api/v1/tasks/{recovery_id}/recover")
                assert recovered.status_code == 202, recovered.text
                restored = wait_for_task(client, recovery_id).json()["state"]
                assert restored["status"] == "completed" and restored["coverage"] == 1 and restored["recovery_count"] == 1
                trace = client.get(f"/api/v1/tasks/{recovery_id}/trace").json()
                assert [step["tool_name"] for step in trace["steps"]] == ["rag_retrieve_tool", "verify_claim_tool"]
                assert restored["retrieval_count"] == 1 and len(trace["checkpoints"]) > 1
                passed("historical_tasks_trace_and_manual_recovery", {"restart": True, "no_repeated_successful_tool": True,
                    "recovery_fixture": "simulated_crash_window_with_real_tool_receipt", "rounds": restored["round_count"]})
                assert client.get("/api/v1/tasks").status_code == 200
                assert client.get(f"/api/v1/tasks/{identifier}").status_code == 200
                assert client.get(f"/api/v1/tasks/{identifier}/report").status_code == 200
                assert client.get("/api/v1/documents").json()["total"] == 1
                assert client.post(f"/api/v1/tasks/{identifier}/feedback", json={"rating": 5, "comment": "验收反馈"}).status_code == 201
                assert client.get("/api/v1/evaluations").json()["total"] > 0
                assert client.post(f"/api/v1/tasks/{recovery_id}/recover").status_code == 409
                passed("all_core_fastapi_endpoints", {"background_task_completed": True, "finished_task_recovery_rejected": True})
                write_json(output/"run.json", {"initial_state": state, "verifications": verifications,
                    "recovered_state": restored, "recovered_trace": trace})
            finally:
                server.stop()
        report["status"] = "passed"
    finally:
        if engine:
            engine.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        admin.dispose()
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        if report["status"] == "running":
            report["status"] = "failed"
        write_json(output/"acceptance.json", report)


if __name__ == "__main__":
    main()
