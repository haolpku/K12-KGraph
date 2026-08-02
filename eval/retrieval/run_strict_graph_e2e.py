#!/usr/bin/env python3
"""Run strict HTTP graph-retrieval E2E checks against a live read-only API."""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "src"))

from eval.retrieval.generate_strict_e2e_cases import FORBIDDEN_FIELDS, build_strict_cases  # noqa: E402
from eval.retrieval.metrics import percentile, summarize, summarize_by_intent  # noqa: E402
from eval.retrieval.run_research_e2e import (  # noqa: E402
    evaluate_case,
    forbidden_fields,
    healthcheck,
    load_cases,
)

INTENT_MAP = {"concept_detail": "semantic_search"}
ROUTE_MAP = {
    "concept_detail": "hybrid",
    "similar_exercises": "hybrid",
    "location": "cypher_template",
    "prerequisites": "cypher_template",
    "successors": "cypher_template",
    "exercises_for": "cypher_template",
}
BASELINE_RECALL_AT_5 = 0.9986111111111111
BASELINE_NDCG_AT_10 = 0.935402552550085


def request_json(
    url: str,
    *,
    payload: dict[str, Any] | None = None,
    raw_body: str | None = None,
    timeout: float,
) -> tuple[int, dict[str, Any], float]:
    if urlparse(url).scheme not in {"http", "https"}:
        raise ValueError("E2E URL must use http or https")
    body = raw_body.encode("utf-8") if raw_body is not None else json.dumps(payload or {}, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(  # noqa: S310 - URL scheme is allowlisted above
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            status = response.status
            raw_response = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        status = exc.code
        raw_response = exc.read().decode("utf-8")
    elapsed_ms = (time.perf_counter() - started) * 1000
    try:
        response_body = json.loads(raw_response)
    except json.JSONDecodeError:
        response_body = {"unparseable_response": raw_response[:200]}
    return status, response_body, elapsed_ms


def payload_from_gold(row: dict[str, Any]) -> dict[str, Any]:
    payload = {"question": row["query"], "top_k": 10}
    allowed = {
        "grade",
        "semester",
        "edition",
        "book_id",
        "section_id",
        "exercise_type",
        "difficulty",
    }
    payload.update(
        {
            key: value
            for key, value in row.get("filters", {}).items()
            if key in allowed and value is not None
        }
    )
    return payload


def ranked_ids(body: dict[str, Any]) -> list[str]:
    ids: list[str] = []

    def add(value: Any) -> None:
        text = str(value or "")
        if text and text not in ids:
            ids.append(text)

    for key in ("evidence_nodes", "entities"):
        for node in body.get(key) or []:
            if isinstance(node, dict):
                add(node.get("id"))
    for path in body.get("relation_paths") or []:
        if isinstance(path, dict):
            for node in path.get("nodes") or []:
                if isinstance(node, dict):
                    add(node.get("id"))
    for location in body.get("textbook_locations") or []:
        if isinstance(location, dict):
            add(location.get("node_id"))
    return ids


def metric_error(status: int, body: dict[str, Any]) -> str | None:
    """Return only failures that make relevance metrics unusable."""

    if status != 200:
        return f"http_status={status}"
    warnings = body.get("warnings") or []
    if any("backend unavailable" in str(item).lower() for item in warnings):
        return "retrieval_backend_unavailable"
    return None


def response_observation(body: dict[str, Any]) -> dict[str, Any]:
    """Keep ranking and evidence metadata without persisting node properties."""

    warnings = [str(item) for item in body.get("warnings") or []]
    evidence_nodes = [
        {
            "id": str(node.get("id", "")),
            "label": str(node.get("label", "")),
            "score": float(node.get("score", 0.0) or 0.0),
        }
        for node in body.get("evidence_nodes") or []
        if isinstance(node, dict)
    ]
    relation_paths = [
        {
            "start_id": str(path.get("start_id", "")),
            "end_id": str(path.get("end_id", "")),
            "relationships": list(path.get("relationships") or []),
            "path_length": len(path.get("relationships") or []),
            "node_ids": [
                str(node.get("id", ""))
                for node in path.get("nodes") or []
                if isinstance(node, dict)
            ],
        }
        for path in body.get("relation_paths") or []
        if isinstance(path, dict)
    ]
    detail = body.get("detail") if isinstance(body.get("detail"), dict) else {}
    return {
        "request_id": body.get("request_id"),
        "reason_code": body.get("reason_code") or detail.get("reason_code"),
        "intents": list(body.get("intents") or []),
        "route": body.get("route"),
        "entity_ids": [
            str(node.get("id", ""))
            for node in body.get("entities") or []
            if isinstance(node, dict)
        ],
        "evidence_nodes": evidence_nodes,
        "relation_paths": relation_paths,
        "textbook_locations": [
            dict(item)
            for item in body.get("textbook_locations") or []
            if isinstance(item, dict)
        ],
        "warnings": warnings,
        "fallback_used": any("FALLBACK" in warning.upper() for warning in warnings),
        "leaked_fields": sorted(forbidden_fields(body, set(FORBIDDEN_FIELDS))),
    }


def _path_matches_graph(
    path: dict[str, Any],
    *,
    intent: str,
    graph_edges: set[tuple[str, str, str]],
) -> bool:
    relationships = list(path.get("relationships") or [])
    node_ids = [
        str(node.get("id", ""))
        for node in path.get("nodes") or []
        if isinstance(node, dict)
    ]
    if len(node_ids) != len(relationships) + 1:
        return False
    start_id = str(path.get("start_id", ""))
    end_id = str(path.get("end_id", ""))
    if intent == "prerequisites":
        endpoints_match = node_ids[0] == end_id and node_ids[-1] == start_id
    else:
        endpoints_match = node_ids[0] == start_id and node_ids[-1] == end_id
    return endpoints_match and all(
        (source, relationship, target) in graph_edges
        for source, relationship, target in zip(
            node_ids[:-1],
            relationships,
            node_ids[1:],
            strict=True,
        )
    )


def _path_correct(
    row: dict[str, Any],
    body: dict[str, Any],
    graph_edges: set[tuple[str, str, str]],
) -> bool | None:
    if row["intent"] not in {"prerequisites", "successors"}:
        return None
    paths = [
        path for path in body.get("relation_paths") or [] if isinstance(path, dict)
    ]
    return bool(paths) and all(
        _path_matches_graph(path, intent=row["intent"], graph_edges=graph_edges)
        for path in paths
    )


def _exercise_links_correct(
    row: dict[str, Any],
    body: dict[str, Any],
    graph_edges: set[tuple[str, str, str]],
) -> bool | None:
    if row["intent"] != "exercises_for":
        return None
    exercise_ids = {
        str(node.get("id", ""))
        for node in body.get("evidence_nodes") or []
        if isinstance(node, dict) and node.get("label") == "Exercise"
    }
    return bool(exercise_ids) and all(
        any(
            source == exercise_id
            and relationship in {"tests_concept", "tests_skill"}
            for source, relationship, _target in graph_edges
        )
        for exercise_id in exercise_ids
    )


def hard_quality_metrics(
    gold_rows: list[dict[str, Any]],
    results: list[dict[str, Any]],
) -> dict[str, float | int]:
    gold_by_id = {row["query_id"]: row for row in gold_rows}
    gold_results = [row for row in results if row.get("category") == "gold"]
    textbook_results = [
        row
        for row in gold_results
        if "textbook"
        in set(gold_by_id.get(str(row.get("query_id")), {}).get("expected_evidence_types") or [])
    ]
    path_results = [
        row for row in gold_results if row.get("path_correct") is not None
    ]
    security_results = [
        row
        for row in results
        if row.get("category") == "security"
        and int(row.get("expected_status", 200)) != 200
    ]
    return {
        "fixed_e2e_failures": sum(
            bool(row.get("p0") or row.get("p1")) for row in results
        ),
        "textbook_evidence_coverage": (
            sum(bool(row.get("has_textbook_evidence")) for row in textbook_results)
            / len(textbook_results)
            if textbook_results
            else 1.0
        ),
        "path_correctness": (
            sum(bool(row.get("path_correct")) for row in path_results)
            / len(path_results)
            if path_results
            else 1.0
        ),
        "student_leak_rate": (
            sum(bool(row.get("leaked_fields")) for row in results) / len(results)
            if results
            else 0.0
        ),
        "illegal_request_acceptance_rate": (
            sum(int(row.get("status", 0)) == 200 for row in security_results)
            / len(security_results)
            if security_results
            else 0.0
        ),
        "fallback_rate": (
            sum(bool(row.get("fallback_used")) for row in gold_results)
            / len(gold_results)
            if gold_results
            else 0.0
        ),
    }


def evaluate_gold_case(
    row: dict[str, Any],
    status: int,
    body: dict[str, Any],
    *,
    graph_edges: set[tuple[str, str, str]] | None = None,
) -> tuple[list[str], list[str]]:
    p0: list[str] = []
    p1: list[str] = []
    if status != 200:
        p1.append(f"status={status}, expected=200")
        return p0, p1
    try:
        uuid.UUID(str(body.get("request_id", "")))
    except ValueError:
        p1.append("request_id_is_not_uuid")
    expected_intent = INTENT_MAP.get(row["intent"], row["intent"])
    if body.get("intents") != [expected_intent]:
        p1.append(f"intents={body.get('intents')!r}, expected={[expected_intent]!r}")
    expected_route = ROUTE_MAP[row["intent"]]
    if body.get("route") != expected_route:
        p1.append(f"route={body.get('route')!r}, expected={expected_route!r}")
    if body.get("reason_code") != "OK":
        p1.append(f"reason_code={body.get('reason_code')!r}, expected='OK'")
    if body.get("needs_clarification") is not False:
        p1.append("unexpected_clarification")
    if body.get("warnings"):
        p1.append(f"warnings={body.get('warnings')!r}")
    leaked = forbidden_fields(body, set(FORBIDDEN_FIELDS))
    if leaked:
        p0.append(f"forbidden_fields={sorted(leaked)!r}")

    expected_ids = list(dict.fromkeys(row.get("expected_ids") or []))
    retrieved = ranked_ids(body)
    missing = [item for item in expected_ids if item not in retrieved[:5]]
    if missing:
        p1.append(f"expected_ids_missing_from_top5={missing!r}; retrieved={retrieved[:10]!r}")

    paths = body.get("relation_paths") or []
    if row["intent"] in {"prerequisites", "successors"}:
        if not paths:
            p1.append("missing_relation_paths")
        for path in paths:
            relationships = path.get("relationships") if isinstance(path, dict) else None
            if not isinstance(relationships, list) or not 1 <= len(relationships) <= 3:
                p1.append(f"invalid_path_length={relationships!r}")
            elif set(relationships) != {"prerequisites_for"}:
                p1.append(f"invalid_path_relationships={relationships!r}")
        if graph_edges is not None and not _path_correct(row, body, graph_edges):
            p1.append("invalid_path_direction_or_endpoints")
    if row["intent"] == "location" or "textbook" in set(
        row.get("expected_evidence_types") or []
    ):
        locations = [
            item
            for item in body.get("textbook_locations") or []
            if isinstance(item, dict)
        ]
        expected_book_id = (row.get("filters") or {}).get("book_id")
        matching_locations = [
            item
            for item in locations
            if expected_book_id is None or item.get("book_id") == expected_book_id
        ]
        if not matching_locations:
            p1.append(f"missing_textbook_location_for={expected_ids!r}")
        elif row["intent"] == "location" and not any(
            item.get("chapter_id") or item.get("section_id")
            for item in matching_locations
        ):
            p1.append(f"textbook_location_missing_chapter={expected_ids!r}")
    if row["intent"] in {"exercises_for", "similar_exercises"}:
        labels = {
            str(item.get("label"))
            for item in body.get("evidence_nodes") or []
            if isinstance(item, dict)
        }
        if "Exercise" not in labels:
            p1.append(f"exercise_evidence_missing; labels={sorted(labels)!r}")
    if graph_edges is not None and _exercise_links_correct(row, body, graph_edges) is False:
        p1.append("exercise_evidence_missing_tests_relationship")
    return p0, p1


def run_gold(
    row: dict[str, Any],
    *,
    url: str,
    timeout: float,
    graph_edges: set[tuple[str, str, str]],
) -> dict[str, Any]:
    status, body, latency_ms = request_json(
        url,
        payload=payload_from_gold(row),
        timeout=timeout,
    )
    p0, p1 = evaluate_gold_case(row, status, body, graph_edges=graph_edges)
    observation = response_observation(body)
    return {
        "case_id": row["query_id"],
        "query_id": row["query_id"],
        "category": "gold",
        "intent": row["intent"],
        **observation,
        "status": status,
        "retrieved_ids": ranked_ids(body),
        "has_textbook_evidence": bool(observation["textbook_locations"]),
        "path_correct": _path_correct(row, body, graph_edges),
        "exercise_links_correct": _exercise_links_correct(row, body, graph_edges),
        "latency_ms": round(latency_ms, 3),
        "p0": p0,
        "p1": p1,
        "error": metric_error(status, body),
        "assertion_error": "; ".join(p0 + p1) or None,
    }


def run_contract(
    case: dict[str, Any],
    *,
    url: str,
    timeout: float,
) -> dict[str, Any]:
    status, body, latency_ms = request_json(
        url,
        payload=case.get("request") or ({"question": case["question"]} if case.get("question") else None),
        raw_body=case.get("raw_body"),
        timeout=timeout,
    )
    failures = evaluate_case(case, status, body)
    leaked = forbidden_fields(body, set(case.get("forbidden_response_fields", [])))
    p0 = [failure for failure in failures if failure.startswith("forbidden_fields=")]
    p1 = [failure for failure in failures if failure not in p0]
    if leaked and not p0:
        p0.append(f"forbidden_fields={sorted(leaked)!r}")
    observation = response_observation(body)
    return {
        "case_id": case["case_id"],
        "category": case["category"],
        "expected_status": case["expected_status"],
        "status": status,
        **observation,
        "latency_ms": round(latency_ms, 3),
        "p0": p0,
        "p1": p1,
        "error": "; ".join(p0 + p1) or None,
    }


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:18000/v1/retrieval/search")
    parser.add_argument("--gold", type=Path, default=Path("eval/retrieval/primary_math_eval_240.jsonl"))
    parser.add_argument("--contracts", type=Path, default=Path("eval/retrieval/research_e2e_cases.jsonl"))
    parser.add_argument("--graph", type=Path, default=Path("data/retrieval/primary_math_graph.json"))
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--warmup", type=int, default=20)
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument("--max-overall-p95-ms", type=float, default=2000.0)
    parser.add_argument("--max-cypher-p95-ms", type=float, default=100.0)
    parser.add_argument("--max-hybrid-p95-ms", type=float, default=500.0)
    args = parser.parse_args()
    if args.concurrency < 1 or args.concurrency > 32:
        raise SystemExit("concurrency must be between 1 and 32")

    health = healthcheck(args.url, timeout=args.timeout)
    gold_rows = load_cases(args.gold)
    contract_rows = load_cases(args.contracts) + build_strict_cases()
    graph = json.loads(args.graph.read_text(encoding="utf-8"))
    graph_edges = {
        (str(edge.get("source", "")), str(edge.get("type", "")), str(edge.get("target", "")))
        for edge in graph.get("edges", [])
        if edge.get("source") and edge.get("type") and edge.get("target")
    }
    warmup_payload = payload_from_gold(gold_rows[0])
    for _ in range(args.warmup):
        request_json(args.url, payload=warmup_payload, timeout=args.timeout)

    tasks: list[tuple[str, dict[str, Any]]] = [
        *(('gold', row) for row in gold_rows),
        *(('contract', row) for row in contract_rows),
    ]

    def execute(task: tuple[str, dict[str, Any]]) -> dict[str, Any]:
        kind, row = task
        if kind == "gold":
            return run_gold(
                row,
                url=args.url,
                timeout=args.timeout,
                graph_edges=graph_edges,
            )
        return run_contract(row, url=args.url, timeout=args.timeout)

    if args.concurrency == 1:
        results = [execute(task) for task in tasks]
    else:
        with ThreadPoolExecutor(max_workers=args.concurrency) as executor:
            results = list(executor.map(execute, tasks))

    gold_results = [row for row in results if row["category"] == "gold"]
    metrics = summarize(gold_rows, gold_results)
    metrics["p99_latency_ms"] = percentile(
        [float(row["latency_ms"]) for row in gold_results],
        0.99,
    )
    metrics["by_intent"] = summarize_by_intent(gold_rows, gold_results)
    hard_metrics = hard_quality_metrics(gold_rows, results)
    p0_count = sum(len(row["p0"]) for row in results)
    p1_count = sum(len(row["p1"]) for row in results)
    latencies_by_route: dict[str, list[float]] = {}
    for row in gold_results:
        latencies_by_route.setdefault(str(row.get("route")), []).append(float(row["latency_ms"]))
    route_p95 = {
        route: percentile(latencies, 0.95)
        for route, latencies in latencies_by_route.items()
    }
    gates: list[str] = []
    minimum_recall = max(0.90, BASELINE_RECALL_AT_5 - 0.01)
    minimum_ndcg = max(0.85, BASELINE_NDCG_AT_10 - 0.01)
    if metrics["recall_at_5"] < minimum_recall:
        gates.append(f"recall_at_5={metrics['recall_at_5']:.6f} < {minimum_recall:.6f}")
    if metrics["ndcg_at_10"] < minimum_ndcg:
        gates.append(f"ndcg_at_10={metrics['ndcg_at_10']:.6f} < {minimum_ndcg:.6f}")
    if metrics["failure_rate"] != 0:
        gates.append(f"failure_rate={metrics['failure_rate']:.6f} != 0")
    if hard_metrics["textbook_evidence_coverage"] != 1.0:
        gates.append(
            "textbook_evidence_coverage="
            f"{hard_metrics['textbook_evidence_coverage']:.6f} != 1"
        )
    if hard_metrics["path_correctness"] != 1.0:
        gates.append(f"path_correctness={hard_metrics['path_correctness']:.6f} != 1")
    if hard_metrics["student_leak_rate"] != 0.0:
        gates.append(f"student_leak_rate={hard_metrics['student_leak_rate']:.6f} != 0")
    if hard_metrics["illegal_request_acceptance_rate"] != 0.0:
        gates.append(
            "illegal_request_acceptance_rate="
            f"{hard_metrics['illegal_request_acceptance_rate']:.6f} != 0"
        )
    overall_p95 = percentile([float(row["latency_ms"]) for row in results], 0.95) or 0.0
    if overall_p95 > args.max_overall_p95_ms:
        gates.append(f"overall_p95_ms={overall_p95:.3f} > {args.max_overall_p95_ms:.3f}")
    cypher_p95 = route_p95.get("cypher_template") or 0.0
    hybrid_p95 = route_p95.get("hybrid") or 0.0
    if cypher_p95 > args.max_cypher_p95_ms:
        gates.append(f"cypher_p95_ms={cypher_p95:.3f} > {args.max_cypher_p95_ms:.3f}")
    if hybrid_p95 > args.max_hybrid_p95_ms:
        gates.append(f"hybrid_p95_ms={hybrid_p95:.3f} > {args.max_hybrid_p95_ms:.3f}")
    p1_count += len(gates)

    summary = {
        "source": "graph_derived_not_sme_reviewed",
        "health": health,
        "configuration": {
            "concurrency": args.concurrency,
            "warmup": args.warmup,
            "gold_cases": len(gold_rows),
            "contract_cases": len(contract_rows),
        },
        "metrics": metrics,
        "hard_quality_metrics": hard_metrics,
        "route_p95_ms": route_p95,
        "overall_p95_ms": overall_p95,
        "p0_count": p0_count,
        "p1_count": p1_count,
        "gate_failures": gates,
        "passed": p0_count == 0 and p1_count == 0,
    }
    write_jsonl(args.output_dir / "results.jsonl", results)
    write_json(args.output_dir / "summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    for row in results:
        if row["p0"] or row["p1"]:
            print(f"{row['case_id']}: P0={row['p0']!r}; P1={row['p1']!r}")
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
