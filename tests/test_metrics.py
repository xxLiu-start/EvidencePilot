import math

import pytest

from app.evaluation.benchmark import load_dataset
from app.evaluation.metrics import ndcg_at_k, recall_at_k, reciprocal_rank


def test_metrics_match_hand_calculation_and_do_not_reward_duplicates():
    labels = {"a": 2, "b": 1}
    ranked = ["x", "a", "b"]
    assert recall_at_k(ranked, labels, 2) == .5
    assert reciprocal_rank(ranked, labels) == .5
    expected = (3 / math.log2(3) + 1 / math.log2(4)) / (3 + 1 / math.log2(3))
    assert ndcg_at_k(ranked, labels) == pytest.approx(expected)
    assert ndcg_at_k(["a", "b"], labels) == 1
    assert ndcg_at_k(["a", "a"], labels) < 1
    assert recall_at_k(["a", "a"], labels) == .5
    assert recall_at_k([], {}) == reciprocal_rank([], {}) == ndcg_at_k([], {}) == 0


def test_frozen_dataset_has_twenty_valid_queries_and_saved_judgments():
    manifest, chunks, queries, qrels = load_dataset("data/benchmark/day3")
    assert len(queries) == 20 and len(chunks) >= 20
    assert len(qrels) == 20
    assert manifest["human_reviewed"] is False
