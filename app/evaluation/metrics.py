"""Rank metrics: binary Recall/MRR and graded nDCG, with no duplicate gain."""

import math
from app.evaluation.research_metrics import calculate_metrics


def recall_at_k(ranked_ids, qrels, k=5):
    if k <= 0:
        raise ValueError("k must be positive")
    relevant = {str(key) for key, grade in qrels.items() if grade > 0}
    return len(set(map(str, ranked_ids[:k])) & relevant) / len(relevant) if relevant else 0.0


def reciprocal_rank(ranked_ids, qrels):
    relevant = {str(key) for key, grade in qrels.items() if grade > 0}
    return next((1 / rank for rank, key in enumerate(ranked_ids, 1) if str(key) in relevant), 0.0)


def ndcg_at_k(ranked_ids, qrels, k=10):
    if k <= 0 or any(not math.isfinite(g) or g < 0 for g in qrels.values()):
        raise ValueError("Require positive k and finite, non-negative relevance grades")
    seen = set()
    dcg = 0.0
    for rank, key in enumerate(map(str, ranked_ids[:k]), 1):
        if key not in seen:
            dcg += (2 ** qrels.get(key, 0) - 1) / math.log2(rank + 1)
            seen.add(key)
    ideal = sum((2 ** grade - 1) / math.log2(rank + 1)
                for rank, grade in enumerate(sorted(qrels.values(), reverse=True)[:k], 1))
    return dcg / ideal if ideal else 0.0


def evaluate_ranking(ranked_ids, qrels):
    return {"recall_at_5": recall_at_k(ranked_ids, qrels),
            "mrr": reciprocal_rank(ranked_ids, qrels),
            "ndcg_at_10": ndcg_at_k(ranked_ids, qrels)}
