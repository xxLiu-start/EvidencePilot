"""Post-hoc paired comparisons and transparent failure evidence, without regrading."""
import argparse
import csv
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import numpy as np
from app.evaluation.baselines import METHODS
from app.evaluation.research_dataset import load_research_dataset, sha
from app.search.cache import write_json


def analyze(dataset, output):
    manifest, sources, tasks, chunks = load_research_dataset(dataset)
    data = json.loads((output/"results.json").read_text())
    if not data["metadata"]["status"].startswith("complete"):
        raise ValueError("Interrupted infrastructure attempts cannot be used as a completed comparison")
    rows = [json.loads(line) for line in (output/"runs.jsonl").read_text().splitlines() if line.strip()]
    assert data["metadata"]["dataset_manifest_sha256"] == sha(dataset/"manifest.json")
    by_key = {(row["method"], row["task_id"]): row for row in rows}
    tasks_by_id = {task["task_id"]: task for task in tasks}
    chunks_by_id = {chunk["id"]: chunk for chunk in chunks}
    summaries = {row["method"]: row for row in data["summary"]}
    ours = summaries["EvidencePilot"]
    rng = np.random.default_rng(0)
    indices = rng.integers(0, len(tasks), size=(10000, len(tasks)))
    comparisons = []
    for method in METHODS:
        if method == "EvidencePilot":
            continue
        differences = np.array([int(by_key[("EvidencePilot", task["task_id"])]["grading"]["task_correct"])
                                - int(by_key[(method, task["task_id"])]["grading"]["task_correct"]) for task in tasks])
        boot = differences[indices].mean(axis=1)
        comparisons.append({"comparison": "EvidencePilot minus " + method,
            "accuracy_difference": float(differences.mean()),
            "paired_bootstrap_95_interval": [float(value) for value in np.quantile(boot, [.025, .975])],
            "task_wins": int((differences > 0).sum()), "task_losses": int((differences < 0).sum()),
            "task_ties": int((differences == 0).sum()),
            "retrieval_call_difference": ours["avg_retrieval_calls"]-summaries[method]["avg_retrieval_calls"],
            "latency_seconds_difference": ours["avg_latency"]-summaries[method]["avg_latency"]})
    failures = []
    for row in rows:
        if row["grading"]["task_correct"]:
            continue
        state = row.get("agent_state") or {}
        normalized = [(item["action"], re.sub(r"\W+", " ", item["query"].casefold()).strip()) for item in row["retrieval_trace"]]
        repeats = len(normalized)-len(set(normalized))
        citation_failures = []
        gold = {claim["id"]: claim for claim in tasks_by_id[row["task_id"]]["required_claims"]}
        for claim in row["answer"]["claims"]:
            bad = next((item for item in row["grading"]["details"] if item["claim_id"] == claim["claim_id"]), {})
            for citation in claim.get("citations", []):
                if citation["evidence_id"] not in bad.get("invalid_citation_ids", []):
                    continue
                chunk = chunks_by_id.get(citation["evidence_id"])
                exact = bool(chunk and citation["supporting_text"] in chunk["text"])
                ref_sources = {ref["source_id"] for ref in gold.get(claim["claim_id"], {}).get("reference_evidence", [])}
                kind = ("fabricated_or_altered_quote" if not exact else
                        "outside_annotated_passages_review_qrels" if chunk["source_id"] in ref_sources else
                        "source_not_qualified_for_this_claim")
                citation_failures.append({"claim_id": claim["claim_id"], "kind": kind,
                    "source_id": chunk["source_id"] if chunk else None, "quote": citation["supporting_text"],
                    "exact_in_retrieved_chunk": exact})
        diagnoses = []
        if row["error_category"]:
            diagnoses.append("Model response failed schema/inference validation; the failed attempt remains in accuracy and cost denominators.")
        if row["category"] == "outdated":
            diagnoses.append("Compare predicted status to the required old-version/date handling; no current fact may be asserted from the historical-only corpus.")
        if row["category"] == "source_conflict":
            diagnoses.append("Check model/index/version qualifiers and whether citations cover both sides; differing scopes do not by themselves prove contradiction.")
        if state.get("termination_action") == "STOP" and state.get("coverage", 0) < 1:
            diagnoses.append("Critical-only stopping left planned noncritical claims uncovered; inspect priorities against the benchmark's required claims.")
        if repeats:
            diagnoses.append(f"Repeated normalized retrieval action/query pairs: {repeats}; these consume retrieval budget.")
        if any(item["kind"] == "outside_annotated_passages_review_qrels" for item in citation_failures):
            diagnoses.append("A real quote comes from an approved source but outside initial qrels. This may be an annotation gap; do not call it a factual model error without human review.")
        if any(item["kind"] == "fabricated_or_altered_quote" for item in citation_failures):
            diagnoses.append("At least one quote is not a contiguous substring of its claimed retrieved chunk; punctuation/Markdown edits also violate exact citation provenance.")
        if any(not detail["answer_patterns_passed"] and detail["actual_status"] == detail["expected_status"]
               for detail in row["grading"]["details"]):
            diagnoses.append("An answer-pattern check failed despite a matching status. Review possible paraphrase/notation gaps in the initial rubric before treating it as a semantic error.")
        if not diagnoses:
            diagnoses.append("The answer did not cover all required facts and qualified evidence groups; compare the exact failed-claim details.")
        failures.append({"task_id": row["task_id"], "method": row["method"], "category": row["category"],
            "query": row["query"], "diagnoses": diagnoses, "citation_failures": citation_failures,
            "answer": row["answer"], "grading": row["grading"], "retrieval_trace": row["retrieval_trace"],
            "termination_action": state.get("termination_action"), "stop_reason": state.get("stop_reason"),
            "agent_coverage": state.get("coverage"), "repeated_retrieval_pairs": repeats,
            "review_status": "pending_human_review"})
    # Choose five diverse *observed* failures, then retain all others for audit.
    selected, used = [], set()
    predicates = [lambda row: row["category"] == "outdated",
        lambda row: row["termination_action"] == "STOP" and (row["agent_coverage"] or 0) < 1,
        lambda row: row["repeated_retrieval_pairs"] > 0,
        lambda row: row["category"] == "source_conflict",
        lambda row: any(item["kind"] == "outside_annotated_passages_review_qrels" for item in row["citation_failures"]),
        lambda row: any(item["kind"] == "fabricated_or_altered_quote" for item in row["citation_failures"]),
        lambda row: row["category"] == "multi_hop", lambda row: True]
    for predicate in predicates:
        row = next((item for item in failures if item["task_id"] not in used and predicate(item)), None)
        if row and len(selected) < 5:
            selected.append(row)
            used.add(row["task_id"])
    for row in failures:
        if len(selected) >= 5:
            break
        if row["task_id"] not in used:
            selected.append(row)
            used.add(row["task_id"])
    assert len(selected) >= 5
    write_json(output/"bad_cases.json", {"kind": "observed_failures_with_annotation_uncertainty_explicit",
        "count": len(selected), "cases": selected, "total_failed_records": len(failures),
        "note": "Scoring failures include provenance errors, incomplete evidence and possible initial-qrel gaps; review before assigning semantic blame."})
    write_json(output/"failure_catalog.json", {"failures": failures})
    analysis = {"annotation_status": manifest["annotation_status"], "human_reviewed": manifest["human_reviewed"],
        "analysis_script_sha256": sha(__file__), "bootstrap_seed": 0, "bootstrap_draws": 10000,
        "comparisons": comparisons, "failed_records": len(failures),
        "conclusion_scope": "Provisional fixed-suite rule scores. Intervals describe these 20 tasks only; human label review and a broader task set are needed for a superiority claim.",
        "source_selection_scope": "Each task uses the same preselected allowed_source_ids for every method. This evaluates reasoning on a controlled corpus, not discovery of sources on the open web.",
        "conflict_suite_scope": "Primarily false-positive conflicts caused by model/index/version differences; does not establish recall on independent same-scope irreconcilable official-source conflicts."}
    analysis["execution_outcomes"] = {
        method: {
            "task_count": sum(row["method"] == method for row in rows),
            "generation_errors": sum(bool(row["error_category"]) for row in rows if row["method"] == method),
            "tool_timeouts": sum((row.get("agent_state") or {}).get("stop_reason") == "tool_timeout"
                                 for row in rows if row["method"] == method),
            "unknown_token_usage_tasks": sum(row["token_cost"] is None for row in rows if row["method"] == method),
        }
        for method in METHODS
    }
    total_claims = sum(len(task["required_claims"]) for task in tasks)
    supported_claims = sum(claim["expected_status"] == "SUPPORTED" for task in tasks for claim in task["required_claims"])
    analysis["coverage_denominator"] = {"required_claims": total_claims, "reference_supported_claims": supported_claims,
        "reference_maximum_coverage": supported_claims/total_claims,
        "note": "Outdated/unanswerable claims remain in the denominator and cannot count as supported coverage, even when correctly abstained."}
    write_json(output/"analysis.json", analysis)
    fields = ["method", "answer_accuracy", "evidence_coverage", "citation_precision", "unsupported_claim_rate",
              "avg_retrieval_calls", "avg_latency", "avg_token_cost", "token_usage_available_tasks", "task_count", "mode", "actual_model",
              "annotation_status", "human_reviewed"]
    with (output/"comparison.csv").open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(file, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for summary in data["summary"]:
            writer.writerow({**summary, "mode": data["metadata"]["model"]["mode"],
                "actual_model": " | ".join(data["metadata"]["actual_models"]),
                "annotation_status": manifest["annotation_status"], "human_reviewed": manifest["human_reviewed"]})
    return analysis


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=Path("data/benchmark/research_v1"))
    parser.add_argument("--output", type=Path, default=Path("data/benchmark/research_v1/results"))
    args = parser.parse_args()
    result = analyze(args.dataset, args.output)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
