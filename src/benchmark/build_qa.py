#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Build final four-option multiple-choice QA JSONL from benchmark candidate files.

Down-samples and formats candidate pools into standardized ``question`` /
``options`` / ``answer`` records for evaluation and training.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
from pathlib import Path
from typing import Any, Dict, List, Sequence, Tuple

_SRC_DIR = Path(__file__).resolve().parents[1]
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from utils.io import read_json, read_jsonl, write_json, write_jsonl  # noqa: E402
from utils.k12_ids import BOOK_CODES, HIGH_SCHOOL_BOOK_CODES, MIDDLE_SCHOOL_BOOK_CODES, PRIMARY_MATH_BOOK_CODES  # noqa: E402


LABELS = ("A", "B", "C", "D")
K_VALUES = (1, 2, 3)
SUBJECT_CN = {
    "math": "数学",
    "physics": "物理",
    "chemistry": "化学",
    "biology": "生物",
}

def clean_text_list(values: Any) -> List[str]:
    if not isinstance(values, list):
        return []
    out: List[str] = []
    seen = set()
    for x in values:
        if not isinstance(x, str):
            continue
        t = " ".join(x.strip().split())
        if not t or t in seen:
            continue
        seen.add(t)
        out.append(t)
    return out


def seed_from_id(qid: str, salt: str = "") -> int:
    h = hashlib.sha256(f"{qid}|{salt}".encode("utf-8")).hexdigest()
    return int(h[:16], 16)


def shuffled_by_id(items: Sequence[str], qid: str, salt: str) -> List[str]:
    arr = list(items)
    rnd = random.Random(seed_from_id(qid, salt))
    rnd.shuffle(arr)
    return arr


def choose_k(
    feasible: List[int],
    global_counts: Dict[int, int],
    seen_count: int,
    qid: str,
    target_ratio: Dict[int, float],
) -> int:
    best_k = feasible[0]
    best_score = None
    for k in feasible:
        target = target_ratio[k] * (seen_count + 1)
        deficit = target - global_counts[k]
        tie = seed_from_id(qid, f"k_tie_{k}") / float(2**64)
        score = (deficit, tie)
        if best_score is None or score > best_score:
            best_score = score
            best_k = k
    return best_k


def combos_for_k(k: int) -> List[Tuple[int, ...]]:
    if k == 1:
        return [(0,), (1,), (2,), (3,)]
    if k == 2:
        return [(0, 1), (0, 2), (0, 3), (1, 2), (1, 3), (2, 3)]
    if k == 3:
        return [(0, 1, 2), (0, 1, 3), (0, 2, 3), (1, 2, 3)]
    raise ValueError(f"unsupported k={k}")


def choose_combo(
    k: int,
    combo_counts: Dict[int, Dict[Tuple[int, ...], int]],
    qid: str,
) -> Tuple[int, ...]:
    combos = combos_for_k(k)
    min_count = min(combo_counts[k][c] for c in combos)
    candidates = [c for c in combos if combo_counts[k][c] == min_count]
    candidates = sorted(candidates, key=lambda c: seed_from_id(qid, f"combo_{k}_{c}"))
    return candidates[0]


def stable_template(prefix: str, sample_id: str, templates: Sequence[str]) -> str:
    digest = hashlib.sha256(f"{prefix}|{sample_id}".encode("utf-8")).hexdigest()
    return templates[int(digest[:8], 16) % len(templates)]


def parse_subject_and_book_code(node_id: str) -> Tuple[str, str]:
    text = str(node_id or "").strip()
    if not text or "_rjb" not in text:
        return ("", "")
    parts = text.split("_")
    if len(parts) < 3:
        return ("", "")
    subject = parts[0]
    book_code = parts[1]
    if book_code not in BOOK_CODES:
        return ("", "")
    return (subject, book_code)


def stage_cn_from_book_code(book_code: str) -> str:
    if book_code in PRIMARY_MATH_BOOK_CODES:
        return "小学"
    if book_code in MIDDLE_SCHOOL_BOOK_CODES:
        return "初中"
    if book_code in HIGH_SCHOOL_BOOK_CODES:
        return "高中"
    return ""


def subject_cn_from_node_id(node_id: str) -> str:
    subject, _ = parse_subject_and_book_code(node_id)
    return SUBJECT_CN.get(subject, "")


def stage_subject_cn_from_node_id(node_id: str) -> str:
    subject, book_code = parse_subject_and_book_code(node_id)
    return f"{stage_cn_from_book_code(book_code)}{SUBJECT_CN.get(subject, '')}".strip()


def load_node_name_map(subject_kg_dir: Path) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for path in sorted(subject_kg_dir.glob("*.json")):
        payload = read_json(path)
        if not isinstance(payload, dict):
            continue
        for item in payload.get("nodes", []):
            if not isinstance(item, dict):
                continue
            node_id = str(item.get("id", "")).strip()
            name = " ".join(str(item.get("name", "")).strip().split())
            if node_id and name:
                out[node_id] = name
    return out


def format_task5_subtask2_option(node_id: str, name_map: Dict[str, str]) -> str:
    name = name_map.get(node_id, "")
    prefix = stage_subject_cn_from_node_id(node_id)
    return f"{prefix}{name}".strip()


def render_question(row: Dict[str, Any]) -> str:
    sample_id = str(row.get("id", "")).strip()
    task = str(row.get("task", "")).strip()
    subtask = str(row.get("subtask", "")).strip()
    query_id = str(row.get("query_id", "")).strip()
    query_text = str(row.get("query_text", "")).strip()
    meta = row.get("meta", {}) if isinstance(row.get("meta"), dict) else {}
    subject_cn = subject_cn_from_node_id(query_id)
    subject_with_in = f"{subject_cn}中的" if subject_cn else ""
    stage_subject_cn = stage_subject_cn_from_node_id(query_id)

    if task == "task1" and subtask == "subtask1":
        answer_type = str(meta.get("query_answer_type", "")).strip()
        if answer_type == "Skill":
            templates = [
                "【X】这道题主要考察了什么核心方法？",
                "要解决【X】这道题，主要需要用到哪些知识？",
            ]
            return stable_template("task1_subtask1_skill", sample_id, templates).replace("【X】", f"【{query_text}】")
        templates = [
            "【X】这道题主要考察了什么核心概念？",
            "要解决【X】这道题，主要需要用到哪些知识？",
        ]
        return stable_template("task1_subtask1_concept", sample_id, templates).replace("【X】", f"【{query_text}】")

    if task == "task1" and subtask == "subtask2":
        templates = [
            "以下哪些题目主要考察了{学科}中的【X】这个知识点？",
            "围绕{学科}中的【X】这一知识点，教材里有哪些例题？",
        ]
        template = stable_template("task1_subtask2", sample_id, templates)
        return template.replace("{学科}", subject_cn).replace("【X】", f"【{query_text}】")

    if task == "task2" and subtask == "subtask1":
        templates = [
            "要掌握{学科}中的【X】，应先具备哪些前置知识？",
            "在学习{学科}中的【X】之前，需要学习哪些知识？",
        ]
        template = stable_template("task2_subtask1", sample_id, templates)
        return template.replace("{学科}", subject_cn).replace("【X】", f"【{query_text}】")

    if task == "task2" and subtask == "subtask2":
        templates = [
            "在学习了{学科}中的【X】之后，下一步最适合学习什么知识？",
            "以下哪些知识是{学科}中的【X】的最直接后置知识？",
            "掌握{学科}中的【X】后，通常会马上继续学习哪些内容？",
        ]
        template = stable_template("task2_subtask2", sample_id, templates)
        return template.replace("{学科}", subject_cn).replace("【X】", f"【{query_text}】")

    if task == "task3":
        return f"以下哪些概念与{subject_cn}中的【{query_text}】直接相关（包括分类关系或紧密关联）？"

    if task == "task4" and subtask == "subtask1":
        templates = [
            "围绕{学科}中的【X】，教材安排了哪些验证实验？",
            "教材中哪些实验可以验证{学科}中的【X】？",
        ]
        template = stable_template("task4_subtask1", sample_id, templates)
        return template.replace("{学科}", subject_cn).replace("【X】", f"【{query_text}】")

    if task == "task4" and subtask == "subtask2":
        templates = [
            "以下哪些概念可由{学科}中的【X】实验验证？",
            "通过{学科}中的【X】实验，可以支持哪些核心概念？",
            "{学科}中的【X】实验，在教材中被用来验证什么原理？",
        ]
        template = stable_template("task4_subtask2", sample_id, templates)
        return template.replace("{学科}", subject_cn).replace("【X】", f"【{query_text}】")

    if task == "task5" and subtask == "subtask1":
        templates = [
            "{学科}中的【X】这一知识点最早出现在教材的哪个章节？",
            "学生第一次学习{学科}中的【X】是在以下哪个章节？",
        ]
        template = stable_template("task5_subtask1", sample_id, templates)
        return template.replace("{学科}", subject_cn).replace("【X】", f"【{query_text}】")

    if task == "task5" and subtask == "subtask2":
        return f"以下哪些章节的知识是{stage_subject_cn}【{query_text}】的基础？"

    raise ValueError(f"unsupported row type: task={task} subtask={subtask} id={sample_id}")


def formatted_options(row: Dict[str, Any], name_map: Dict[str, str]) -> Tuple[List[str], List[str]]:
    task = str(row.get("task", "")).strip()
    subtask = str(row.get("subtask", "")).strip()
    if task == "task5" and subtask == "subtask2":
        answers = [format_task5_subtask2_option(node_id, name_map) for node_id in row.get("answer_ids", [])]
        candidates = [format_task5_subtask2_option(node_id, name_map) for node_id in row.get("candidate_ids", [])]
        return (clean_text_list(answers), clean_text_list(candidates))
    return (
        clean_text_list(row.get("answer_names", [])),
        clean_text_list(row.get("candidate_names", [])),
    )


def convert_file(input_path: Path, output_path: Path, name_map: Dict[str, str]) -> Dict[str, Any]:
    rows = read_jsonl(input_path)
    out_rows: List[Dict[str, Any]] = []

    k_counts = {1: 0, 2: 0, 3: 0}
    combo_counts: Dict[int, Dict[Tuple[int, ...], int]] = {
        1: {c: 0 for c in combos_for_k(1)},
        2: {c: 0 for c in combos_for_k(2)},
        3: {c: 0 for c in combos_for_k(3)},
    }
    skipped = 0
    desired_k_counts = {1: 0, 2: 0, 3: 0}
    downgraded_rows = 0

    for row in rows:
        qid = str(row.get("id", "")).strip()
        question = render_question(row)
        answers, candidates = formatted_options(row, name_map)
        candidates = [x for x in candidates if x not in set(answers)]

        total_unique = len(answers) + len(candidates)
        if total_unique < 4 or not answers:
            skipped += 1
            continue

        desired_k = choose_k(
            list(K_VALUES),
            desired_k_counts,
            len(out_rows),
            qid,
            {1: 1 / 3, 2: 1 / 3, 3: 1 / 3},
        )
        desired_k_counts[desired_k] += 1

        actual_k = min(desired_k, len(answers))
        if actual_k != desired_k:
            downgraded_rows += 1

        selected_answers = shuffled_by_id(answers, qid, "answers")[:actual_k]
        selected_wrong = candidates[: (4 - actual_k)]
        if len(selected_answers) + len(selected_wrong) < 4:
            skipped += 1
            continue

        combo = choose_combo(actual_k, combo_counts, qid)
        options = [""] * 4
        ans_for_slots = shuffled_by_id(selected_answers, qid, "answer_slots")
        for idx, opt_pos in enumerate(combo):
            options[opt_pos] = ans_for_slots[idx]

        wrong_iter = iter(selected_wrong)
        for i in range(4):
            if not options[i]:
                options[i] = next(wrong_iter)

        answer_labels = sorted(LABELS[i] for i in combo)
        out_rows.append(
            {
                "id": qid,
                "question": question,
                "options": {LABELS[i]: options[i] for i in range(4)},
                "answer": answer_labels,
            }
        )

        k_counts[actual_k] += 1
        combo_counts[actual_k][combo] += 1

    write_jsonl(output_path, out_rows)
    written = len(out_rows)
    return {
        "input_file": str(input_path),
        "output_file": str(output_path),
        "input_rows": len(rows),
        "written_rows": written,
        "skipped_rows": skipped,
        "desired_k_counts": desired_k_counts,
        "actual_k_counts": k_counts,
        "downgraded_rows": downgraded_rows,
        "combo_counts": {str(k): {str(c): v for c, v in combo_counts[k].items()} for k in K_VALUES},
    }


def parse_args() -> argparse.Namespace:
    repo_root = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(description="Build final 4-option QA jsonl from full benchmark candidate files.")
    parser.add_argument(
        "--input-dir",
        type=Path,
        default=repo_root / "tmp" / "benchmark_candidates_fullcheck",
        help="Input full benchmark candidate directory.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=repo_root / "data" / "benchmark_qa",
        help="Output QA directory.",
    )
    parser.add_argument(
        "--subject-kg-dir",
        type=Path,
        default=repo_root / "data" / "subject_kg",
        help="Subject KG directory for node name lookup.",
    )
    parser.add_argument("--glob", default="*.jsonl", help="Input file glob pattern.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    files = sorted([p for p in args.input_dir.glob(args.glob) if p.is_file()])
    args.output_dir.mkdir(parents=True, exist_ok=True)
    name_map = load_node_name_map(args.subject_kg_dir)

    reports: List[Dict[str, Any]] = []
    total_in = 0
    total_out = 0
    total_skip = 0
    total_downgraded = 0
    agg_desired_k = {1: 0, 2: 0, 3: 0}
    agg_actual_k = {1: 0, 2: 0, 3: 0}

    for fp in files:
        out_path = args.output_dir / fp.name
        rep = convert_file(fp, out_path, name_map)
        reports.append(rep)
        total_in += rep["input_rows"]
        total_out += rep["written_rows"]
        total_skip += rep["skipped_rows"]
        total_downgraded += rep["downgraded_rows"]
        for k in K_VALUES:
            agg_desired_k[k] += rep["desired_k_counts"][k]
            agg_actual_k[k] += rep["actual_k_counts"][k]

    stale_task5 = args.output_dir / "task5.jsonl"
    if stale_task5.exists():
        stale_task5.unlink()

    summary = {
        "input_dir": str(args.input_dir),
        "output_dir": str(args.output_dir),
        "file_count": len(files),
        "total_input_rows": total_in,
        "total_written_rows": total_out,
        "total_skipped_rows": total_skip,
        "total_downgraded_rows": total_downgraded,
        "global_desired_k_counts": agg_desired_k,
        "global_actual_k_counts": agg_actual_k,
        "global_actual_k_ratio_realized": {
            str(k): (agg_actual_k[k] / total_out if total_out else 0.0) for k in K_VALUES
        },
        "files": reports,
    }
    write_json(args.output_dir / "summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
