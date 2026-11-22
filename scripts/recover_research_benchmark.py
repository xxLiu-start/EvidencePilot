"""Retry ONLY recorded infrastructure transport failures with the pinned model.

Original algorithm/schema failures and every interrupted attempt remain archived.
No label, prompt, source, or algorithm changes are permitted during recovery.
"""
import argparse
import csv
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pydantic import BaseModel
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.config import settings
from app.db.session import Base
from app.evaluation.baselines import METHODS, run_method
from app.evaluation.frozen_research import FrozenResearchTools, seed_frozen_corpus
from app.evaluation.research_benchmark import bad_cases, model_metadata, replay, summarize
from app.evaluation.research_dataset import load_research_dataset, sha
from app.evaluation.research_metrics import grade_answer
from app.llm_client import ReasoningClient
from app.retrieval.embedding import BGEEmbeddingService
from app.search.cache import digest, write_json


class Probe(BaseModel):
    ok: bool


def recover(dataset, output):
    manifest, sources, tasks, chunks = load_research_dataset(dataset)
    tasks = {task["task_id"]: task for task in tasks}
    original = output/"attempts/initial_interrupted"
    data = json.loads((original/"results.json").read_text())
    meta = data["metadata"]
    assert meta["dataset_manifest_sha256"] == sha(dataset/"manifest.json"), "Dataset changed"
    for name, expected in meta["code_sha256"].items():
        assert sha(name) == expected, "Implementation changed; run a new full experiment"
    records = [json.loads(line) for line in (original/"runs.jsonl").read_text().splitlines() if line.strip()]
    indices = [index for index, row in enumerate(records) if row["error_category"] == "transport"]
    cfg = settings.model_copy(update={"embedding_local_files_only": True, "embedding_device": "cpu", "embedding_use_fp16": False})
    current_model = model_metadata(cfg)
    for key in ("mode", "requested_model", "local_config_sha256", "weight_file_manifest", "server_version"):
        assert current_model[key] == meta["model"][key], "Inference identity/version changed: " + key
    for key, value in {"llm_timeout_seconds": cfg.llm_timeout_seconds, "llm_max_retries": cfg.llm_max_retries,
                       "disable_thinking": cfg.llm_disable_thinking, "freshness_days": cfg.agent_evidence_max_age_days}.items():
        assert meta["parameters"][key] == value, "Parameters changed"
    traces = []
    inference = ReasoningClient(cfg, trace_sink=traces.append)
    probe, probe_trace = inference.complete_json([
        {"role": "system", "content": 'Return only JSON {"ok":true}.'},
        {"role": "user", "content": "Non-sensitive model identity probe."},
    ], Probe, operation="benchmark_recovery_identity_probe", max_tokens=50, timeout_seconds=20)
    assert probe.ok and probe_trace.actual_model in meta["actual_models"]
    assert probe_trace.system_fingerprint in meta["system_fingerprints"], "Server fingerprint changed; do not mix experiments"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    attempt = output/"attempts"/("recovery_"+stamp)
    attempt.mkdir(parents=True, exist_ok=False)
    write_json(attempt/"identity.json", {"model": current_model, "probe_trace": traces,
        "dataset_manifest_sha256": meta["dataset_manifest_sha256"], "retry_count": len(indices),
        "selection_rule": "ONLY original error_category=transport; no schema/algorithm failure retries"})
    url = os.environ.get("EVIDENCEPILOT_TEST_POSTGRES_URL")
    if not url:
        raise ValueError("Set EVIDENCEPILOT_TEST_POSTGRES_URL")
    admin, engine = create_engine(url), None
    schema = "ep_recover_benchmark_"+uuid4().hex
    as_of = datetime.fromisoformat(manifest["evaluation_as_of"])
    try:
        with admin.begin() as connection:
            connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public"))
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        scoped = make_url(url).update_query_dict({"options": f"-csearch_path={schema},public"})
        engine = create_engine(scoped)
        Base.metadata.create_all(engine)
        embedder = BGEEmbeddingService(cfg)
        with Session(engine) as db:
            seeded = seed_frozen_corpus(db, sources, chunks, embedder, dataset/"embedding_cache")
        assert seeded["vectors_sha256"] == meta["embeddings"]["vectors_sha256"]
        embedder.embed_query("EvidencePilot benchmark encoder warmup")
        with (attempt/"runs.jsonl").open("w", encoding="utf-8") as stream:
            for completed, index in enumerate(indices, 1):
                before = records[index]
                task, method = tasks[before["task_id"]], before["method"]
                traces.clear()
                tools = FrozenResearchTools(sources, chunks, task, embedder, as_of=as_of)
                start, state, observations, error = time.perf_counter(), None, [], None
                try:
                    with Session(engine, autoflush=False) as db:
                        answer, state, observations = run_method(method, db, task, tools, inference, cfg, as_of)
                except Exception as exc:
                    error = getattr(exc, "category", type(exc).__name__)
                    answer = {"claims": [{"claim_id": item["id"], "status": "INSUFFICIENT", "answer": "Run failed; no validated answer.", "citations": []} for item in task["required_claims"]]}
                grading = grade_answer(task, answer, sources, list(tools.seen.values()))
                if error:
                    grading["task_correct"], grading["counts"]["correct_claims"], grading["claim_accuracy"] = False, 0, 0
                known_usage = bool(traces) and all(trace["total_tokens"] is not None for trace in traces)
                row = {"task_id": task["task_id"], "category": task["category"], "query": task["query"], "method": method,
                    "mode": cfg.llm_mode, "requested_model": inference.model,
                    "actual_models": sorted({trace["actual_model"] for trace in traces if trace["actual_model"]}),
                    "answer": answer, "grading": grading, "retrieval_calls": len(tools.calls),
                    "retrieval_trace": tools.calls, "retrieved_chunk_ids": list(tools.seen), "latency_seconds": time.perf_counter()-start,
                    "token_cost": sum(trace["total_tokens"] for trace in traces) if known_usage else None,
                    "traces": list(traces), "error_category": error, "agent_state": state, "observations": observations,
                    "attempt_index": 2, "retry_reason": "inference_service_transport_outage",
                    "original_attempt_sha256": digest(json.dumps(before, sort_keys=True, ensure_ascii=False))}
                stream.write(json.dumps(row, ensure_ascii=False)+"\n")
                stream.flush()
                if error == "transport":
                    raise RuntimeError("Inference unavailable again; recovery stopped without replacing canonical results")
                assert set(row["actual_models"]) <= set(meta["actual_models"])
                assert all(trace["system_fingerprint"] in meta["system_fingerprints"] for trace in traces if trace["system_fingerprint"])
                records[index] = row
                write_json(attempt/"progress.json", {"completed": completed, "expected": len(indices), "task_id": task["task_id"], "method": method})
                print(f"RECOVERED {completed}/{len(indices)} {method} {task['task_id']} error={error}", flush=True)
        assert not any(row["error_category"] == "transport" for row in records)
        meta.update(status="complete_after_pinned_model_infrastructure_recovery", finished_at=datetime.now(timezone.utc).isoformat(),
            generation_errors=sum(bool(row["error_category"]) for row in records),
            infrastructure_outage={"original_transport_failed_records": len(indices), "original_attempt_archive": "attempts/initial_interrupted",
                "recovery_attempts": attempt.relative_to(output).as_posix(), "mode_model_weights_version_fingerprint_matched": True,
                "matrix_uses": "completed matched-model attempts; original non-transport algorithm/schema failures retained",
                "original_attempts_preserved": True, "latency_scope": "valid task attempts; outage attempts and waiting are archived separately",
                "unknown_original_token_usage_not_fabricated": True})
        summary = summarize(records)
        (output/"runs.jsonl").write_text("".join(json.dumps(row, ensure_ascii=False)+"\n" for row in records), encoding="utf-8")
        write_json(output/"results.json", {"metadata": meta, "summary": summary})
        fields = ["method", "answer_accuracy", "claim_accuracy", "evidence_coverage", "citation_precision", "unsupported_claim_rate", "avg_retrieval_calls", "avg_latency", "avg_token_cost", "task_count"]
        with (output/"results.csv").open("w", newline="", encoding="utf-8") as file:
            writer = csv.DictWriter(file, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(summary)
        cases = bad_cases(records)
        write_json(output/"bad_cases.json", {"kind": "observed_run_failures", "cases": cases, "count": len(cases)})
        write_json(output/"replay.json", replay(dataset, records))
        acceptance = json.loads((original/"acceptance.json").read_text())
        acceptance.update(status="passed" if manifest["human_reviewed"] else "partial", engineering_status="passed",
            ground_truth_status="human_reviewed" if manifest["human_reviewed"] else "pending_human_review",
            formal_superiority_claim_allowed=bool(manifest["human_reviewed"]), infrastructure_recovery_completed=True)
        acceptance["checks"]["all_tasks_have_valid_inference_attempts"] = True
        write_json(output/"acceptance.json", acceptance)
        write_json(output/"progress.json", {"completed": len(records), "expected": len(records), "transport_recovery_completed": len(indices)})
        print("COMPLETE infrastructure recovery; original schema/algorithm failures retained", flush=True)
    finally:
        inference.close()
        if engine:
            engine.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        admin.dispose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=Path("data/benchmark/research_v1"))
    parser.add_argument("--output", type=Path, default=Path("data/benchmark/research_v1/results"))
    args = parser.parse_args()
    recover(args.dataset, args.output)


if __name__ == "__main__":
    main()
