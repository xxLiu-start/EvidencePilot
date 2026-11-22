"""Reproducible real-model benchmark in a disposable PostgreSQL schema.

EVIDENCEPILOT_TEST_POSTGRES_URL=... python -m app.evaluation.benchmark
"""

import argparse
import csv
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import time
from uuid import UUID, uuid4

import numpy as np
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.config import settings
from app.db.models import DocumentChunk, DocumentRecord
from app.db.schema import initialize_database
from app.db.session import Base
from app.evaluation.metrics import evaluate_ranking
from app.evaluation.prepare import sha256
from app.retrieval.bm25 import BM25Store
from app.retrieval.dense import search_chunks
from app.retrieval.embedding import get_embedding_service, validate_embeddings
from app.retrieval.hybrid import fuse_hits
from app.retrieval.reranker import get_reranker_service


METHODS = ["Dense", "BM25", "Hybrid", "Hybrid + Reranker"]


def read_jsonl(path):
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]


def load_dataset(directory):
    directory = Path(directory)
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    for name, digest in manifest["files"].items():
        if sha256(directory / name) != digest:
            raise ValueError(f"Dataset hash mismatch: {name}")
    chunks = read_jsonl(directory / "chunks.jsonl")
    queries = read_jsonl(directory / "queries.jsonl")
    qrels = json.loads((directory / "qrels.json").read_text(encoding="utf-8"))
    chunk_ids = {chunk["id"] for chunk in chunks}
    if len(chunk_ids) != len(chunks) or len({q["id"] for q in queries}) != len(queries):
        raise ValueError("Duplicate chunk/query IDs")
    if not 15 <= len(queries) <= 20:
        raise ValueError("Day 3 benchmark requires 15–20 queries")
    for query in queries:
        labels = qrels.get(query["id"], {})
        if not query["query"].strip() or not labels or not set(labels) <= chunk_ids:
            raise ValueError(f"Invalid query/judgment IDs: {query['id']}")
        if any(not isinstance(g, int) or not 0 <= g <= 3 for g in labels.values()) or not any(labels.values()):
            raise ValueError("Judgments require graded relevance 0–3 and at least one positive")
    return manifest, chunks, queries, qrels


def seed_corpus(db, chunks, embedder):
    vectors = validate_embeddings(embedder.embed_documents([c["text"] for c in chunks]), len(chunks))
    grouped = {}
    for chunk in chunks:
        grouped.setdefault(chunk["document_id"], []).append(chunk)
    try:
        for document_id, items in grouped.items():
            db.add(DocumentRecord(id=UUID(document_id), filename=Path(items[0]["source"]).name,
                                  source=items[0]["source"], version=items[0]["version"], chunk_count=len(items)))
        db.flush()
        for chunk, vector in zip(chunks, vectors, strict=True):
            fields = {**chunk, "id": UUID(chunk["id"]), "document_id": UUID(chunk["document_id"])}
            db.add(DocumentChunk(**fields, embedding=vector, embedding_model=embedder.model_name))
        db.commit()
    except Exception:
        db.rollback()
        raise


def model_revision(model, cache_dir, revision="main"):
    path = Path(model)
    if path.is_dir():
        candidates = sorted(path.glob("*.safetensors")) + sorted(path.glob("pytorch_model*.bin"))
        return {"local_weights": {p.name: sha256(p) for p in candidates}}
    if len(revision) == 40 and all(c in "0123456789abcdef" for c in revision):
        return {"hub_commit": revision}
    ref = Path(cache_dir) / ("models--" + model.replace("/", "--")) / "refs" / revision
    return {"hub_commit": ref.read_text().strip() if ref.exists() else "unknown"}


def run_experiment(db, queries, qrels, *, embedder, reranker, candidate_k=10, rrf_k=60, progress=None):
    store = BM25Store()
    store.rebuild(db)
    records = []
    for query in queries:
        start = time.perf_counter()
        dense = search_chunks(db, query["query"], top_k=candidate_k, embedder=embedder)
        dense_ms = (time.perf_counter() - start) * 1000
        start = time.perf_counter()
        lexical = store.retrieve(db, query["query"], top_k=candidate_k)
        lexical_ms = (time.perf_counter() - start) * 1000
        start = time.perf_counter()
        hybrid = fuse_hits([dense, lexical], k=rrf_k, top_k=2 * candidate_k)
        fusion_ms = (time.perf_counter() - start) * 1000
        start = time.perf_counter()
        reranked = reranker.rerank_hits(query["query"], hybrid, top_k=10)
        rerank_ms = (time.perf_counter() - start) * 1000
        stages = [dense[:10], lexical[:10], hybrid[:10], reranked]
        latencies = [dense_ms, lexical_ms, dense_ms + lexical_ms + fusion_ms,
                     dense_ms + lexical_ms + fusion_ms + rerank_ms]
        for method, hits, latency in zip(METHODS, stages, latencies, strict=True):
            ids = [str(hit.chunk.id) for hit in hits]
            records.append({
                "query_id": query["id"], "method": method, "query": query["query"],
                "metrics": evaluate_ranking(ids, qrels[query["id"]]), "latency_ms": latency,
                "results": [{"chunk_id": str(hit.chunk.id), "score": hit.score,
                             "dense_score": hit.dense_score, "bm25_score": hit.bm25_score,
                             "rrf_score": hit.rrf_score, "reranker_score": hit.reranker_score} for hit in hits],
            })
        if progress:
            progress(query["id"])
    summary = []
    for method in METHODS:
        rows = [r for r in records if r["method"] == method]
        summary.append({"method": method, **{
            key: float(np.mean([r["metrics"][key] for r in rows]))
            for key in ["recall_at_5", "mrr", "ndcg_at_10"]},
            "mean_latency_ms": float(np.mean([r["latency_ms"] for r in rows]))})
    return summary, records


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=Path("data/benchmark/day3"))
    parser.add_argument("--output", type=Path, default=Path("data/benchmark/day3/results"))
    args = parser.parse_args()
    url = os.environ.get("EVIDENCEPILOT_TEST_POSTGRES_URL")
    if not url:
        parser.error("Set EVIDENCEPILOT_TEST_POSTGRES_URL to an isolated PostgreSQL test database")
    manifest, chunks, queries, qrels = load_dataset(args.dataset)
    import torch

    torch.manual_seed(0)
    np.random.seed(0)
    admin = create_engine(url)
    engine = None
    schema = "ep_day3_" + uuid4().hex
    try:
        with admin.begin() as connection:
            connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public"))
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        scoped_url = make_url(url).update_query_dict({"options": f"-csearch_path={schema},public"})
        engine = create_engine(scoped_url)
        with engine.begin() as connection:
            Base.metadata.create_all(connection, checkfirst=False)
        initialize_database(engine)
        embedder, reranker = get_embedding_service(), get_reranker_service()
        with Session(engine) as db:
            seed_corpus(db, chunks, embedder)
            # Fix exact dense rankings for the small benchmark; production keeps its index.
            db.execute(text("SET LOCAL enable_indexscan = off"))
            summary, records = run_experiment(
                db, queries, qrels, embedder=embedder, reranker=reranker,
                progress=lambda query_id: print(f"Evaluated {query_id}", flush=True),
            )
        metadata = {
            "created_at": datetime.now(timezone(timedelta(hours=8))).isoformat(),
            "dataset_manifest_sha256": sha256(args.dataset / "manifest.json"),
            "annotation_status": manifest["annotation_status"], "human_reviewed": manifest["human_reviewed"],
            "query_count": len(queries), "chunk_count": len(chunks), "evaluation_depth": 10,
            "candidate_k": 10, "rrf_k": 60, "fusion_pool_limit": 20, "dense_mode": "exact",
            "embedding_model": embedder.model_name, "embedding_revision": model_revision(embedder.model_name, settings.embedding_cache_dir, settings.embedding_revision),
            "reranker_model": reranker.model_name, "reranker_revision": model_revision(reranker.model_name, settings.reranker_cache_dir, settings.reranker_revision),
            "embedding_max_length": settings.embedding_max_length, "reranker_max_length": settings.reranker_max_length,
            "embedding_batch_size": settings.embedding_batch_size, "reranker_batch_size": settings.reranker_batch_size,
            "embedding_device": settings.embedding_device, "reranker_device": settings.reranker_device,
            "embedding_use_fp16": settings.embedding_use_fp16, "reranker_use_fp16": settings.reranker_use_fp16,
            "reranker_normalize": True, "seed": 0, "torch_threads": torch.get_num_threads(),
            "python": platform.python_version(), "platform": platform.platform(),
            "versions": {name: importlib.metadata.version(name) for name in ["torch", "transformers", "FlagEmbedding", "rank-bm25", "jieba", "pgvector"]},
            "bm25": {"k1": 1.5, "b": 0.75, "epsilon": 0.25, "tokenizer": "regex-casefold+jieba-HMM-false"},
            "code_sha256": {path: sha256(path) for path in [
                "app/retrieval/dense.py", "app/retrieval/bm25.py", "app/retrieval/hybrid.py",
                "app/retrieval/reranker.py", "app/retrieval/embedding.py", "app/evaluation/metrics.py",
                "app/evaluation/benchmark.py", "app/config.py",
            ]},
            "limitations": manifest["limitations"],
            "latency_note": "Includes first-call model loading; single-run CPU timings, not a production latency benchmark.",
        }
        args.output.mkdir(parents=True, exist_ok=True)
        (args.output / "results.json").write_text(json.dumps({"metadata": metadata, "summary": summary}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        (args.output / "rankings.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records), encoding="utf-8")
        with (args.output / "results.csv").open("w", newline="", encoding="utf-8") as file:
            writer = csv.DictWriter(file, fieldnames=list(summary[0]))
            writer.writeheader()
            writer.writerows(summary)
        print("Method | Recall@5 | MRR (Top-10) | nDCG@10")
        for row in summary:
            print(f"{row['method']} | {row['recall_at_5']:.4f} | {row['mrr']:.4f} | {row['ndcg_at_10']:.4f}")
    finally:
        if engine is not None:
            engine.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        admin.dispose()


if __name__ == "__main__":
    main()
