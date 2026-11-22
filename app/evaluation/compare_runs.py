"""Compare two saved experiments, excluding runtime and latency variability."""

import argparse
import json
from pathlib import Path

from app.evaluation.benchmark import read_jsonl


def compare_runs(first: Path, second: Path):
    left = json.loads((first / "results.json").read_text())
    right = json.loads((second / "results.json").read_text())
    invariant_keys = [
        "dataset_manifest_sha256", "annotation_status", "query_count", "chunk_count", "evaluation_depth",
        "candidate_k", "rrf_k", "fusion_pool_limit", "dense_mode", "embedding_model", "embedding_revision",
        "reranker_model", "reranker_revision", "embedding_max_length", "reranker_max_length",
        "embedding_batch_size", "reranker_batch_size", "embedding_device", "reranker_device", "seed", "versions",
    ]
    mismatches = [key for key in invariant_keys if left["metadata"].get(key) != right["metadata"].get(key)]
    left_rows = {(r["query_id"], r["method"]): r for r in read_jsonl(first / "rankings.jsonl")}
    right_rows = {(r["query_id"], r["method"]): r for r in read_jsonl(second / "rankings.jsonl")}
    if left_rows.keys() != right_rows.keys() or not left_rows:
        raise ValueError("Experiments have different or empty query/method sets")
    matching, metric_delta, score_delta = 0, 0.0, 0.0
    for key, row in left_rows.items():
        other = right_rows[key]
        ids = [hit["chunk_id"] for hit in row["results"]]
        other_ids = [hit["chunk_id"] for hit in other["results"]]
        matching += ids == other_ids
        metric_delta = max(metric_delta, *(abs(row["metrics"][m] - other["metrics"][m]) for m in row["metrics"]))
        if ids == other_ids:
            score_delta = max(score_delta, *(abs(a["score"] - b["score"]) for a, b in zip(row["results"], other["results"])), 0.0)
    hashes_left, hashes_right = left["metadata"].get("code_sha256"), right["metadata"].get("code_sha256")
    if hashes_left and hashes_right and hashes_left != hashes_right:
        mismatches.append("code_sha256")
    passed = not mismatches and matching == len(left_rows) and metric_delta <= 1e-9 and score_delta <= 1e-6
    return {
        "status": "passed" if passed else "failed", "query_method_pairs": len(left_rows),
        "identical_rankings": matching, "max_metric_difference": metric_delta,
        "max_score_difference": score_delta, "configuration_mismatches": mismatches,
        "both_runs_have_code_hashes": bool(hashes_left and hashes_right),
        "code_hashes_available_in_rerun": bool(hashes_right),
        "latency_compared": False,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("first", type=Path)
    parser.add_argument("second", type=Path)
    parser.add_argument("--output", type=Path, default=Path("data/benchmark/day3/reproducibility.json"))
    args = parser.parse_args()
    result = compare_runs(args.first, args.second)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))
    if result["status"] != "passed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
