from eval.retrieval.metrics import (
    ndcg_at_k,
    percentile,
    recall_at_k,
    reciprocal_rank,
    summarize,
    summarize_by_intent,
)


def test_recall_at_5_counts_expected_ids_in_top_5():
    assert recall_at_k(["a", "b"], ["x", "a", "b", "c", "d"], 5) == 1.0


def test_recall_at_5_penalizes_unexpected_results_for_no_result_query():
    assert recall_at_k([], ["a"], 5) == 0.0


def test_reciprocal_rank_uses_first_relevant_hit():
    assert reciprocal_rank(["target"], ["x", "target", "other"]) == 0.5


def test_ndcg_at_10_is_one_when_all_expected_ids_are_ranked_ideally():
    assert ndcg_at_k(["a", "b"], ["a", "b", "x"], 10) == 1.0


def test_summarize_reports_latency_and_failure_rate():
    gold = [
        {"query_id": "q1", "expected_ids": ["a"]},
        {"query_id": "q2", "expected_ids": ["b"]},
    ]
    results = [
        {"query_id": "q1", "retrieved_ids": ["a"], "latency_ms": 12.5, "error": None},
        {"query_id": "q2", "retrieved_ids": [], "latency_ms": None, "error": "timeout"},
    ]
    summary = summarize(gold, results)
    assert summary["avg_latency_ms"] == 12.5
    assert summary["p50_latency_ms"] == 12.5
    assert summary["p95_latency_ms"] == 12.5
    assert summary["failure_rate"] == 0.5


def test_percentile_interpolates_sorted_latency_values():
    assert percentile([30.0, 10.0, 20.0], 0.5) == 20.0
    assert percentile([], 0.95) is None


def test_summarize_by_intent_keeps_component_metrics_separate():
    gold = [
        {"query_id": "q1", "intent": "location", "expected_ids": ["a"]},
        {"query_id": "q2", "intent": "prerequisites", "expected_ids": ["b"]},
    ]
    results = [
        {"query_id": "q1", "retrieved_ids": ["a"], "latency_ms": 1, "error": None},
        {"query_id": "q2", "retrieved_ids": [], "latency_ms": 2, "error": None},
    ]

    grouped = summarize_by_intent(gold, results)

    assert grouped["location"]["recall_at_5"] == 1.0
    assert grouped["prerequisites"]["recall_at_5"] == 0.0
