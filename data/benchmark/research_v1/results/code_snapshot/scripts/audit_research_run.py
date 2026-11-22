"""Check experiment identity, budget bounds and deterministic grading replay."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.evaluation.baselines import METHODS
from app.evaluation.research_benchmark import replay
from app.evaluation.research_dataset import load_research_dataset, sha
from app.search.cache import write_json
from app.search.cache import digest


def audit(dataset, output):
    manifest, _, tasks, _ = load_research_dataset(dataset)
    experiment = json.loads((output/"results.json").read_text())
    meta = experiment["metadata"]
    if meta.get("reference_system_fingerprint"):
        assert meta["system_fingerprints"] == [meta["reference_system_fingerprint"]], "Mixed inference epochs"
    records = [json.loads(line) for line in (output/"runs.jsonl").read_text().splitlines() if line.strip()]
    assert meta["dataset_manifest_sha256"] == sha(dataset/"manifest.json"), "Experiment dataset identity changed"
    for name, expected in meta["code_sha256"].items():
        assert sha(output/"code_snapshot"/name) == expected, "Archived experiment implementation changed"
        assert sha(name) == expected, "Current implementation differs; use the archived experiment version"
    expected = {(method, task["task_id"]) for method in METHODS for task in tasks}
    actual = [(row["method"], row["task_id"]) for row in records]
    assert len(actual) == len(set(actual)) and set(actual) == expected, "Missing/duplicate method-task record"
    for row in records:
        assert row["retrieval_calls"] == len(row["retrieval_trace"]) <= 6
        assert row["mode"] == meta["model"]["mode"]
        assert row["requested_model"] == meta["model"]["requested_model"]
        assert set(row["actual_models"]) <= set(meta["actual_models"])
        if row["token_cost"] is not None:
            assert all(trace["total_tokens"] is not None for trace in row["traces"])
            assert row["token_cost"] == sum(trace["total_tokens"] for trace in row["traces"])
        state = row.get("agent_state")
        if state:
            assert state["round_count"] <= 12 and state["policy_step_count"] <= 12
            assert state["remaining_budget"] == 12-state["round_count"] >= 0
            assert state["retrieval_action_count"] <= 6 and state["query_rewrite_count"] <= 2
            assert max(state["claim_retrieval_counts"].values(), default=0) <= 3
    repeated = replay(dataset, records)
    assert repeated["summary"] == experiment["summary"], "Summary replay differs"
    cases = json.loads((output/"bad_cases.json").read_text())["cases"]
    assert len(cases) >= 5 and all((case["method"], case["task_id"]) in expected for case in cases)
    acceptance = json.loads((output/"acceptance.json").read_text())
    assert acceptance["formal_superiority_claim_allowed"] == manifest["human_reviewed"]
    preserved = None
    initial = output/"attempts/initial_interrupted/runs.jsonl"
    if initial.exists():
        originals = [json.loads(line) for line in initial.read_text().splitlines() if line.strip()]
        by_key = {(row["method"], row["task_id"]): row for row in records}
        for before in originals:
            after = by_key[(before["method"], before["task_id"])]
            if before["error_category"] != "transport":
                assert before == after, "A non-infrastructure outcome was replaced"
            else:
                assert after["retry_reason"] == "inference_service_transport_outage"
                assert after["original_attempt_sha256"] == digest(json.dumps(before, sort_keys=True, ensure_ascii=False))
                assert after["error_category"] != "transport"
        preserved = True
    result = {"status": "passed", "records": len(records), "dataset_identity_matches": True,
        "code_snapshot_and_current_implementation_match": True, "all_hard_budgets_respected": True,
        "same_mode_and_model": True, "token_accounting_matches_api_traces": True,
        "offline_replay_and_summary_identical": True, "observed_bad_cases": len(cases),
        "human_reviewed": manifest["human_reviewed"], "formal_comparison_provisional": not manifest["human_reviewed"],
        "generation_errors_included_in_scores": meta["generation_errors"]}
    result["non_transport_outcomes_preserved_exactly"] = preserved
    result["audit_script_sha256"] = sha(__file__)
    write_json(output/"integrity_audit.json", result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=Path("data/benchmark/research_v1"))
    parser.add_argument("--output", type=Path, default=Path("data/benchmark/research_v1/results"))
    args = parser.parse_args()
    print(json.dumps(audit(args.dataset, args.output), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
