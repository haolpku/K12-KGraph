#!/usr/bin/env python3
"""Generate a deterministic retrieval evaluation set from graph JSON.

The output is JSONL and can be expanded by human annotation. With small demo
graphs it emits as many examples as possible; with the released primary math
graph it caps at 200 by default.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from utils.bootstrap import ensure_src_on_path

ensure_src_on_path(__file__)

from utils.io import read_json, write_jsonl  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--graph", default="data/retrieval/graph_enriched.json")
    parser.add_argument("--output", default="eval/retrieval/retrieval_eval_200.jsonl")
    parser.add_argument("--limit", type=int, default=200)
    parser.add_argument("--subject", default="数学")
    parser.add_argument("--stage", default="小学")
    parser.add_argument("--grade")
    parser.add_argument("--semester")
    parser.add_argument("--edition")
    return parser.parse_args()


def build_examples(
    graph: Dict[str, Any],
    limit: int,
    filters: Dict[str, Any] | None = None,
) -> List[Dict[str, Any]]:
    scope = {
        key: value
        for key, value in (filters or {"subject": "数学", "stage": "小学"}).items()
        if value not in (None, "")
    }
    nodes = {str(node.get("id", "")): node for node in graph.get("nodes", [])}
    concepts = [node for node in graph.get("nodes", []) if node.get("label") == "Concept"]
    exercises = [node for node in graph.get("nodes", []) if node.get("label") == "Exercise"]
    buckets: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for node in concepts:
        node_scope = _node_scope(node, scope)
        buckets["concept_detail"].append(
            _row("concept_detail", f"请解释【{node.get('name')}】这个知识点", [node["id"]], node_scope)
        )
        buckets["location"].append(
            _row("location", f"【{node.get('name')}】在哪一册哪一章出现？", [node["id"]], node_scope)
        )
    for edge in graph.get("edges", []):
        source, target, rel = str(edge.get("source", "")), str(edge.get("target", "")), str(edge.get("type", ""))
        if rel == "prerequisites_for" and source in nodes and target in nodes:
            buckets["prerequisites"].append(
                _row(
                    "prerequisites",
                    f"学习【{nodes[target].get('name')}】之前需要哪些前置知识？",
                    [source],
                    _node_scope(nodes[target], scope),
                )
            )
            buckets["successors"].append(
                _row(
                    "successors",
                    f"掌握【{nodes[source].get('name')}】后可以继续学习什么？",
                    [target],
                    _node_scope(nodes[source], scope),
                )
            )
        if rel in {"tests_concept", "tests_skill"} and source in nodes and target in nodes:
            buckets["exercises_for"].append(
                _row(
                    "exercises_for",
                    f"哪些练习题考察【{nodes[target].get('name')}】？",
                    [source],
                    _node_scope(nodes[target], scope),
                )
            )
    for node in exercises:
        buckets["similar_exercises"].append(
            _row(
                "similar_exercises",
                f"找几道和【{node.get('name')}】类似的题",
                [node["id"]],
                _node_scope(node, scope, exercise=True),
            )
        )
    merged_buckets = {
        intent: _spread_by_book(_merge_rows(rows))
        for intent, rows in buckets.items()
    }
    intent_order = (
        "concept_detail",
        "location",
        "prerequisites",
        "successors",
        "exercises_for",
        "similar_exercises",
    )
    selected: List[Dict[str, Any]] = []
    index = 0
    while len(selected) < limit:
        added = False
        for intent in intent_order:
            rows = merged_buckets.get(intent, [])
            if index < len(rows):
                selected.append(rows[index])
                added = True
                if len(selected) >= limit:
                    break
        if not added:
            break
        index += 1
    return selected


def _merge_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    merged: Dict[tuple[str, str], Dict[str, Any]] = {}
    for row in rows:
        scope_key = json.dumps(
            row.get("filters", {}),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        key = (row["query_id"], scope_key)
        existing = merged.get(key)
        if existing is None:
            merged[key] = row
            continue
        existing["expected_ids"] = list(
            dict.fromkeys(existing["expected_ids"] + row["expected_ids"])
        )

    counts: Dict[str, int] = defaultdict(int)
    for query_id, _scope_key in merged:
        counts[query_id] += 1
    output: List[Dict[str, Any]] = []
    for (query_id, scope_key), row in merged.items():
        if counts[query_id] == 1:
            output.append(row)
            continue
        scoped_row = dict(row)
        suffix = hashlib.sha256(scope_key.encode("utf-8")).hexdigest()[:8]
        scoped_row["query_id"] = f"{query_id}_{suffix}"
        output.append(scoped_row)
    return output


def _spread_by_book(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Round-robin rows by book so a capped evaluation spans the curriculum."""

    by_book: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_book[str(row.get("filters", {}).get("book_id", ""))].append(row)
    out: List[Dict[str, Any]] = []
    index = 0
    while len(out) < len(rows):
        added = False
        for book_rows in by_book.values():
            if index < len(book_rows):
                out.append(book_rows[index])
                added = True
        if not added:
            break
        index += 1
    return out


def _node_scope(
    node: Dict[str, Any],
    defaults: Dict[str, Any],
    *,
    exercise: bool = False,
) -> Dict[str, Any]:
    props = node.get("properties", {}) if isinstance(node.get("properties"), dict) else {}
    out = dict(defaults)
    for key in ("subject", "stage", "grade", "semester", "edition", "book_id", "section_id"):
        value = props.get(key)
        if value not in (None, ""):
            out[key] = value
    if exercise:
        if props.get("type") not in (None, ""):
            out["exercise_type"] = props["type"]
        if props.get("difficulty") not in (None, ""):
            out["difficulty"] = props["difficulty"]
    return out


def _row(
    intent: str,
    question: str,
    relevant_ids: List[str],
    filters: Dict[str, Any],
) -> Dict[str, Any]:
    digest = hashlib.sha256(f"{intent}:{question}".encode()).hexdigest()[:16]
    return {
        "query_id": f"{intent}_{digest}",
        "query": question,
        "user_type": "student",
        "filters": dict(filters),
        "intent": intent,
        "expected_ids": relevant_ids,
        "expected_evidence_types": _evidence_types(intent, relevant_ids),
        "notes": "synthetic_seed_not_sme_reviewed",
    }


def _evidence_types(intent: str, relevant_ids: List[str]) -> List[str]:
    if not relevant_ids:
        return []
    if intent in {"prerequisites", "successors"}:
        return ["path", "textbook"]
    if intent == "location":
        return ["textbook"]
    return ["node", "textbook"]


def main() -> None:
    args = parse_args()
    graph = read_json(args.graph)
    rows = build_examples(
        graph,
        args.limit,
        {
            "subject": args.subject,
            "stage": args.stage,
            "grade": args.grade,
            "semester": args.semester,
            "edition": args.edition,
        },
    )
    write_jsonl(args.output, rows)
    print(f"wrote {len(rows)} examples to {args.output}")


if __name__ == "__main__":
    main()
