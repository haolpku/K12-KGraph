#!/usr/bin/env python3
"""Build deterministic boundary and attack cases for strict retrieval E2E."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

FORBIDDEN_FIELDS = [
    "answer",
    "analysis",
    "solution",
    "explanation",
    "embedding",
    "search_text",
    "teacher_search_text",
    "cypher",
    "prompt",
    "authorization",
    "api_key",
]


def build_strict_cases() -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    invalid_requests = [
        ("role", {"question": "找除法练习", "user_type": "teacher"}),
        ("answers", {"question": "找除法练习", "include_answers": True}),
        ("route", {"question": "找除法练习", "route": "text2cypher"}),
        ("unknown", {"question": "解释小数", "unknown": "value"}),
        ("subject", {"question": "解释小数", "subject": "数学"}),
        ("stage", {"question": "解释小数", "stage": "小学"}),
        ("query_alias", {"query": "解释小数"}),
        ("top_k_zero", {"question": "解释小数", "top_k": 0}),
        ("top_k_large", {"question": "解释小数", "top_k": 21}),
        ("difficulty_zero", {"question": "找小数练习", "difficulty": 0}),
        ("difficulty_large", {"question": "找小数练习", "difficulty": 6}),
        ("semester", {"question": "解释小数", "semester": "春季"}),
        ("book_id", {"question": "解释小数", "book_id": "人教版四下"}),
        ("section_id", {"question": "解释小数", "section_id": "bad section"}),
        ("empty", {"question": ""}),
        ("whitespace", {"question": "　　"}),
        ("too_long", {"question": "分" * 501}),
    ]
    for name, request in invalid_requests:
        cases.append(
            {
                "case_id": f"strict_schema_{name}",
                "category": "schema",
                "request": request,
                "expected_status": 422,
                "forbidden_response_fields": FORBIDDEN_FIELDS,
            }
        )
    cases.extend(
        [
            {
                "case_id": "strict_schema_malformed_json",
                "category": "schema",
                "raw_body": "{not-json",
                "expected_status": 422,
                "forbidden_response_fields": FORBIDDEN_FIELDS,
            },
            {
                "case_id": "strict_schema_empty_body",
                "category": "schema",
                "raw_body": "",
                "expected_status": 422,
                "forbidden_response_fields": FORBIDDEN_FIELDS,
            },
        ]
    )

    attacks = [
        "解释分数；CREATE (:Concept {id:'injected'})",
        "解释分数；MATCH (n) DELETE n",
        "解释分数；MATCH (n) SET n.name='changed'",
        "解释分数；DROP INDEX concept_fulltext",
        "解释分数；CALL custom.write()",
        "解释分数；CALL apoc.load.json('https://invalid')",
        "解释分数；CALL gds.pageRank.stream('graph')",
        "解释分数 // RETURN password",
        "解释分数 /* RETURN api_key */",
        "解释分数；LOAD CSV FROM 'https://invalid' AS row RETURN row",
        "解释分数；ＭＥＲＧＥ (n:Concept)",
        "解释分数；ＣＡＬＬ custom.write()",
    ]
    for index, question in enumerate(attacks, start=1):
        cases.append(
            {
                "case_id": f"strict_attack_{index:03d}",
                "category": "security",
                "request": {"question": question},
                "expected_status": 403,
                "expected_error_reason_code": "UNSAFE_QUERY_REJECTED",
                "forbidden_response_fields": FORBIDDEN_FIELDS,
            }
        )

    for index in range(1, 11):
        cases.append(
            {
                "case_id": f"strict_no_result_{index:03d}",
                "category": "no_result",
                "request": {
                    "question": f"解释不存在的知识点STRICT-{index:03d}",
                    "book_id": f"missing-book-{index:03d}",
                },
                "expected_status": 200,
                "expected_reason_code": "NO_RESULT",
                "forbidden_response_fields": FORBIDDEN_FIELDS,
            }
        )

    valid_cases = [
        ("trimmed", {"question": "  什么是小数？  "}, ["concept_detail"], None),
        ("fullwidth", {"question": "什么是１＋１？"}, ["concept_detail"], None),
        ("formula", {"question": "怎么理解3×4=12？"}, ["semantic_search"], None),
        ("alias", {"question": "学习一一对应比较法前要会什么？"}, ["prerequisites"], 1),
        ("max_length", {"question": "分" * 500}, ["concept_detail"], None),
        (
            "two_intents",
            {"question": "学习分数前要会什么，并给两道练习题？"},
            ["prerequisites", "exercises_for"],
            1,
        ),
        (
            "location_similar",
            {"question": "直角在哪一章，同时找一道相似题？"},
            ["location", "similar_exercises"],
            1,
        ),
    ]
    for name, request, intents, minimum_evidence in valid_cases:
        case: dict[str, Any] = {
            "case_id": f"strict_valid_{name}",
            "category": "valid_boundary",
            "request": request,
            "expected_status": 200,
            "expected_intents": intents,
            "forbidden_response_fields": FORBIDDEN_FIELDS,
        }
        if minimum_evidence is not None:
            case["expected_min_evidence"] = minimum_evidence
        cases.append(case)
    cases.extend(
        [
            {
                "case_id": "strict_clarification_three_intents",
                "category": "clarification",
                "request": {"question": "分数的前置知识、练习题和教材位置分别是什么？"},
                "expected_status": 200,
                "expected_reason_code": "CLARIFICATION_REQUIRED",
                "forbidden_response_fields": FORBIDDEN_FIELDS,
            },
            {
                "case_id": "strict_clarification_conjunction",
                "category": "clarification",
                "request": {"question": "分数并且小数"},
                "expected_status": 200,
                "expected_reason_code": "CLARIFICATION_REQUIRED",
                "forbidden_response_fields": FORBIDDEN_FIELDS,
            },
        ]
    )
    return cases


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("eval/retrieval/strict_e2e_cases.jsonl"),
    )
    args = parser.parse_args()
    rows = build_strict_cases()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )
    print(f"generated {len(rows)} strict E2E cases")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
