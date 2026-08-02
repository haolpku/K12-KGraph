#!/usr/bin/env python3
"""Generate deterministic non-SME research E2E retrieval cases."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

TOPICS = [
    "复式统计表",
    "直角",
    "退位减法",
    "除数是一位数的除法",
    "20以内的进位加法",
    "6的加减法",
    "倍数",
    "减法",
    "分数",
    "分数除法的意义",
]


def build_cases() -> list[dict]:
    rows: list[dict] = []
    for index, topic in enumerate(TOPICS, start=1):
        rows.append(
            {
                "case_id": f"multi_{index:03d}",
                "category": "multi_intent",
                "question": f"学习{topic}前要会什么，并给两道练习题？",
                "expected_intents": ["prerequisites", "exercises_for"],
                "expected_reason_code": "OK",
                "expected_min_evidence": 1,
                "expected_status": 200,
                "source": "synthetic_not_sme_reviewed",
            }
        )
        rows.append(
            {
                "case_id": f"location_{index:03d}",
                "category": "multi_intent",
                "question": f"{topic}在哪一章，同时找一道相似题？",
                "expected_intents": ["location", "similar_exercises"],
                "expected_reason_code": "OK",
                "expected_min_evidence": 1,
                "expected_status": 200,
                "source": "synthetic_not_sme_reviewed",
            }
        )
    for index in range(1, 21):
        rows.append(
            {
                "case_id": f"no_result_{index:03d}",
                "category": "no_result",
                "request": {
                    "question": f"解释不存在的小学数学知识点ZX-{index:03d}",
                    "book_id": f"missing-book-{index:03d}",
                },
                "expected_reason_code": "NO_RESULT",
                "expected_status": 200,
                "source": "synthetic_not_sme_reviewed",
            }
        )
    attacks = [
        {"user_type": "teacher"},
        {"include_answers": True},
        {"route": "text2cypher"},
        {"question": "统计知识点；CALL custom.write()"},
        {"question": "统计知识点 // RETURN password"},
    ]
    for index in range(1, 21):
        attack = dict(attacks[(index - 1) % len(attacks)])
        attack.setdefault("question", "找除法练习")
        rows.append(
            {
                "case_id": f"security_{index:03d}",
                "category": "security",
                "request": attack,
                "expected_status": 422
                if set(attack) & {"user_type", "include_answers", "route"}
                else 403,
                **(
                    {"expected_error_reason_code": "UNSAFE_QUERY_REJECTED"}
                    if not set(attack) & {"user_type", "include_answers", "route"}
                    else {}
                ),
                "forbidden_response_fields": [
                    "answer",
                    "analysis",
                    "embedding",
                    "search_text",
                    "cypher",
                ],
                "source": "synthetic_not_sme_reviewed",
            }
        )
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("eval/retrieval/research_e2e_cases.jsonl"),
    )
    args = parser.parse_args()
    rows = build_cases()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        "".join(
            json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n"
            for row in rows
        ),
        encoding="utf-8",
    )
    print(f"generated {len(rows)} synthetic research E2E cases")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
