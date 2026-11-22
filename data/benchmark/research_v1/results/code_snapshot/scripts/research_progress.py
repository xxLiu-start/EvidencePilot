"""Print a compact, read-only progress snapshot for a research experiment."""
import argparse
from collections import Counter
import json
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("data/benchmark/research_v1/results"))
    args = parser.parse_args()
    path = args.output/"runs.jsonl"
    text = path.read_text() if path.exists() else ""
    rows = [json.loads(line) for line in text[:text.rfind("\n")+1].splitlines() if line.strip()]
    manifest = args.output/"run_manifest.json"
    meta = json.loads(manifest.read_text()) if manifest.exists() else {}
    results = args.output/"results.json"
    if results.exists():
        meta = json.loads(results.read_text())["metadata"]
    print(json.dumps({"completed": len(rows), "expected": len(meta.get("execution_order", [])),
        "status": meta.get("status", "initializing"),
        "per_method": dict(Counter(row["method"] for row in rows)),
        "errors": dict(Counter(row["error_category"] for row in rows if row["error_category"])),
        "token_usage_available": sum(row["token_cost"] is not None for row in rows),
        "system_fingerprints": sorted({trace["system_fingerprint"] for row in rows for trace in row["traces"] if trace["system_fingerprint"]}),
        "last": [{"method": row["method"], "task_id": row["task_id"]} for row in rows[-2:]]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
