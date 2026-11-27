"""Export an official MIRACL language/split to the benchmark JSONL schema.

This uses only the query-associated positive/negative candidate passages from
MIRACL. It is a reproducible candidate-pool evaluation, not a full-corpus run.
"""
import argparse
import json
from pathlib import Path
from datasets import load_dataset

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--language", default="en")
    parser.add_argument("--split", choices=("dev", "testA"), default="dev")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--max-queries", type=int, default=0, help="Deterministic prefix for a bounded smoke benchmark; 0 uses the full split")
    args = parser.parse_args()

    rows = load_dataset("miracl/miracl", args.language, split=args.split, trust_remote_code=True)
    if args.max_queries > 0:
        rows = rows.select(range(min(args.max_queries, len(rows))))
    corpus, queries, qrels = {}, [], []
    for row in rows:
        qid = str(row["query_id"])
        queries.append({"id": qid, "text": row["query"]})
        for field, relevance in (("positive_passages", 1), ("negative_passages", 0)):
            for passage in row.get(field, []):
                docid = str(passage["docid"])
                text = (str(passage.get("title", "")) + "\n" + str(passage.get("text", ""))).strip()
                if not text:
                    continue
                previous = corpus.get(docid)
                if previous is not None and previous != text:
                    raise RuntimeError("MIRACL docid maps to conflicting text: " + docid)
                corpus[docid] = text
                if relevance:
                    qrels.append({"query_id": qid, "doc_id": docid, "relevance": relevance})

    args.output.mkdir(parents=True, exist_ok=True)
    def write(name, values):
        with (args.output / name).open("w", encoding="utf-8") as f:
            for value in values:
                f.write(json.dumps(value, ensure_ascii=False) + "\n")
    write("queries.jsonl", queries)
    write("corpus.jsonl", [{"id": k, "text": v} for k, v in sorted(corpus.items())])
    write("qrels.jsonl", qrels)
    print(json.dumps({
        "dataset": "miracl/miracl", "language": args.language, "split": args.split,
        "queries": len(queries), "candidate_corpus_documents": len(corpus),
        "positive_qrels": len(qrels), "output": str(args.output.resolve()),
    }, indent=2))

if __name__ == "__main__":
    main()
