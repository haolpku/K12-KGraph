#!/usr/bin/env python3
"""Generate exercise-style SFT QA JSONL from after-class JSON or a legacy manual file.

Primary input is ``data/afterclass_exercises/*.json`` produced by
``src/kg/build_afterclass_exercises.py``. The legacy ``data/exercise_manual_selected.jsonl``
format remains supported for small experiments.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

_SRC_DIR = Path(__file__).resolve().parents[1]
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from sft_qa.common import load_openai_env, resolve_workspace_root  # noqa: E402
from utils.config import load_config  # noqa: E402
from utils.io import read_json, read_jsonl, write_jsonl  # noqa: E402


DEFAULT_INPUT_NAME = "exercise_manual_selected.jsonl"
DEFAULT_AFTERCLASS_DIRNAME = "afterclass_exercises"
SHORT_ANSWER_TYPES = {"计算题", "应用题"}
REASONING_TYPES = {"证明题", "综合题", "解答题"}
TRIVIAL_ANSWERS = {"对", "错", "正确", "错误", "是", "否"}
LABEL_PREFIX_RE = re.compile(r"^(分析|思路|解题思路|解析|说明|判断依据)\s*[：:]\s*")
STOP_TOKENS = {
    "答案",
    "分析",
    "所以",
    "因此",
    "则",
    "得",
    "可得",
    "说明",
    "因为",
    "最后",
    "其中",
    "分别",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="把课后题(afterclass_exercises)或手工题库转成 exercise SFT QA")
    parser.add_argument("--config", type=str, default=None)
    parser.add_argument(
        "--subject-stage",
        type=str,
        default="manual_exercise",
        help="默认输出到 workspace/sft_qa/<subject_stage>/parts/exercise.jsonl",
    )
    parser.add_argument(
        "--input-jsonl",
        type=str,
        default=None,
        help="默认读取 data/exercise_manual_selected.jsonl",
    )
    parser.add_argument(
        "--afterclass-dir",
        type=str,
        default=None,
        help="读取 enrich 版课后题目录（默认 data/afterclass_exercises/），目录下每本一个 JSON",
    )
    parser.add_argument(
        "--workspace-dir",
        type=str,
        default=None,
        help="覆盖默认 workspace/sft_qa/<subject_stage>",
    )
    parser.add_argument(
        "--output-jsonl",
        type=str,
        default=None,
        help="直接指定输出 JSONL；指定后不再使用 workspace 默认路径",
    )
    parser.add_argument(
        "--mode",
        choices=["answer", "analysis", "hybrid", "dual", "reasoning"],
        default="dual",
        help="answer=只保留直接作答；analysis=只保留思路样本；hybrid=每题只选一种；dual=两种都尽量保留",
    )
    parser.add_argument(
        "--reasoning-format",
        choices=["analysis_only", "answer_then_analysis", "analysis_then_answer"],
        default="answer_then_analysis",
        help="只在 mode=reasoning 时生效",
    )
    parser.add_argument(
        "--min-analysis-score",
        type=int,
        default=5,
        help="只在 mode=reasoning 时生效，用于筛选更好的 analysis",
    )
    parser.add_argument(
        "--allowed-types",
        type=str,
        default="",
        help="可选，逗号分隔，只保留这些题型，例如 应用题,计算题,证明题,综合题,解答题",
    )
    return parser.parse_args()


def resolve_input_path(config: Any, input_jsonl: Optional[str]) -> Path:
    if input_jsonl:
        return Path(input_jsonl).resolve()
    return (config.output_dir / DEFAULT_INPUT_NAME).resolve()


def resolve_afterclass_dir(config: Any, afterclass_dir: Optional[str]) -> Path:
    if afterclass_dir:
        return Path(afterclass_dir).resolve()
    return (config.output_dir / DEFAULT_AFTERCLASS_DIRNAME).resolve()


def _map_afterclass_item_to_legacy(item: Dict[str, Any], *, fallback_subject: str = "") -> Optional[Dict[str, Any]]:
    stem = normalize_text(item.get("stem", ""))
    if not stem:
        return None
    answer = normalize_text(item.get("answer", ""))
    analysis = normalize_text(item.get("analysis", ""))
    qtype = normalize_text(item.get("type", ""))
    subject = normalize_text(item.get("meta", {}).get("subject", "")) if isinstance(item.get("meta"), dict) else ""
    if not subject:
        subject = fallback_subject

    # Keep fields in the legacy schema that build_records() expects.
    return {
        "id": str(item.get("id", "")).strip() or "",
        "subject": subject,
        "properties": {
            "stem": stem,
            "answer": answer,
            "analysis": analysis,
            "type": qtype,
            "difficulty": item.get("difficulty", None),
        },
    }


def load_afterclass_items(afterclass_dir: Path) -> List[Dict[str, Any]]:
    if not afterclass_dir.exists():
        raise FileNotFoundError(f"afterclass_dir not found: {afterclass_dir}")
    items: List[Dict[str, Any]] = []
    for path in sorted(afterclass_dir.glob("*.json")):
        payload = read_json(path)
        if not isinstance(payload, dict):
            continue
        questions = payload.get("questions", [])
        if not isinstance(questions, list):
            continue
        # book-level subject fallback
        fallback_subject = normalize_text(payload.get("subject", ""))
        for q in questions:
            if not isinstance(q, dict):
                continue
            mapped = _map_afterclass_item_to_legacy(q, fallback_subject=fallback_subject)
            if mapped is not None:
                items.append(mapped)
    return items

def resolve_output_path(config: Any, subject_stage: str, workspace_dir: Optional[str], output_jsonl: Optional[str]) -> Path:
    if output_jsonl:
        return Path(output_jsonl).resolve()
    return resolve_workspace_root(config, subject_stage, workspace_dir) / "parts" / "exercise.jsonl"


def normalize_text(text: Any) -> str:
    s = str(text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    s = re.sub(r"[ \t]+\n", "\n", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def compact_text(text: str) -> str:
    return re.sub(r"\s+", "", text)


def clean_analysis_text(text: Any) -> str:
    s = normalize_text(text)
    s = LABEL_PREFIX_RE.sub("", s)
    return s.strip()


def direct_question(stem: str) -> str:
    return f"题目：{stem}\n\n请直接给出答案。"


def reasoning_question(stem: str, question_type: str) -> str:
    if question_type in REASONING_TYPES:
        tail = "请简要说明证明思路或关键推理。"
    elif question_type in SHORT_ANSWER_TYPES:
        tail = "请简要说明解题步骤和关键计算。"
    else:
        tail = "请简要说明作答依据或分析思路。"
    return f"题目：{stem}\n\n{tail}"


def cot_question(stem: str, question_type: str) -> str:
    if question_type == "证明题":
        tail = "请先给出关键推理，再给出结论，尽量写得清楚简洁。"
    elif question_type in SHORT_ANSWER_TYPES:
        tail = "请写出关键分析过程，并给出最终答案。"
    else:
        tail = "请给出简要分析过程，并明确写出最终答案。"
    return f"题目：{stem}\n\n{tail}"


def answer_looks_too_short(answer: str) -> bool:
    compact = compact_text(answer)
    if not compact:
        return True
    if compact in TRIVIAL_ANSWERS:
        return True
    if len(compact) < 8 and not re.search(r"[，。；：,.!?！？]", answer):
        return True
    if re.fullmatch(r"[-+−]?\d+(?:\.\d+)?(?:[%％a-zA-Z℃°/]+)?", compact):
        return True
    return False


def should_emit_answer(question_type: str, answer: str) -> bool:
    if not answer:
        return False
    if question_type == "证明题":
        return False
    if question_type in SHORT_ANSWER_TYPES and answer_looks_too_short(answer):
        return False
    if question_type in REASONING_TYPES and len(compact_text(answer)) < 16:
        return False
    return True


def should_emit_analysis(question_type: str, analysis: str) -> bool:
    if not analysis:
        return False
    if len(compact_text(analysis)) < 20:
        return False
    return True


def extract_compare_tokens(text: str) -> set[str]:
    tokens = re.findall(r"[A-Za-z]+(?:[0-9]+)?|[0-9]+(?:\.[0-9]+)?[%％]?|[\u4e00-\u9fff]{2,}", text)
    cleaned: set[str] = set()
    for token in tokens:
        token = token.strip()
        if not token or token in STOP_TOKENS:
            continue
        cleaned.add(token)
    return cleaned


def answer_analysis_aligned(answer: str, analysis: str) -> bool:
    compact_answer = compact_text(answer)
    compact_analysis = compact_text(analysis)
    if not compact_answer or not compact_analysis:
        return False

    if compact_answer in compact_analysis:
        return True

    answer_tokens = extract_compare_tokens(answer)
    analysis_tokens = extract_compare_tokens(analysis)
    if not answer_tokens or not analysis_tokens:
        return False

    answer_numeric = {t for t in answer_tokens if re.fullmatch(r"[0-9]+(?:\.[0-9]+)?[%％]?", t)}
    if answer_numeric and answer_numeric.issubset(analysis_tokens):
        return True

    overlap = answer_tokens & analysis_tokens
    return len(overlap) >= max(1, min(3, len(answer_tokens) // 2))


def analysis_quality_score(question_type: str, answer: str, analysis: str) -> int:
    score = 0
    compact_analysis = compact_text(analysis)

    if len(compact_analysis) >= 40:
        score += 1
    if len(compact_analysis) >= 80:
        score += 1
    if len(compact_analysis) >= 140:
        score += 1
    if re.search(r"首先|先|再|然后|最后|因此|所以|据此|由此|设|代入|解得|可得|分两种|分情况|步骤|思路|分别", analysis):
        score += 1
    if re.search(r"[（(]1[)）]|①|1\.|2\.|3\.|×|÷|=|→|⇒", analysis):
        score += 1
    if answer_analysis_aligned(answer, analysis):
        score += 1

    return score


def choose_hybrid_mode(question_type: str, answer_ok: bool, analysis_ok: bool) -> Optional[str]:
    if question_type in REASONING_TYPES:
        if analysis_ok:
            return "analysis"
        if answer_ok:
            return "answer"
        return None
    if question_type in SHORT_ANSWER_TYPES:
        if analysis_ok:
            return "analysis"
        if answer_ok:
            return "answer"
        return None
    if answer_ok:
        return "answer"
    if analysis_ok:
        return "analysis"
    return None


def build_record(item: Dict[str, Any], source_mode: str) -> Dict[str, Any]:
    props = item.get("properties", {})
    stem = normalize_text(props.get("stem", ""))
    answer = normalize_text(props.get("answer", ""))
    analysis = clean_analysis_text(props.get("analysis", ""))
    question_type = normalize_text(props.get("type", ""))

    if source_mode == "answer":
        question = direct_question(stem)
        target = answer
    else:
        question = reasoning_question(stem, question_type)
        target = analysis

    return {
        "task": f"exercise_{source_mode}",
        "source_id": str(item.get("id", "")),
        "source_mode": source_mode,
        "subject": str(item.get("subject", "")),
        "question_type": question_type,
        "stem": stem,
        "question": question,
        "answer": target,
    }


def format_reasoning_answer(answer: str, analysis: str, reasoning_format: str) -> str:
    if reasoning_format == "analysis_only":
        return analysis
    if reasoning_format == "analysis_then_answer":
        return f"分析：{analysis}\n\n答案：{answer}"
    return f"答案：{answer}\n\n分析：{analysis}"


def build_reasoning_record(item: Dict[str, Any], reasoning_format: str) -> Dict[str, Any]:
    props = item.get("properties", {})
    stem = normalize_text(props.get("stem", ""))
    answer = normalize_text(props.get("answer", ""))
    analysis = clean_analysis_text(props.get("analysis", ""))
    question_type = normalize_text(props.get("type", ""))

    return {
        "task": "exercise_reasoning",
        "source_id": str(item.get("id", "")),
        "source_mode": "reasoning",
        "subject": str(item.get("subject", "")),
        "question_type": question_type,
        "analysis_score": analysis_quality_score(question_type, answer, analysis),
        "answer_analysis_aligned": answer_analysis_aligned(answer, analysis),
        "stem": stem,
        "question": cot_question(stem, question_type),
        "answer": format_reasoning_answer(answer, analysis, reasoning_format),
    }


def build_records(items: List[Dict[str, Any]], mode: str, *, reasoning_format: str, min_analysis_score: int) -> List[Dict[str, Any]]:
    records: List[Dict[str, Any]] = []

    for item in items:
        props = item.get("properties", {})
        stem = normalize_text(props.get("stem", ""))
        if not stem:
            continue

        question_type = normalize_text(props.get("type", ""))
        answer = normalize_text(props.get("answer", ""))
        analysis = clean_analysis_text(props.get("analysis", ""))

        answer_ok = should_emit_answer(question_type, answer)
        analysis_ok = should_emit_analysis(question_type, analysis)
        analysis_score = analysis_quality_score(question_type, answer, analysis)

        if mode == "answer":
            if answer_ok:
                records.append(build_record(item, "answer"))
            continue

        if mode == "analysis":
            if analysis_ok:
                records.append(build_record(item, "analysis"))
            continue

        if mode == "reasoning":
            if answer and analysis_ok and analysis_score >= min_analysis_score:
                records.append(build_reasoning_record(item, reasoning_format))
            continue

        if mode == "hybrid":
            chosen = choose_hybrid_mode(question_type, answer_ok, analysis_ok)
            if chosen:
                records.append(build_record(item, chosen))
            continue

        if answer_ok:
            records.append(build_record(item, "answer"))
        if analysis_ok:
            records.append(build_record(item, "analysis"))

    return records


def parse_allowed_types(raw: str) -> Optional[set[str]]:
    names = [part.strip() for part in str(raw or "").split(",") if part.strip()]
    if not names:
        return None
    return set(names)


def main() -> None:
    args = parse_args()
    load_openai_env(args.config)
    config = load_config(args.config)

    output_path = resolve_output_path(config, args.subject_stage, args.workspace_dir, args.output_jsonl)

    # Prefer afterclass exercises when provided (or when default dir exists and input_jsonl is not set).
    afterclass_dir = resolve_afterclass_dir(config, args.afterclass_dir)
    if args.afterclass_dir is not None or (args.input_jsonl is None and afterclass_dir.exists()):
        items = load_afterclass_items(afterclass_dir)
        input_desc = str(afterclass_dir)
    else:
        input_path = resolve_input_path(config, args.input_jsonl)
        items = read_jsonl(input_path)
        input_desc = str(input_path)

    allowed_types = parse_allowed_types(args.allowed_types)
    if allowed_types is not None:
        items = [item for item in items if normalize_text(item.get("properties", {}).get("type", "")) in allowed_types]
    records = build_records(
        items,
        args.mode,
        reasoning_format=args.reasoning_format,
        min_analysis_score=args.min_analysis_score,
    )
    write_jsonl(output_path, records)

    answer_count = sum(1 for rec in records if rec.get("source_mode") == "answer")
    analysis_count = sum(1 for rec in records if rec.get("source_mode") == "analysis")
    reasoning_count = sum(1 for rec in records if rec.get("source_mode") == "reasoning")
    print(f"[ok] input: {len(items)} 条 <- {input_desc}")
    print(f"[ok] output: {len(records)} 条 -> {output_path}")
    print(f"[ok] answer records: {answer_count}")
    print(f"[ok] analysis records: {analysis_count}")
    print(f"[ok] reasoning records: {reasoning_count}")


if __name__ == "__main__":
    main()
