"""Run 20 frozen research tasks, three baselines, EvidencePilot and three ablations.

EVIDENCEPILOT_TEST_POSTGRES_URL=... python -m app.evaluation.research_benchmark
"""
import argparse
import csv
from datetime import datetime, timezone
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import random
import time
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import httpx
from dataclasses import asdict
from pydantic import BaseModel
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.config import settings
from app.db.session import Base
from app.evaluation.baselines import ANSWER_PROMPT, METHODS, NO_STATE_PROMPT, PROMPT_VERSION, REACT_PROMPT, run_method
from app.evaluation.benchmark import model_revision
from app.evaluation.frozen_research import FrozenResearchTools, seed_frozen_corpus
from app.evaluation.research_dataset import load_research_dataset, sha
from app.evaluation.research_metrics import calculate_metrics, grade_answer
from app.llm_client import ReasoningClient, selected_branch
from app.retrieval.embedding import BGEEmbeddingService
from app.search.cache import digest, write_json


class IdentityProbe(BaseModel):
    ok: bool


def identity_matches(traces, model, fingerprint):
    return all((not trace["actual_model"] or trace["actual_model"] == model)
               and (not trace["system_fingerprint"] or trace["system_fingerprint"] == fingerprint)
               for trace in traces)


def service_available(config):
    base, _, _ = selected_branch(config)
    parts = urlsplit(base)
    try:
        response = httpx.get(urlunsplit((parts.scheme, parts.netloc, "/health", "", "")), timeout=5, trust_env=False)
        return True if response.status_code == 200 else False if response.status_code >= 500 else None
    except httpx.HTTPError:
        return False


def model_metadata(config):
    base, requested_model, _ = selected_branch(config)
    info = {"mode": config.llm_mode, "requested_model": requested_model, "server_version": None,
            "local_config_sha256": {}, "weight_file_manifest": [], "weight_hashes_computed": False}
    if config.llm_mode == "local":
        root = Path(requested_model)
        if root.is_dir():
            info["local_config_sha256"] = {name: sha(root/name) for name in (
                "config.json", "tokenizer_config.json", "generation_config.json", "model.safetensors.index.json") if (root/name).is_file()}
            info["weight_file_manifest"] = [{"name": path.name, "bytes": path.stat().st_size,
                "mtime_ns": path.stat().st_mtime_ns} for path in sorted(root.glob("*.safetensors"))]
        parsed = urlsplit(base)
        version_url = urlunsplit((parsed.scheme, parsed.netloc, "/version", "", ""))
        try:
            response = httpx.get(version_url, timeout=5, trust_env=False)
            if response.status_code == 200:
                info["server_version"] = response.json()
        except (httpx.HTTPError, ValueError):
            pass
    return info


def summarize(records):
    return [{"method": method, **calculate_metrics([row for row in records if row["method"] == method])}
            for method in METHODS if any(row["method"] == method for row in records)]


def bad_cases(records):
    failures = [row for row in records if not row["grading"]["task_correct"]]
    chosen = []
    used = set()
    # Prefer diverse task types; every entry points to an actual failed run.
    for category in ("outdated", "source_conflict", "multi_hop", "multi_source", "single_source", "unanswerable"):
        match = next((row for row in failures if row["category"] == category and (row["method"], row["task_id"]) not in used), None)
        if match:
            chosen.append(match)
            used.add((match["method"], match["task_id"]))
    for row in failures:
        if len(chosen) >= 5:
            break
        if (row["method"], row["task_id"]) not in used:
            chosen.append(row)
            used.add((row["method"], row["task_id"]))
    output = []
    for row in chosen[:5]:
        missing = [item for item in row["grading"]["details"] if not item["correct"]]
        counts = row["grading"]["counts"]
        diagnosis = ("Obsolete/version-mismatched evidence was not handled as required." if row["category"] == "outdated" else
                     "Model/index/version qualifications or required corroborating sources were missed." if row["category"] == "source_conflict" else
                     "Required claims lack exact qualifying citations or complete answer facts.")
        if counts["unsupported_claims"]:
            diagnosis += " The method asserted at least one insufficiently supported claim."
        repeats = len(row["retrieval_trace"]) - len({(item["action"], item["query"].strip().casefold()) for item in row["retrieval_trace"]})
        if repeats:
            diagnosis += f" Repeated identical retrieval actions: {repeats}."
        output.append({"task_id": row["task_id"], "method": row["method"], "category": row["category"],
                       "query": row["query"], "answer": row["answer"], "failed_claims": missing,
                       "diagnosis": diagnosis, "retrieval_trace": row["retrieval_trace"],
                       "suggested_followup": "Review original annotated passages, source/version qualifications, retrieval coverage and stopping decisions before changing labels."})
    return output


def replay(dataset, records):
    _, sources, tasks, chunks = load_research_dataset(dataset)
    tasks = {task["task_id"]: task for task in tasks}
    chunks = {chunk["id"]: chunk for chunk in chunks}
    for row in records:
        result = grade_answer(tasks[row["task_id"]], row["answer"], sources, [chunks[key] for key in row["retrieved_chunk_ids"]])
        if row["error_category"]:
            result["task_correct"] = False
            result["counts"]["correct_claims"] = 0
            result["claim_accuracy"] = 0
        if result != row["grading"]:
            raise ValueError("Scoring replay changed")
    return {"status": "passed", "record_count": len(records), "network_requests": 0, "llm_requests": 0,
            "identical_grades": True, "summary": summarize(records),
            "note": "Deterministic scoring replay; live generation can vary even with identical temperature/settings."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=Path("data/benchmark/research_v1"))
    parser.add_argument("--output", type=Path, default=Path("data/benchmark/research_v1/results"))
    parser.add_argument("--task-ids", nargs="+")
    parser.add_argument("--methods", nargs="+", choices=METHODS)
    parser.add_argument("--replay", action="store_true")
    parser.add_argument("--resume", action="store_true", help="Resume only the same sealed dataset/code/model/fingerprint")
    args = parser.parse_args()
    records_path = args.output/"runs.jsonl"
    if args.replay:
        records = [json.loads(line) for line in records_path.read_text().splitlines() if line.strip()]
        write_json(args.output/"replay.json", replay(args.dataset, records))
        print("PASS deterministic offline scoring replay", flush=True)
        return
    if records_path.exists() and not args.resume:
        parser.error("Output already contains a run; select a new output directory")
    url = os.environ.get("EVIDENCEPILOT_TEST_POSTGRES_URL")
    if not url:
        parser.error("Set EVIDENCEPILOT_TEST_POSTGRES_URL to an isolated PostgreSQL database")
    manifest, sources, tasks, chunks = load_research_dataset(args.dataset)
    if args.task_ids:
        if not set(args.task_ids) <= {task["task_id"] for task in tasks}:
            parser.error("Unknown task ID")
        tasks = [task for task in tasks if task["task_id"] in args.task_ids]
    methods = args.methods or METHODS
    jobs = [(method, task) for method in methods for task in tasks]
    random.Random(0).shuffle(jobs)
    as_of = datetime.fromisoformat(manifest["evaluation_as_of"])
    cfg = settings.model_copy(update={"embedding_local_files_only": True, "embedding_device": "cpu", "embedding_use_fp16": False})
    # The same immutable reasoning client is shared by all methods and ablations.
    traces = []
    client = ReasoningClient(cfg, trace_sink=traces.append)
    probe, identity = client.complete_json([
        {"role": "system", "content": 'Return only JSON {"ok":true}.'},
        {"role": "user", "content": "Non-sensitive fixed-model identity check."},
    ], IdentityProbe, operation="benchmark_identity_probe", max_tokens=50, timeout_seconds=20)
    if not probe.ok or not identity.actual_model:
        client.close()
        raise ValueError("No verified inference identity")
    meta = {"created_at": datetime.now(timezone.utc).isoformat(), "status": "running",
            "dataset_manifest_sha256": sha(args.dataset/"manifest.json"), "annotation_status": manifest["annotation_status"],
            "human_reviewed": manifest["human_reviewed"], "limitations": manifest["limitations"],
            "task_count": len(tasks), "methods": list(methods), "model": model_metadata(cfg),
            "reference_actual_model": identity.actual_model, "reference_system_fingerprint": identity.system_fingerprint,
            "identity_probe": asdict(identity),
            "execution_order_seed": 0, "generation_seed": None,
            "execution_order": [{"method": method, "task_id": task["task_id"]} for method, task in jobs],
            "prompt_version": PROMPT_VERSION, "prompt_hashes": {name: digest(value) for name, value in {
                "answer": ANSWER_PROMPT, "react": REACT_PROMPT, "no_state": NO_STATE_PROMPT}.items()},
            "code_sha256": {path.as_posix(): sha(path) for path in sorted(Path("app").rglob("*.py"))},
            "parameters": {"temperature": 0, "llm_timeout_seconds": cfg.llm_timeout_seconds,
                "llm_max_retries": cfg.llm_max_retries, "disable_thinking": cfg.llm_disable_thinking,
                "retrieval_budget": 6, "per_claim_retrieval_budget": 3, "max_tool_calls": 12,
                "max_steps": 12, "top_k": 5, "max_final_context_chunks": 10,
                "chunk_size": 500, "chunk_overlap": 80, "freshness_days": cfg.agent_evidence_max_age_days,
                "evaluation_as_of": manifest["evaluation_as_of"], "tool_timeout_seconds": 30,
                "max_tokens": {"answer": 2400, "react_action": 400, "planner": 1800, "router": 900, "verification": 1500, "extraction": 2200}},
            "retrieval": {"dense": "real BGE-M3 + PostgreSQL exact cosine", "web": "BM25 frozen official-page windows",
                "external_search_calls_during_evaluation": 0,
                "source_origin": "RAG snapshots preserve verified external official URL provenance"},
            "latency_unit": "seconds", "token_cost_unit": "API-reported total tokens; not money",
            "latency_note": "Corpus/model warmup excluded; task timer includes query embeddings, model calls and DB work. Sequential local run.",
            "shared_server_note": "Existing local inference server is reused; unrelated server traffic is not controlled. Latency is descriptive, not a controlled throughput claim.",
            "python": platform.python_version(), "versions": {name: importlib.metadata.version(name) for name in (
                "langgraph", "FlagEmbedding", "transformers", "torch", "openai", "rank-bm25", "pgvector", "pydantic")}}
    args.output.mkdir(parents=True, exist_ok=True)
    records = []
    if args.resume:
        saved = json.loads((args.output/"run_manifest.json").read_text())
        for key in ("dataset_manifest_sha256", "code_sha256", "parameters", "execution_order", "model",
                    "reference_actual_model", "reference_system_fingerprint"):
            if saved[key] != meta[key]:
                client.close()
                raise ValueError("Cannot resume changed experiment: " + key)
        records = [json.loads(line) for line in records_path.read_text().splitlines() if line.strip()]
        completed = {(row["method"], row["task_id"]) for row in records}
        if len(completed) != len(records):
            raise ValueError("Duplicate prior records")
        jobs = [(method, task) for method, task in jobs if (method, task["task_id"]) not in completed]
        meta["resumed_from_record_count"] = len(records)
        meta["created_at"] = saved["created_at"]
    write_json(args.output/"run_manifest.json", meta)
    admin, engine = create_engine(url), None
    schema = "ep_research_" + uuid4().hex
    try:
        with admin.begin() as conn:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public"))
            conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        scoped = make_url(url).update_query_dict({"options": f"-csearch_path={schema},public"})
        engine = create_engine(scoped)
        Base.metadata.create_all(engine)
        embedder = BGEEmbeddingService(cfg)
        with Session(engine) as db:
            meta["embeddings"] = seed_frozen_corpus(db, sources, chunks, embedder, args.dataset/"embedding_cache")
        meta["embeddings"].update(model=embedder.model_name,
            revision=model_revision(embedder.model_name, cfg.embedding_cache_dir, cfg.embedding_revision))
        embedder.embed_query("EvidencePilot benchmark encoder warmup")
        print(f"READY {len(tasks)} tasks x {len(methods)} methods, {len(chunks)} frozen chunks", flush=True)
        with records_path.open("a" if args.resume else "w", encoding="utf-8") as stream:
            for method, task in jobs:
                traces.clear()
                tools = FrozenResearchTools(sources, chunks, task, embedder, as_of=as_of)
                start, state, observations, error = time.perf_counter(), None, [], None
                try:
                    with Session(engine, autoflush=False) as db:
                        answer, state, observations = run_method(method, db, task, tools, client, cfg, as_of)
                except Exception as exc:
                    error = getattr(exc, "category", type(exc).__name__)
                    answer = {"claims": [{"claim_id": item["id"], "status": "INSUFFICIENT", "answer": "Run failed; no validated answer.", "citations": []} for item in task["required_claims"]]}
                elapsed = time.perf_counter()-start
                grading = grade_answer(task, answer, sources, list(tools.seen.values()))
                if error:
                    grading["task_correct"], grading["counts"]["correct_claims"], grading["claim_accuracy"] = False, 0, 0
                usage_complete = bool(traces) and all(trace["total_tokens"] is not None for trace in traces)
                row = {"task_id": task["task_id"], "category": task["category"], "query": task["query"], "method": method,
                       "mode": cfg.llm_mode, "requested_model": client.model,
                       "actual_models": sorted({trace["actual_model"] for trace in traces if trace["actual_model"]}),
                       "answer": answer, "grading": grading, "retrieval_calls": len(tools.calls),
                       "retrieval_trace": tools.calls, "retrieved_chunk_ids": list(tools.seen),
                       "latency_seconds": elapsed, "token_cost": sum(trace["total_tokens"] for trace in traces) if usage_complete else None,
                       "traces": list(traces), "error_category": error, "agent_state": state, "observations": observations}
                changed_identity = not identity_matches(traces, identity.actual_model, identity.system_fingerprint)
                transport_seen = error == "transport" or any(trace["error_category"] == "transport" for trace in traces)
                unavailable = transport_seen and service_available(cfg) is False
                if changed_identity or unavailable:
                    # Preserve the interrupted attempt, but stop instead of racing
                    # through the remaining tasks under an unavailable/new backend.
                    reason = "inference_identity_changed" if changed_identity else "inference_service_unavailable"
                    with (args.output/"interrupted_attempts.jsonl").open("a", encoding="utf-8") as attempts:
                        attempts.write(json.dumps({**row, "interruption_reason": reason}, ensure_ascii=False)+"\n")
                    write_json(args.output/"run_manifest.json", {**meta, "status": "interrupted", "interruption_reason": reason})
                    write_json(args.output/"progress.json", {"completed": len(records), "expected": len(tasks)*len(methods), "status": "interrupted", "reason": reason})
                    raise RuntimeError(reason + "; resume only when the pinned identity is restored")
                records.append(row)
                stream.write(json.dumps(row, ensure_ascii=False)+"\n")
                stream.flush()
                print(f"DONE {method} {task['task_id']} correct={grading['task_correct']} calls={len(tools.calls)} tokens={row['token_cost']} error={error}", flush=True)
                write_json(args.output/"progress.json", {"completed": len(records), "expected": len(tasks)*len(methods), "last_method": method, "last_task": task["task_id"]})
        observed_modes = {row["mode"] for row in records}
        actual_models = {model for row in records for model in row["actual_models"]}
        if len(observed_modes) != 1 or len(actual_models) != 1:
            raise ValueError("Methods did not share one actual inference mode/model")
        meta.update(status="complete", actual_models=sorted(actual_models), finished_at=datetime.now(timezone.utc).isoformat(),
                    system_fingerprints=sorted({trace["system_fingerprint"] for row in records for trace in row["traces"] if trace["system_fingerprint"]}),
                    generation_errors=sum(bool(row["error_category"]) for row in records))
        summary = summarize(records)
        write_json(args.output/"results.json", {"metadata": meta, "summary": summary})
        keys = ["method", "answer_accuracy", "claim_accuracy", "evidence_coverage", "citation_precision", "unsupported_claim_rate", "avg_retrieval_calls", "avg_latency", "avg_token_cost", "task_count"]
        with (args.output/"results.csv").open("w", newline="", encoding="utf-8") as file:
            writer = csv.DictWriter(file, fieldnames=keys, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(summary)
        cases = bad_cases(records)
        write_json(args.output/"bad_cases.json", {"kind": "observed_run_failures", "cases": cases, "count": len(cases)})
        write_json(args.output/"replay.json", replay(args.dataset, records))
        complete = len(tasks) == 20 and set(methods) == set(METHODS)
        engineering_passed = complete and len(cases) >= 5
        write_json(args.output/"acceptance.json", {"status": ("passed" if manifest["human_reviewed"] else "partial") if engineering_passed else "smoke",
            "engineering_status": "passed" if engineering_passed else "smoke",
            "ground_truth_status": "human_reviewed" if manifest["human_reviewed"] else "pending_human_review",
            "formal_superiority_claim_allowed": bool(manifest["human_reviewed"]),
            "checks": {"fixed_tasks_20": len(tasks) == 20, "three_baselines": set(METHODS[:3]) <= set(methods),
                "automatic_metrics": True, "three_ablations": set(METHODS[4:]) <= set(methods), "five_observed_bad_cases": len(cases) >= 5,
                "frozen_snapshots_and_replay": True, "same_mode_and_actual_model": True}, "record_count": len(records)})
        print("COMPLETE research benchmark; human review status:", manifest["annotation_status"], flush=True)
        for row in summary:
            print(row["method"], json.dumps({key: row[key] for key in keys[1:]}, ensure_ascii=False), flush=True)
    finally:
        client.close()
        if engine is not None:
            engine.dispose()
        with admin.begin() as conn:
            conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        admin.dispose()


if __name__ == "__main__":
    main()
