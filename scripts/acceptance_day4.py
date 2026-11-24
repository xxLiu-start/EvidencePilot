"""Real Qwen + Tavily + PostgreSQL acceptance, with honest remote-mode status."""

from dataclasses import asdict
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

from app.config import Settings, settings
from app.db.models import ClaimRecord, EvidenceRecord, WebContent, WebSource
from app.db.session import get_cached_content
from app.db.session import Base
from app.evaluation.web_benchmark import validate_snapshot
from app.evidence.schema import Evidence
from app.llm_client import LLMConfigurationError, selected_branch
from app.search.cache import digest, write_json
from scripts.acceptance import Server


def main():
    url = os.environ.get("EVIDENCEPILOT_TEST_POSTGRES_URL")
    if not url:
        raise SystemExit("Set EVIDENCEPILOT_TEST_POSTGRES_URL to an isolated PostgreSQL test database")
    report = {"checked_at": datetime.now(timezone(timedelta(hours=8))).isoformat(),
              "status": "running", "checks": []}
    output = ROOT / "data/benchmark/day4"
    output.mkdir(parents=True, exist_ok=True)

    def passed(name, details):
        report["checks"].append({"name": name, "passed": True, "details": details})
        print(f"PASS {name}", flush=True)

    admin, engine = create_engine(url), None
    schema = "ep_day4_" + uuid4().hex
    try:
        with admin.begin() as conn:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public"))
            conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        scoped_url = make_url(url).update_query_dict({"options": f"-csearch_path={schema},public"})
        engine = create_engine(scoped_url)
        with engine.begin() as conn:
            Base.metadata.create_all(conn, checkfirst=False)
        with TemporaryDirectory(prefix="ep-day4-") as directory:
            os.environ["LLM_MODE"] = "local"
            server = Server(scoped_url.render_as_string(hide_password=False), Path(directory))
            try:
                client = server.start()
                probe = client.post("/api/v1/llm/probe")
                assert probe.status_code == 200, probe.text
                local_trace = probe.json()["trace"]
                assert local_trace["mode"] == "local" and "Qwen3.8-27B" in local_trace["actual_model"]
                request = {"claims": [{"claim_id": "D4-C1", "claim_text": "Qwen provides official self-hosted deployment documentation and function calling examples."}],
                           "max_queries": 2, "max_results": 2, "max_pages": 3}
                result = client.post("/api/v1/web-research", json=request)
                assert result.status_code == 200, result.text
                snapshot = result.json()
                assert len(snapshot["queries"]) >= 2 and all(q["claim_id"] == "D4-C1" for q in snapshot["queries"])
                assert len({q["query"].casefold() for q in snapshot["queries"]}) == len(snapshot["queries"])
                passed("multi_query_generation", {"queries": snapshot["queries"], "trace": local_trace})
                assert snapshot["pages"] and snapshot["evidences"], json.dumps(snapshot, ensure_ascii=False)
                passed("search_results_parsed", {"pages": len(snapshot["pages"]), "errors": snapshot["errors"]})
                values = [Evidence.model_validate(value) for value in snapshot["evidences"]]
                passed("evidence_pydantic_schema", {"evidences": len(values)})
                with Session(engine) as db:
                    stored = list(db.scalars(select(EvidenceRecord)))
                    assert len(stored) == len(values)
                    for row in stored:
                        claim = db.get(ClaimRecord, row.claim_record_id)
                        source = db.scalar(select(WebSource).where(WebSource.canonical_url == row.source_url))
                        content = get_cached_content(db, row.content_hash)
                        assert claim.claim_id == "D4-C1" and source is not None and content is not None
                        assert digest(content.body) == row.content_hash
                        assert content.body[row.quote_start:row.quote_end] == row.supporting_text
                passed("claim_association", {"claim_id": "D4-C1", "stored_evidences": len(values)})
                assert all(value.source_url and value.source_type == "web" for value in values)
                passed("every_web_evidence_has_source_url", {"urls": sorted({value.source_url for value in values}), "quotes_exact": True})
                replay = client.post(f"/api/v1/web-research/{snapshot['run_id']}/replay")
                assert replay.status_code == 200 and replay.json()["replayed"] is True, replay.text
                assert replay.json()["evidences"] == snapshot["evidences"]
                path = Path(settings.web_cache_dir) / "research" / (snapshot["run_id"] + ".json")
                offline = validate_snapshot(path)
                server.stop()
                client = server.start()
                response = client.get("/api/v1/claims/D4-C1/evidences")
                assert response.status_code == 200 and len(response.json()) == len(values)
                with Session(engine) as db:
                    assert db.scalar(select(func.count()).select_from(EvidenceRecord)) == len(values)
                passed("url_dedup_and_fixed_cache", {"run_id": snapshot["run_id"], "offline": offline,
                                                    "restart_persistence": True})
                write_json(output / "fixed_snapshot.json", snapshot)
                write_json(output / "manifest.json", {"snapshot": "fixed_snapshot.json", "snapshot_hash": digest(json.dumps(snapshot, sort_keys=True)),
                                                       "content_hashes": sorted({page["content_hash"] for page in snapshot["pages"]}),
                                                       "local_trace": local_trace})
            finally:
                server.stop()
            remote_config = Settings().model_copy(update={"llm_mode": "remote"})
            try:
                selected_branch(remote_config)
            except LLMConfigurationError:
                report["checks"].append({"name": "local_and_remote_inference", "passed": False,
                    "status": "pending_remote_configuration", "local": local_trace,
                    "reason": "Remote HTTPS URL, model ID and its own .env credential are required; no remote fallback was attempted."})
                print("PENDING remote inference: missing remote branch configuration", flush=True)
            else:
                os.environ["LLM_MODE"] = "remote"
                try:
                    client = server.start()
                    remote = client.post("/api/v1/llm/probe")
                    assert remote.status_code == 200, remote.text
                    assert remote.json()["trace"]["mode"] == "remote"
                    passed("local_and_remote_inference", {"local": local_trace, "remote": remote.json()["trace"]})
                finally:
                    server.stop()
                    os.environ["LLM_MODE"] = "local"
        report["status"] = "passed" if all(c["passed"] for c in report["checks"]) else "partial"
    except Exception as exc:
        report["status"] = "failed"
        # Do not copy HTTP response bodies or prompts into the acceptance report.
        report["error_category"] = type(exc).__name__
        raise
    finally:
        if engine is not None:
            engine.dispose()
        with admin.begin() as conn:
            conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        admin.dispose()
        write_json(output / "acceptance.json", report)
    print(f"Acceptance status: {report['status']}; report: {output / 'acceptance.json'}", flush=True)


if __name__ == "__main__":
    main()
