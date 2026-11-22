"""Real Day 3 API acceptance plus saved experiment/judgment checks."""

import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from app.db.session import Base
from app.evaluation.benchmark import METHODS, load_dataset
from app.retrieval.hybrid import reciprocal_rank_fusion
from scripts.acceptance import Server, make_fixtures


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=ROOT / "data/benchmark/day3")
    parser.add_argument("--results", type=Path, default=ROOT / "data/benchmark/day3/results/results.json")
    parser.add_argument("--report", type=Path, default=ROOT / "data/benchmark/day3/acceptance.json")
    args = parser.parse_args()
    url = os.environ.get("EVIDENCEPILOT_TEST_POSTGRES_URL")
    if not url:
        parser.error("Set EVIDENCEPILOT_TEST_POSTGRES_URL to a PostgreSQL test database")
    manifest, chunks, queries, qrels = load_dataset(args.dataset)
    results = json.loads(args.results.read_text(encoding="utf-8"))
    report = {"checked_at": datetime.now(timezone(timedelta(hours=8))).isoformat(),
              "status": "running", "checks": [], "annotation_status": manifest["annotation_status"]}

    def passed(name, details):
        report["checks"].append({"name": name, "passed": True, "details": details})
        print(f"PASS {name}", flush=True)

    admin = create_engine(url)
    engine = None
    schema = "ep_day3_api_" + uuid4().hex
    try:
        with admin.begin() as connection:
            connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public"))
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        scoped_url = make_url(url).update_query_dict({"options": f"-csearch_path={schema},public"})
        engine = create_engine(scoped_url)
        with engine.begin() as connection:
            Base.metadata.create_all(connection, checkfirst=False)
        with TemporaryDirectory(prefix="evidencepilot-day3-") as directory:
            os.environ["RERANKER_LOCAL_FILES_ONLY"] = "true"
            os.environ["RERANKER_DEVICE"] = "cpu"
            server = Server(scoped_url.render_as_string(hide_password=False), Path(directory))
            try:
                client = server.start()
                for path in make_fixtures(Path(directory)):
                    with path.open("rb") as file:
                        response = client.post("/api/v1/documents", files={"file": (path.name, file)},
                                               data={"chunk_size": 160, "chunk_overlap": 24})
                    assert response.status_code == 201, response.text
                query = "PostgreSQL pgvector cosine similarity search"

                def search(method, top_k=5, query=query):
                    response = client.post("/api/v1/search", json={"query": query, "method": method, "top_k": top_k})
                    assert response.status_code == 200, response.text
                    return response.json()["results"]

                lexical = search("bm25", 10)
                assert len(lexical) >= 5 and all(hit["bm25_score"] is not None for hit in lexical)
                assert search("bm25", query="day3lexicalrefreshxyz") == []
                response = client.post("/api/v1/documents", files={
                    "file": ("refresh.txt", "day3lexicalrefreshxyz demonstrates updated BM25 data")})
                assert response.status_code == 201
                new_id = response.json()["id"]
                assert search("bm25", query="day3lexicalrefreshxyz")[0]["document_id"] == new_id
                rebuild = client.post("/api/v1/search/rebuild-bm25")
                assert rebuild.status_code == 200 and rebuild.json()["indexed_chunks"] > 0
                passed("bm25", {"results": len(lexical), "automatic_update": True,
                                "manual_rebuild": rebuild.json()["indexed_chunks"]})
                dense = search("dense", 10)
                hybrid = search("hybrid")
                assert len(dense) == 10 and len(hybrid) == 5
                passed("dense_plus_bm25", {"dense_candidates": len(dense), "bm25_candidates": len(lexical), "results": len(hybrid)})
                # Re-read BM25 after corpus growth so ranks reflect the same IDF snapshot.
                lexical = search("bm25", 10)
                expected = reciprocal_rank_fusion([dense, lexical])[:5]
                assert [hit["id"] for hit in hybrid] == [key for key, score in expected]
                assert all(abs(hit["rrf_score"] - score) < 1e-10 for hit, (key, score) in zip(hybrid, expected))
                passed("rrf", {"k": 60, "deduplicated_results": len({hit["id"] for hit in hybrid})})
                reranked = search("hybrid_rerank")
                assert len(reranked) == 5
                assert all(0 <= hit["reranker_score"] <= 1 and hit["rrf_score"] is not None for hit in reranked)
                assert all(reranked[i]["score"] >= reranked[i + 1]["score"] for i in range(4))
                server.stop()
                client = server.start()
                assert search("bm25", query="day3lexicalrefreshxyz")[0]["document_id"] == new_id
                assert len(search("hybrid_rerank")) == 5
                passed("cross_encoder_reranker", {"model": "BAAI/bge-reranker-v2-m3", "results": 5,
                                                  "score_normalization": "sigmoid", "restart_verified": True})
            finally:
                server.stop()
        assert [row["method"] for row in results["summary"]] == METHODS
        assert results["metadata"]["query_count"] == len(queries) == 20
        assert all(0 <= row[key] <= 1 for row in results["summary"] for key in ["recall_at_5", "mrr", "ndcg_at_10"])
        passed("four_method_evaluation", results["summary"])
        assert len(qrels) == len(queries) and (args.dataset / "annotations.jsonl").is_file()
        passed("queries_and_judgments_saved", {"queries": len(queries), "chunks": len(chunks),
                                              "annotation_status": manifest["annotation_status"], "human_reviewed": False})
        report["status"] = "passed"
    except Exception as exc:
        report["status"] = "failed"
        report["error"] = str(exc)
        raise
    finally:
        if engine is not None:
            engine.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        admin.dispose()
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Day 3 acceptance report: {args.report}", flush=True)


if __name__ == "__main__":
    main()
