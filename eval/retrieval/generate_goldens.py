"""Generate a deterministic retrieval golden set for primary-school math.

The generated data is intentionally synthetic and reviewable. It is not a
teacher-approved benchmark.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

CONCEPTS = [
    ("whole_number", "整数", "concept_whole_number"),
    ("addition", "加法", "concept_addition"),
    ("subtraction", "减法", "concept_subtraction"),
    ("multiplication", "乘法", "concept_multiplication"),
    ("division", "除法", "concept_division"),
    ("fraction_basic", "分数的初步认识", "concept_fraction_basic"),
    ("decimal_basic", "小数的初步认识", "concept_decimal_basic"),
    ("average", "平均数", "concept_average"),
    ("area_rectangle", "长方形面积", "concept_area_rectangle"),
    ("perimeter", "周长", "concept_perimeter"),
    ("ratio", "比", "concept_ratio"),
    ("percentage", "百分数", "concept_percentage"),
]

INTENTS = [
    ("concept_lookup", "介绍一下{label}", ["{cid}"]),
    ("alias_lookup", "{label}是什么意思", ["{cid}"]),
    ("prerequisite", "学习{label}前要会什么", ["pre_{key}_1", "pre_{key}_2"]),
    ("successor", "{label}后面会学什么", ["next_{key}_1"]),
    ("textbook_location", "{label}在哪一册哪一章", ["section_{key}"]),
    ("exercise_by_concept", "找几道考察{label}的题", ["exercise_{key}_1", "exercise_{key}_2"]),
    ("similar_exercise", "找和{label}应用题意思差不多的题", ["exercise_{key}_similar_1"]),
    ("grade_filter", "三年级上册有关{label}的内容", ["section_{key}", "{cid}"]),
    ("formula_lookup", "{label}有没有公式或计算方法", ["{cid}"]),
    ("no_result", "火星历法里的{label}小学数学知识点", []),
    ("ambiguous", "{label}和生活问题有什么关系", ["{cid}", "skill_{key}"]),
    ("skill_lookup", "{label}需要掌握哪些技能", ["skill_{key}"]),
    ("teacher_analysis", "教师端分析{label}相关易错题", ["exercise_{key}_1", "misconception_{key}"]),
    ("symbol_query", "{label}中的符号怎么理解", ["{cid}"]),
    ("difficulty_filter", "找一题较难的{label}练习", ["exercise_{key}_hard"]),
    ("edition_filter", "人教版小学数学里{label}的位置", ["section_{key}"]),
    ("semester_filter", "下册的{label}相关内容", ["section_{key}"]),
    ("path_evidence", "{label}和哪些知识点有关系", ["{cid}", "related_{key}_1"]),
]


def build_rows() -> list[dict]:
    rows: list[dict] = []
    counter = 1
    for key, label, cid in CONCEPTS:
        for intent, question_template, expected_templates in INTENTS:
            expected_ids = [
                template.format(key=key, label=label, cid=cid)
                for template in expected_templates
            ]
            rows.append(
                {
                    "query_id": f"q{counter:03d}",
                    "query": question_template.format(key=key, label=label, cid=cid),
                    "intent": intent,
                    "user_type": "teacher" if intent == "teacher_analysis" else "student",
                    "filters": {
                        "subject": "数学",
                        "stage": "小学",
                        "grade": 3 if intent in {"grade_filter", "semester_filter"} else None,
                        "semester": "下册" if intent == "semester_filter" else None,
                        "edition": "人教版" if intent == "edition_filter" else None,
                    },
                    "expected_ids": expected_ids,
                    "expected_evidence_types": ["path", "textbook"] if expected_ids else [],
                    "notes": "synthetic_seed_not_sme_reviewed",
                }
            )
            counter += 1
    return rows


def write_jsonl(rows: list[dict], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(__file__).with_name("retrieval_goldens.jsonl"),
    )
    args = parser.parse_args()
    rows = build_rows()
    write_jsonl(rows, args.output)
    print(f"wrote {len(rows)} rows to {args.output}")


if __name__ == "__main__":
    main()
