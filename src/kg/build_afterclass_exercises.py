#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Build enriched after-class exercise JSON from segmented sections and chapter KG.

Selects exercise-like sections, extracts question stems, optionally enriches answers
and concept/skill links with the configured LLM, and writes ``afterclass_exercises``.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

_SRC_DIR = Path(__file__).resolve().parents[1]
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from utils.config import PipelineConfig, load_config
from utils.io import read_json, write_json
from utils.llm_client import LLMClient, create_llm_client

AFTERCLASS_KEYWORDS = (
    "课后练习",
    "练习",
    "习题",
    "复习题",
    "复习参考题",
    "整理和复习",
    "总复习",
    "做一做",
)

SKIP_KEYWORDS = (
    "<table",
    "</table>",
    "下图",
    "图示",
    "图中",
    "画图",
    "下表",
    "填表",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build afterclass exercises from segmented textbook sections and enrich them with LLM outputs."
    )
    parser.add_argument("--config", default=None, help="Pipeline config path")
    parser.add_argument("--filter-prefix", action="append", default=None, help="Only process specific book_prefix values")
    parser.add_argument("--limit", type=int, default=None, help="Process at most N books")
    parser.add_argument("--no-overwrite", action="store_true", help="Reuse existing afterclass-exercise outputs when possible")
    parser.add_argument("--extract-only", action="store_true", help="Only extract raw afterclass questions from sections")
    parser.add_argument("--enrich-only", action="store_true", help="Only enrich previously extracted questions")
    parser.add_argument("--answer-max-tokens", type=int, default=2400, help="Max completion tokens for answer enrichment")
    parser.add_argument("--link-max-tokens", type=int, default=1800, help="Max completion tokens for concept/skill linking")
    parser.add_argument("--failure-report", default=None, help="Optional path to write failure report jsonl")
    return parser.parse_args()


def normalize_text(text: str) -> str:
    return unicodedata.normalize("NFKC", text or "")


def compact_text(text: str) -> str:
    return re.sub(r"\s+", "", normalize_text(text)).strip().lower()


AFTERCLASS_KEYWORDS_COMPACT = tuple(compact_text(keyword) for keyword in AFTERCLASS_KEYWORDS)


def dedup_strings(values: Iterable[str]) -> List[str]:
    seen: set[str] = set()
    out: List[str] = []
    for value in values:
        token = str(value or "").strip()
        if not token:
            continue
        key = compact_text(token)
        if key in seen:
            continue
        seen.add(key)
        out.append(token)
    return out


def safe_read_json(path: Path, default: Any) -> Any:
    try:
        return read_json(path)
    except Exception:
        return default


def create_client(config: PipelineConfig) -> LLMClient:
    llm_cfg = config.llm
    return create_llm_client(
        provider=str(llm_cfg.get("provider", "openai")),
        model=str(llm_cfg.get("model", "gpt-4.1-mini")),
        api_key=str(llm_cfg.get("api_key", "")),
        base_url=str(llm_cfg.get("base_url", "")) or None,
    )


def looks_like_afterclass_section(section: Dict[str, Any]) -> bool:
    tokens = [
        str(section.get("section_title", "")),
        str(section.get("chapter_title", "")),
        str(section.get("file", "")),
    ]
    compact_tokens = [compact_text(token) for token in tokens if token]
    return any(keyword in token for token in compact_tokens for keyword in AFTERCLASS_KEYWORDS_COMPACT)


def load_selected_sections(config: PipelineConfig, book: Dict[str, Any]) -> List[Dict[str, Any]]:
    sections_index_path = config.sections_index_for(book)
    if not sections_index_path.exists():
        raise FileNotFoundError(f"missing sections index: {sections_index_path}")
    sections_index = read_json(sections_index_path)
    sections = sections_index.get("sections", [])
    if not isinstance(sections, list):
        raise ValueError(f"invalid sections index: {sections_index_path}")
    return [item for item in sections if isinstance(item, dict) and looks_like_afterclass_section(item)]


def normalize_markdown(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"\u00a0", " ", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def contains_skip_keywords(text: str) -> List[str]:
    lowered = normalize_text(text).lower()
    hits: List[str] = []
    for keyword in SKIP_KEYWORDS:
        if keyword.lower() in lowered:
            hits.append(keyword)
    return hits


def extract_questions_from_section(source_md: str, body: str, start_global_no: int) -> Tuple[List[Dict[str, Any]], int]:
    lines = body.splitlines()
    question_head_re = re.compile(r"^\s*(\d+)\s*[\.、]\s*(.*)$")
    starts: List[Tuple[int, int, str]] = []
    for idx, line in enumerate(lines):
        match = question_head_re.match(line)
        if match:
            starts.append((idx, int(match.group(1)), match.group(2).strip()))

    source_stem = Path(source_md).stem
    output: List[Dict[str, Any]] = []
    global_no = start_global_no
    for idx, (line_idx, question_no_in_source, first_line_tail) in enumerate(starts):
        end = starts[idx + 1][0] if idx + 1 < len(starts) else len(lines)
        stem_lines = [first_line_tail] if first_line_tail else []
        stem_lines.extend(lines[line_idx + 1 : end])
        stem = normalize_markdown("\n".join(stem_lines))
        if not stem:
            global_no += 1
            continue
        output.append(
            {
                "id": f"{source_stem}_t{global_no}",
                "source_md": source_md,
                "question_no_in_source": question_no_in_source,
                "global_no": global_no,
                "stem": stem,
            }
        )
        global_no += 1
    return output, global_no


def build_afterclass_markdown(config: PipelineConfig, book_prefix: str, sections: Sequence[Dict[str, Any]]) -> Tuple[str, List[Dict[str, Any]]]:
    chunks: List[str] = []
    expanded: List[Dict[str, Any]] = []
    for section in sections:
        file_name = str(section.get("file", "")).strip()
        if not file_name:
            continue
        section_path = config.sections_dir_for(book_prefix) / file_name
        if not section_path.exists():
            continue
        content = normalize_markdown(section_path.read_text(encoding="utf-8", errors="ignore"))
        if not content:
            continue
        chunks.append(f"## 来源: {file_name}\n\n{content}")
        expanded.append(dict(section))
    return "\n\n".join(chunks).strip() + ("\n" if chunks else ""), expanded


def extract_questions(config: PipelineConfig, book: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    book_prefix = str(book["book_prefix"])
    workspace_dir = config.afterclass_exercises_book_dir(book_prefix)
    workspace_dir.mkdir(parents=True, exist_ok=True)

    selected_sections = load_selected_sections(config, book)
    combined_markdown, expanded_sections = build_afterclass_markdown(config, book_prefix, selected_sections)
    (workspace_dir / "afterclass_sections.md").write_text(combined_markdown, encoding="utf-8")
    write_json(workspace_dir / "selected_sections.json", expanded_sections)

    kept: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    global_no = 1
    for section in expanded_sections:
        source_md = str(section.get("file", "")).strip()
        if not source_md:
            continue
        section_path = config.sections_dir_for(book_prefix) / source_md
        body = normalize_markdown(section_path.read_text(encoding="utf-8", errors="ignore"))
        questions, global_no = extract_questions_from_section(source_md, body, global_no)
        for item in questions:
            skip_hits = contains_skip_keywords(item["stem"])
            question_id = f"{book_prefix}_{item['id']}"
            if skip_hits:
                skipped.append(
                    {
                        "id": question_id,
                        "source_md": item["source_md"],
                        "question_no_in_source": item["question_no_in_source"],
                        "keywords": skip_hits,
                        "reason": "multimodal_or_table",
                        "stem_head": item["stem"][:220],
                    }
                )
                continue
            kept.append(
                {
                    "id": question_id,
                    "stem": item["stem"],
                    "meta": {
                        "book_prefix": book_prefix,
                        "source_md": item["source_md"],
                        "question_no_in_source": item["question_no_in_source"],
                        "global_no": item["global_no"],
                    },
                }
            )

    report = {
        "book_prefix": book_prefix,
        "selected_sections": len(expanded_sections),
        "total_extracted": len(kept) + len(skipped),
        "kept_text_only": len(kept),
        "skipped_multimodal": len(skipped),
        "skipped_items": skipped,
    }
    write_json(workspace_dir / "extracted_questions.json", kept)
    write_json(workspace_dir / "extract_report.json", report)
    return kept, report


def normalize_difficulty(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if 1 <= value <= 5 else None
    if isinstance(value, float) and value.is_integer():
        value = int(value)
        return value if 1 <= value <= 5 else None
    if isinstance(value, str) and value.strip().isdigit():
        value = int(value.strip())
        return value if 1 <= value <= 5 else None
    return None


def extract_json_text(text: str) -> Optional[str]:
    if not text:
        return None
    fenced = re.search(r"```(?:json)?\s*(\{.*?\}|\[.*?\])\s*```", text, re.DOTALL)
    if fenced:
        return fenced.group(1)
    start_obj = text.find("{")
    end_obj = text.rfind("}")
    if start_obj != -1 and end_obj != -1 and end_obj > start_obj:
        return text[start_obj : end_obj + 1]
    start_arr = text.find("[")
    end_arr = text.rfind("]")
    if start_arr != -1 and end_arr != -1 and end_arr > start_arr:
        return text[start_arr : end_arr + 1]
    return None


def parse_json_loose(text: str) -> Optional[Any]:
    json_text = extract_json_text(text)
    if not json_text:
        return None
    try:
        return json.loads(json_text)
    except Exception:
        return None


def answer_prompt(stem: str) -> str:
    return (
        "你是一位K12学科老师。\n"
        "请只根据下面这道课后题的题干，输出一个JSON对象，不要输出任何其他文字。\n"
        "JSON字段要求：\n"
        '- answer: 字符串，题目参考答案\n'
        '- difficulty: 数字1~5，题目的难度，数字越大代表难度越高\n'
        '- type: 字符串，题目的题型，取值为选择题、填空题、判断题、简答题、证明题、应用题等\n'
        '- analysis: 字符串，题目的解题过程分析，可为空字符串\n\n'
        "题干如下：\n"
        f"{stem}\n"
    )


def links_prompt(stem: str, concept_names: Sequence[str], skill_names: Sequence[str]) -> str:
    candidates = {
        "concept_names": list(concept_names),
        "skill_names": list(skill_names),
    }
    return (
        "你是一位K12知识图谱标注专家。\n"
        "给定一道课后题和候选知识点/方法名称，请只输出一个JSON对象，不要输出任何其他文字。\n"
        "你只能从候选集合里选择名称。\n"
        "JSON字段要求：\n"
        '- concept_names: 字符串数组，可为空\n'
        '- skill_names: 字符串数组，可为空\n\n'
        "题干如下：\n"
        f"{stem}\n\n"
        "候选集合如下：\n"
        f"{json.dumps(candidates, ensure_ascii=False)}\n"
    )


def normalize_name_choices(values: Any, candidates: Sequence[str]) -> List[str]:
    allowed = {compact_text(item): item for item in candidates if str(item).strip()}
    if not isinstance(values, list):
        return []
    output: List[str] = []
    seen: set[str] = set()
    for value in values:
        token = str(value or "").strip()
        if not token:
            continue
        compact = compact_text(token)
        canonical = allowed.get(compact)
        if not canonical or compact in seen:
            continue
        seen.add(compact)
        output.append(canonical)
    return output


def candidate_names_for_source(config: PipelineConfig, book_prefix: str, source_md: str) -> Tuple[List[str], List[str]]:
    chapter_json_path = config.chapter_kg_dir / book_prefix / f"{Path(source_md).stem}.json"
    data = safe_read_json(chapter_json_path, {})
    nodes = data.get("nodes", []) if isinstance(data, dict) else []
    concept_names: List[str] = []
    skill_names: List[str] = []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        name = str(node.get("name", "")).strip()
        label = str(node.get("label", "")).strip()
        if not name:
            continue
        if label == "Concept":
            concept_names.append(name)
        elif label == "Skill":
            skill_names.append(name)
    return dedup_strings(concept_names), dedup_strings(skill_names)


def enrich_questions(
    config: PipelineConfig,
    client: LLMClient,
    book: Dict[str, Any],
    questions: Sequence[Dict[str, Any]],
    answer_max_tokens: int,
    link_max_tokens: int,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    book_prefix = str(book["book_prefix"])
    llm_cfg = config.llm
    raw_dir = config.afterclass_exercises_book_dir(book_prefix) / "llm_raw"
    raw_dir.mkdir(parents=True, exist_ok=True)

    enriched: List[Dict[str, Any]] = []
    summary = {
        "book_prefix": book_prefix,
        "questions": len(questions),
        "answered": 0,
        "linked": 0,
        "partial": 0,
        "missing_section_kg": 0,
    }

    for item in questions:
        stem = str(item.get("stem", "")).strip()
        meta = item.get("meta", {}) if isinstance(item.get("meta"), dict) else {}
        source_md = str(meta.get("source_md", "")).strip()
        question_id = str(item.get("id", "")).strip()
        concept_candidates, skill_candidates = candidate_names_for_source(config, book_prefix, source_md)
        if not concept_candidates and not skill_candidates:
            summary["missing_section_kg"] += 1

        answer_raw_path = raw_dir / f"{question_id}_answer.txt"
        link_raw_path = raw_dir / f"{question_id}_links.txt"

        answer_response = client.generate(
            answer_prompt(stem),
            temperature=float(llm_cfg.get("temperature", 0.0)),
            max_tokens=answer_max_tokens,
        )
        answer_raw_path.write_text(answer_response, encoding="utf-8")
        answer_obj = parse_json_loose(answer_response)

        answer = ""
        difficulty: Optional[int] = None
        question_type = ""
        analysis = ""
        answer_ok = False
        if isinstance(answer_obj, dict):
            answer = str(answer_obj.get("answer", "") or "").strip()
            difficulty = normalize_difficulty(answer_obj.get("difficulty"))
            question_type = str(answer_obj.get("type", "") or "").strip()
            analysis = str(answer_obj.get("analysis", "") or "").strip()
            answer_ok = bool(answer or difficulty is not None or question_type or analysis)

        link_concepts: List[str] = []
        link_skills: List[str] = []
        link_ok = False
        if concept_candidates or skill_candidates:
            link_response = client.generate(
                links_prompt(stem, concept_candidates, skill_candidates),
                temperature=float(llm_cfg.get("temperature", 0.0)),
                max_tokens=link_max_tokens,
            )
            link_raw_path.write_text(link_response, encoding="utf-8")
            link_obj = parse_json_loose(link_response)
            if isinstance(link_obj, dict):
                link_concepts = normalize_name_choices(link_obj.get("concept_names"), concept_candidates)
                link_skills = normalize_name_choices(link_obj.get("skill_names"), skill_candidates)
                link_ok = bool(link_concepts or link_skills)

        if answer_ok:
            summary["answered"] += 1
        if link_ok:
            summary["linked"] += 1
        if not answer_ok or ((concept_candidates or skill_candidates) and not link_ok):
            summary["partial"] += 1

        enriched.append(
            {
                "id": question_id,
                "stem": stem,
                "answer": answer,
                "analysis": analysis,
                "difficulty": difficulty,
                "type": question_type,
                "meta": {
                    "book_prefix": book_prefix,
                    "source_md": source_md,
                    "question_no_in_source": meta.get("question_no_in_source"),
                    "global_no": meta.get("global_no"),
                },
                "links": {
                    "concept_names": link_concepts,
                    "skill_names": link_skills,
                },
            }
        )

    return enriched, summary


def build_book(
    config: PipelineConfig,
    client: Optional[LLMClient],
    book: Dict[str, Any],
    *,
    overwrite: bool,
    extract_only: bool,
    enrich_only: bool,
    answer_max_tokens: int,
    link_max_tokens: int,
) -> Dict[str, Any]:
    book_prefix = str(book["book_prefix"])
    workspace_dir = config.afterclass_exercises_book_dir(book_prefix)
    output_path = config.afterclass_exercises_output_for(book_prefix)
    extracted_path = workspace_dir / "extracted_questions.json"

    if output_path.exists() and not overwrite and not extract_only:
        return {"book_prefix": book_prefix, "status": "skipped_existing"}

    workspace_dir.mkdir(parents=True, exist_ok=True)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if enrich_only:
        if not extracted_path.exists():
            raise FileNotFoundError(f"missing extracted questions: {extracted_path}")
        questions = safe_read_json(extracted_path, [])
        if not isinstance(questions, list):
            raise ValueError(f"invalid extracted questions: {extracted_path}")
        extract_report = safe_read_json(workspace_dir / "extract_report.json", {})
    else:
        questions, extract_report = extract_questions(config, book)

    if extract_only:
        return {
            "book_prefix": book_prefix,
            "status": "ok",
            "selected_sections": int(extract_report.get("selected_sections", 0)),
            "questions": int(extract_report.get("kept_text_only", 0)),
            "mode": "extract_only",
        }

    if not isinstance(questions, list):
        raise ValueError(f"invalid extracted questions: {extracted_path}")
    if client is None:
        raise ValueError("LLM client is required unless --extract-only is used")
    if not questions:
        payload = {
            "book_prefix": book_prefix,
            "subject": str(book["subject"]),
            "stage": str(book["stage"]),
            "grade": str(book["grade"]),
            "publisher": str(book["publisher"]),
            "source_md": str(config.resolve_book_markdown(book) or ""),
            "questions": [],
        }
        write_json(output_path, payload)
        return {"book_prefix": book_prefix, "status": "ok", "questions": 0, "partial": 0}

    enriched, enrich_summary = enrich_questions(
        config=config,
        client=client,
        book=book,
        questions=questions,
        answer_max_tokens=answer_max_tokens,
        link_max_tokens=link_max_tokens,
    )
    write_json(workspace_dir / "enrich_summary.json", enrich_summary)

    payload = {
        "book_prefix": book_prefix,
        "subject": str(book["subject"]),
        "stage": str(book["stage"]),
        "grade": str(book["grade"]),
        "publisher": str(book["publisher"]),
        "source_md": str(config.resolve_book_markdown(book) or ""),
        "questions": enriched,
    }
    write_json(output_path, payload)
    return {
        "book_prefix": book_prefix,
        "status": "ok",
        "questions": len(enriched),
        "partial": int(enrich_summary.get("partial", 0)),
    }


def process_all(
    config_path: Optional[str],
    filter_prefixes: Optional[Sequence[str]],
    limit: Optional[int],
    overwrite: bool,
    extract_only: bool,
    enrich_only: bool,
    answer_max_tokens: int,
    link_max_tokens: int,
) -> Dict[str, Any]:
    if extract_only and enrich_only:
        raise ValueError("--extract-only and --enrich-only cannot be used together")

    config = load_config(config_path)
    client = None if extract_only else create_client(config)
    wanted = {item.strip() for item in (filter_prefixes or []) if item.strip()}

    summary: Dict[str, Any] = {
        "processed": 0,
        "success": 0,
        "skipped": 0,
        "failed": 0,
        "questions": 0,
        "partial": 0,
        "books": [],
        "failures": [],
    }

    count = 0
    for book in config.load_books(require_source=False):
        book_prefix = str(book["book_prefix"])
        if wanted and book_prefix not in wanted:
            continue

        summary["processed"] += 1
        try:
            result = build_book(
                config=config,
                client=client,
                book=book,
                overwrite=overwrite,
                extract_only=extract_only,
                enrich_only=enrich_only,
                answer_max_tokens=answer_max_tokens,
                link_max_tokens=link_max_tokens,
            )
        except Exception as exc:
            failure = {
                "book_prefix": book_prefix,
                "status": type(exc).__name__,
                "message": str(exc),
            }
            summary["failed"] += 1
            summary["failures"].append(failure)
            print(f"[FAIL] {book_prefix} ({failure['status']})")
            count += 1
            if limit and count >= limit:
                break
            continue

        summary["books"].append(result)
        status = str(result.get("status", "")).strip()
        if status == "skipped_existing":
            summary["skipped"] += 1
            print(f"[SKIP] {book_prefix} -> existing afterclass_exercises")
        else:
            summary["success"] += 1
            summary["questions"] += int(result.get("questions", 0))
            summary["partial"] += int(result.get("partial", 0))
            print(
                f"[OK] {book_prefix} -> {result.get('questions', 0)} questions"
                + (f", {result.get('partial', 0)} partial" if not extract_only else "")
            )

        count += 1
        if limit and count >= limit:
            break

    return summary


def main() -> None:
    args = parse_args()
    summary = process_all(
        config_path=args.config,
        filter_prefixes=args.filter_prefix,
        limit=args.limit,
        overwrite=not args.no_overwrite,
        extract_only=args.extract_only,
        enrich_only=args.enrich_only,
        answer_max_tokens=args.answer_max_tokens,
        link_max_tokens=args.link_max_tokens,
    )

    if args.failure_report:
        report_path = Path(args.failure_report)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        with report_path.open("w", encoding="utf-8") as f:
            for item in summary["failures"]:
                f.write(json.dumps(item, ensure_ascii=False) + "\n")

    print("\n===== SUMMARY =====")
    print(json.dumps({k: v for k, v in summary.items() if k not in {"books", "failures"}}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
