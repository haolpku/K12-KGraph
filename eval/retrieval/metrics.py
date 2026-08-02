"""Compute retrieval metrics for cypher/fulltext/vector/hybrid result files."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from statistics import mean


def load_jsonl(path: Path) -> list[dict]:
    rows = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def recall_at_k(expected: list[str], retrieved: list[str], k: int) -> float:
    if not expected:
        return 1.0 if not retrieved[:k] else 0.0
    return len(set(expected) & set(retrieved[:k])) / len(set(expected))


def reciprocal_rank(expected: list[str], retrieved: list[str]) -> float:
    expected_set = set(expected)
    if not expected_set:
        return 1.0 if not retrieved else 0.0
    for rank, item_id in enumerate(retrieved, start=1):
        if item_id in expected_set:
            return 1.0 / rank
    return 0.0


def ndcg_at_k(expected: list[str], retrieved: list[str], k: int) -> float:
    expected_set = set(expected)
    if not expected_set:
        return 1.0 if not retrieved[:k] else 0.0
    dcg = 0.0
    for rank, item_id in enumerate(retrieved[:k], start=1):
        if item_id in expected_set:
            dcg += 1.0 / math.log2(rank + 1)
    ideal_hits = min(len(expected_set), k)
    idcg = sum(1.0 / math.log2(rank + 1) for rank in range(1, ideal_hits + 1))
    return dcg / idcg if idcg else 0.0


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def summarize(gold_rows: list[dict], result_rows: list[dict]) -> dict:
    gold_by_id = {row["query_id"]: row for row in gold_rows}
    result_by_id = {row["query_id"]: row for row in result_rows}
    recalls = []
    reciprocal_ranks = []
    ndcgs = []
    latencies = []
    failures = 0

    for query_id, gold in gold_by_id.items():
        result = result_by_id.get(query_id)
        if result is None or result.get("error"):
            failures += 1
            retrieved = []
        else:
            retrieved = result.get("retrieved_ids", [])
            if result.get("latency_ms") is not None:
                latencies.append(float(result["latency_ms"]))

        expected = gold["expected_ids"]
        recalls.append(recall_at_k(expected, retrieved, 5))
        reciprocal_ranks.append(reciprocal_rank(expected, retrieved))
        ndcgs.append(ndcg_at_k(expected, retrieved, 10))

    count = len(gold_by_id)
    return {
        "queries": count,
        "recall_at_5": mean(recalls) if recalls else 0.0,
        "mrr": mean(reciprocal_ranks) if reciprocal_ranks else 0.0,
        "ndcg_at_10": mean(ndcgs) if ndcgs else 0.0,
        "avg_latency_ms": mean(latencies) if latencies else None,
        "p50_latency_ms": percentile(latencies, 0.50),
        "p95_latency_ms": percentile(latencies, 0.95),
        "failure_rate": failures / count if count else 0.0,
        "missing_results": count - len(set(gold_by_id) & set(result_by_id)),
    }


def summarize_by_intent(gold_rows: list[dict], result_rows: list[dict]) -> dict[str, dict]:
    """Return the same metrics grouped by the gold row's retrieval intent."""
    results_by_id = {row["query_id"]: row for row in result_rows}
    intents = sorted({str(row.get("intent", "unknown")) for row in gold_rows})
    return {
        intent: summarize(
            [row for row in gold_rows if str(row.get("intent", "unknown")) == intent],
            [
                results_by_id[row["query_id"]]
                for row in gold_rows
                if str(row.get("intent", "unknown")) == intent and row["query_id"] in results_by_id
            ],
        )
        for intent in intents
    }


def parse_result_arg(value: str) -> tuple[str, Path]:
    if "=" not in value:
        raise argparse.ArgumentTypeError("result spec must be name=path")
    name, path = value.split("=", 1)
    if not name:
        raise argparse.ArgumentTypeError("result name cannot be empty")
    return name, Path(path)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--gold", type=Path, required=True)
    parser.add_argument("--results", nargs="+", type=parse_result_arg, required=True)
    args = parser.parse_args()

    gold_rows = load_jsonl(args.gold)
    summaries = {
        name: summarize(gold_rows, load_jsonl(path))
        for name, path in args.results
    }
    print(json.dumps(summaries, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
