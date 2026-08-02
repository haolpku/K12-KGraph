#!/usr/bin/env python3
"""Run live Neo4j retrieval methods and write comparable result JSONL files."""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "src"))

from eval.retrieval.metrics import summarize, summarize_by_intent  # noqa: E402
from retrieval.models import RetrievalRequest  # noqa: E402
from retrieval.service import RetrievalService  # noqa: E402
from retrieval.settings import RetrievalSettings  # noqa: E402
from retrieval.store import Neo4jStore  # noqa: E402
from utils.io import read_jsonl, write_json, write_jsonl  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gold", default="eval/retrieval/retrieval_goldens.jsonl")
    parser.add_argument("--output-dir", default="eval/retrieval/results")
    parser.add_argument(
        "--methods",
        nargs="+",
        choices=["auto", "cypher", "hybrid", "fulltext", "vector"],
        default=["cypher", "fulltext", "vector", "hybrid"],
    )
    parser.add_argument(
        "--intents",
        nargs="+",
        help="Optionally evaluate only these gold-set intents.",
    )
    return parser.parse_args()


def run_method(rows: List[Dict[str, Any]], service: RetrievalService, route: str) -> List[Dict[str, Any]]:
    results: List[Dict[str, Any]] = []
    for row in rows:
        start = time.perf_counter()
        filters = dict(row.get("filters", {}))
        request = RetrievalRequest(
            question=row["query"],
            user_type=row.get("user_type", "student"),
            route=None if route == "auto" else route,
            top_k=10,
            grade=filters.get("grade"),
            semester=filters.get("semester"),
            edition=filters.get("edition"),
            subject=filters.get("subject", "数学"),
            stage=filters.get("stage", "小学"),
            book_id=filters.get("book_id"),
            section_id=filters.get("section_id"),
            exercise_type=filters.get("exercise_type"),
            difficulty=filters.get("difficulty"),
        )
        error = None
        try:
            response = service.retrieve(request)
            ranked = [node.id for node in response.evidence_nodes]
            if response.warnings:
                error = "; ".join(response.warnings)
        except Exception as exc:  # noqa: BLE001 - result files must record failures
            ranked = []
            error = str(exc)
        results.append(
            {
                "query_id": row["query_id"],
                "retrieved_ids": ranked,
                "latency_ms": round((time.perf_counter() - start) * 1000, 3),
                "error": error,
            }
        )
    return results


def main() -> None:
    args = parse_args()
    settings = RetrievalSettings.from_env()
    rows = read_jsonl(args.gold)
    if args.intents:
        selected = set(args.intents)
        rows = [row for row in rows if row.get("intent") in selected]
        if not rows:
            raise SystemExit("no gold rows matched --intents")
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    summaries: Dict[str, Any] = {}
    with Neo4jStore.connect(settings, readonly=True) as store:
        service = RetrievalService(store, settings)
        for method in args.methods:
            results = run_method(rows, service, method)
            write_jsonl(output_dir / f"{method}.jsonl", results)
            summaries[method] = {
                **summarize(rows, results),
                "by_intent": summarize_by_intent(rows, results),
            }
    write_json(output_dir / "summary.json", summaries)
    print(summaries)


if __name__ == "__main__":
    main()
