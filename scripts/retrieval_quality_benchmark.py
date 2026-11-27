"""Evaluate BM25, BGE-M3 Dense, RRF Hybrid and Hybrid+Reranker.

Input is JSONL: queries {id,text}, corpus {id,text}, qrels {query_id,doc_id,relevance}.
The benchmark never mutates production indexes or the original benchmark evaluator.
"""
import argparse
import json
from pathlib import Path

import numpy as np
import jieba
from rank_bm25 import BM25Okapi

from app.retrieval.embedding import BGEEmbeddingService
from app.retrieval.reranker import RerankerService

def read_jsonl(path):
    with Path(path).open(encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]

def tokens(text):
    return [x.lower() for x in jieba.cut(text) if x.strip()]

def dcg(rels):
    return sum((2 ** rel - 1) / np.log2(i + 2) for i, rel in enumerate(rels))

def metrics(ranked, qrels, k):
    recalls, mrrs, ndcgs = [], [], []
    for qid, docs in ranked.items():
        relevant = {d for d, rel in qrels.get(qid, {}).items() if rel > 0}
        if not relevant:
            continue
        top = docs[:k]
        rels = [qrels.get(qid, {}).get(doc, 0) for doc in top]
        recalls.append(len(set(top) & relevant) / len(relevant))
        first = next((i + 1 for i, rel in enumerate(rels) if rel > 0), None)
        mrrs.append(1 / first if first else 0.0)
        ideal = sorted(qrels[qid].values(), reverse=True)[:k]
        ndcgs.append(dcg(rels) / dcg(ideal) if dcg(ideal) else 0.0)
    return {
        f"Recall@{k}": float(np.mean(recalls)) if recalls else 0.0,
        f"MRR@{k}": float(np.mean(mrrs)) if mrrs else 0.0,
        f"NDCG@{k}": float(np.mean(ndcgs)) if ndcgs else 0.0,
        "evaluated_queries": len(recalls),
    }

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--queries", type=Path, required=True)
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--qrels", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--candidate-k", type=int, default=50)
    parser.add_argument("--top-k", type=int, default=10)
    args = parser.parse_args()

    queries = read_jsonl(args.queries)
    corpus = read_jsonl(args.corpus)
    qrels_rows = read_jsonl(args.qrels)
    if not queries or not corpus or not qrels_rows:
        raise ValueError("Queries, corpus and qrels must all be non-empty")
    query_ids = {x["id"] for x in queries}
    doc_ids = {x["id"] for x in corpus}
    if len(query_ids) != len(queries) or len(doc_ids) != len(corpus):
        raise ValueError("Query and corpus IDs must be unique")
    qrels = {}
    for row in qrels_rows:
        qrels.setdefault(row["query_id"], {})[row["doc_id"]] = int(row["relevance"])
    if any(qid not in query_ids or doc not in doc_ids
           for qid, docs in qrels.items() for doc in docs):
        raise ValueError("Qrels contain unknown query or document IDs")

    texts = [d["text"] for d in corpus]
    bm25 = BM25Okapi([tokens(t) for t in texts])
    embedder = BGEEmbeddingService()
    reranker = RerankerService()
    doc_vecs = np.asarray(embedder.embed_documents(texts), dtype=np.float32)
    if doc_vecs.shape != (len(corpus), 1024) or not np.isfinite(doc_vecs).all():
        raise RuntimeError("BGE-M3 document embeddings have invalid shape or values")

    bm25_ranked, dense_ranked, hybrid_ranked, reranked = {}, {}, {}, {}
    for query in queries:
        qid, text = query["id"], query["text"]
        qvec = np.asarray(embedder.embed_query(text), dtype=np.float32)
        if qvec.shape != (1024,) or not np.isfinite(qvec).all():
            raise RuntimeError("BGE-M3 query embedding has invalid shape or values")
        dense_scores = doc_vecs @ qvec
        sparse_scores = bm25.get_scores(tokens(text))
        dense_order = np.argsort(-dense_scores, kind="stable")[:args.candidate_k]
        bm_order = np.argsort(-sparse_scores, kind="stable")[:args.candidate_k]
        bm25_ranked[qid] = [corpus[i]["id"] for i in bm_order]
        dense_ranked[qid] = [corpus[i]["id"] for i in dense_order]
        fused = {}
        for order in (dense_order, bm_order):
            for rank, index in enumerate(order, 1):
                fused[index] = fused.get(index, 0.0) + 1.0 / (60 + rank)
        hybrid_order = sorted(fused, key=lambda i: (-fused[i], corpus[i]["id"]))
        hybrid_ranked[qid] = [corpus[i]["id"] for i in hybrid_order]
        candidates = [{"id": corpus[i]["id"], "text": corpus[i]["text"]}
                      for i in hybrid_order[:args.candidate_k]]
        scores = reranker.score(text, candidates)
        rerank_order = sorted(zip(candidates, scores, strict=True),
                              key=lambda pair: (-pair[1], pair[0]["id"]))
        reranked[qid] = [item["id"] for item, _ in rerank_order]

    report = {
        "queries": len(queries), "corpus_documents": len(corpus),
        "candidate_k": args.candidate_k, "top_k": args.top_k,
        "methods": {
            "BM25": metrics(bm25_ranked, qrels, args.top_k),
            "BGE-M3 Dense": metrics(dense_ranked, qrels, args.top_k),
            "BGE-M3 Dense+BM25 RRF": metrics(hybrid_ranked, qrels, args.top_k),
            "Hybrid+BAAI Reranker v2 m3": metrics(reranked, qrels, args.top_k),
        },
        "notes": [
            "This is an offline benchmark; it does not mutate production indexes.",
            "Reranker quality is measured on a fixed hybrid candidate pool.",
            "Use a held-out, human-judged qrels file; do not tune on the test split.",
            "Run multiple seeds/bootstrap confidence intervals before claiming small gains."
        ],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(report, ensure_ascii=False, indent=2))

if __name__ == "__main__":
    main()
