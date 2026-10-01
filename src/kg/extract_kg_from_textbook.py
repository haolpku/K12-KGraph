#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Step 2: extract section-level KG JSON from segmented Markdown sections.

Iterates section files under the segmentation workspace, calls the configured LLM
with a structured prompt, and writes per-section graph JSON under ``chapter_kg``.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

_SRC_DIR = Path(__file__).resolve().parents[1]
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from utils.config import PipelineConfig, load_config
from utils.io import read_json, write_json
from utils.llm_client import LLMClient, create_llm_client

NODE_PREFIX_MAP = {
    "Concept": "cpt",
    "Skill": "skl",
    "Experiment": "exp",
    "Exercise": "exe",
}

NODE_ALLOWED_PROPS = {
    "Concept": {"definition", "importance", "examples", "aliases", "formula", "unit", "pages"},
    "Skill": {"description", "importance", "examples"},
    "Experiment": {"description", "importance", "instrument", "is_student", "conclusion", "process", "phenomena"},
    "Exercise": {"stem", "answer", "difficulty", "type", "analysis"},
}

EDGE_ALLOWED_PROPS = {
    "evidence",
    "original_text",
    "relations",
}

AGGREGATED_EDGE_TYPES = {"tests_concept", "tests_skill"}


class ExtractionValidationError(ValueError):
    def __init__(self, code: str, message: Optional[str] = None) -> None:
        self.code = code
        super().__init__(message or code)


@dataclass
class Section:
    chapter_num: str
    chapter_title: str
    section_num: str
    section_title: str
    file_name: str
    content: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Extract section KG files from segmented textbook sections")
    parser.add_argument("--config", default=None, help="Pipeline config path")
    parser.add_argument("--prompt", default=None, help="Prompt template path")
    parser.add_argument("--filter-prefix", action="append", default=None, help="Only process specific book_prefix values")
    parser.add_argument(
        "--chapters",
        default=None,
        help="Comma-separated section file stems, e.g. u1_ch1,u1_ch1_s1",
    )
    parser.add_argument("--limit", type=int, default=None, help="Process at most N books")
    parser.add_argument("--max-tokens", type=int, default=4000, help="Max completion tokens")
    parser.add_argument(
        "--no-overwrite",
        action="store_true",
        help="Reuse existing extracted section KG files when possible",
    )
    parser.add_argument(
        "--failure-report",
        default=None,
        help="Optional path to write failure report jsonl",
    )
    return parser.parse_args()


def load_prompt(prompt_path: Path) -> str:
    return prompt_path.read_text(encoding="utf-8")


def build_prompt(template: str, section: Section) -> str:
    return template.replace("{{Section_Markdown_Content}}", section.content)


def normalize_node_label(raw_node: Dict[str, Any]) -> str:
    label = raw_node.get("label")
    if isinstance(label, str) and label.strip():
        return label.strip()
    raw_id = str(raw_node.get("id", ""))
    if raw_id.startswith("cpt_"):
        return "Concept"
    if raw_id.startswith("skl_"):
        return "Skill"
    if raw_id.startswith("exp_"):
        return "Experiment"
    if raw_id.startswith("exe_"):
        return "Exercise"
    return "Concept"


def normalize_node_index(raw_node: Dict[str, Any], counters: Dict[str, int]) -> int:
    raw_id = str(raw_node.get("id", "")).strip()
    match = re.search(r"(\d+)$", raw_id)
    label = normalize_node_label(raw_node)
    if match:
        return int(match.group(1))
    counters[label] = counters.get(label, 0) + 1
    return counters[label]


def normalize_ref(ref: Any) -> List[str]:
    if ref is None:
        return []
    if isinstance(ref, str):
        return [ref]
    if isinstance(ref, dict):
        for key in ("id", "name", "stem", "label"):
            value = ref.get(key)
            if isinstance(value, str) and value.strip():
                return [value.strip()]
        return []
    if isinstance(ref, list):
        out: List[str] = []
        for item in ref:
            out.extend(normalize_ref(item))
        return out
    return [str(ref)]


def clean_node_properties(label: str, raw_node: Dict[str, Any], name: str) -> Dict[str, Any]:
    props = raw_node.get("properties")
    merged: Dict[str, Any] = dict(props) if isinstance(props, dict) else {}
    for key, value in raw_node.items():
        if key in {"id", "label", "name", "properties"}:
            continue
        merged[key] = value
    allowed = NODE_ALLOWED_PROPS.get(label, set())
    cleaned = {key: value for key, value in merged.items() if key in allowed}
    if label == "Exercise" and "stem" not in cleaned and name:
        cleaned["stem"] = name
    return cleaned


def clean_edge_properties(raw_edge: Dict[str, Any]) -> Dict[str, Any]:
    props = raw_edge.get("properties")
    merged: Dict[str, Any] = dict(props) if isinstance(props, dict) else {}
    for key, value in raw_edge.items():
        if key in {"source", "target", "type", "properties", "target_name_to_ids"}:
            continue
        merged[key] = value
    return {key: value for key, value in merged.items() if key in EDGE_ALLOWED_PROPS}


def materialize_section(section_record: Dict[str, Any], content: str) -> Section:
    return Section(
        chapter_num=str(section_record.get("chapter_num", "")),
        chapter_title=str(section_record.get("chapter_title", "")),
        section_num=str(section_record.get("section_num", "")),
        section_title=str(section_record.get("section_title", "")),
        file_name=str(section_record.get("file", "")),
        content=content,
    )


def build_nodes(
    book_prefix: str,
    raw_nodes: Sequence[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], Dict[str, str], Dict[str, Dict[str, Any]]]:
    processed: List[Dict[str, Any]] = []
    raw_to_new: Dict[str, str] = {}
    info_by_new: Dict[str, Dict[str, Any]] = {}
    counters: Dict[str, int] = {}

    for raw_node in raw_nodes:
        if not isinstance(raw_node, dict):
            continue
        label = normalize_node_label(raw_node)
        idx = normalize_node_index(raw_node, counters)
        prefix = NODE_PREFIX_MAP.get(label, label.lower())
        node_id = f"{book_prefix}_{prefix}{idx}"
        name = str(raw_node.get("name") or raw_node.get("stem") or raw_node.get("id") or node_id).strip()
        raw_key = str(raw_node.get("id") or name)
        properties = clean_node_properties(label, raw_node, name)
        node = {
            "id": node_id,
            "label": label,
            "name": name,
        }
        if properties:
            node["properties"] = properties
        processed.append(node)
        raw_to_new[raw_key] = node_id
        info_by_new[node_id] = node

    return processed, raw_to_new, info_by_new


def build_standard_edge(
    source_id: str,
    target_id: str,
    edge_type: str,
    source_node: Dict[str, Any],
    target_node: Dict[str, Any],
    properties: Dict[str, Any],
) -> Dict[str, Any]:
    edge = {
        "source": source_id,
        "target": target_id,
        "type": edge_type,
    }
    if source_node["label"] == "Exercise":
        edge["source_stem"] = source_node.get("properties", {}).get("stem", source_node["name"])
    else:
        edge["source_name"] = source_node["name"]
    edge["target_name"] = target_node["name"]
    if properties:
        edge["properties"] = properties
    return edge


def build_edges(
    raw_edges: Sequence[Dict[str, Any]],
    raw_to_new: Dict[str, str],
    info_by_new: Dict[str, Dict[str, Any]],
) -> List[Dict[str, Any]]:
    standard_edges: List[Dict[str, Any]] = []
    aggregated: Dict[Tuple[str, str], Dict[str, Any]] = {}

    for raw_edge in raw_edges:
        if not isinstance(raw_edge, dict):
            continue
        edge_type = str(raw_edge.get("type", "")).strip()
        if not edge_type:
            continue

        source_refs = normalize_ref(raw_edge.get("source"))
        if not source_refs:
            continue
        source_id = raw_to_new.get(source_refs[0])
        if not source_id or source_id not in info_by_new:
            continue
        source_node = info_by_new[source_id]
        properties = clean_edge_properties(raw_edge)

        if edge_type in AGGREGATED_EDGE_TYPES:
            target_pairs = raw_edge.get("target_name_to_ids")
            target_refs: List[str] = []
            if isinstance(target_pairs, list):
                for item in target_pairs:
                    if isinstance(item, dict) and isinstance(item.get("target"), str):
                        target_refs.append(item["target"])
            else:
                target_refs.extend(normalize_ref(raw_edge.get("target")))

            key = (source_id, edge_type)
            bucket = aggregated.setdefault(
                key,
                {
                    "source": source_id,
                    "type": edge_type,
                    "source_stem": source_node.get("properties", {}).get("stem", source_node["name"]),
                    "target_name_to_ids": [],
                },
            )
            if properties and "properties" not in bucket:
                bucket["properties"] = properties
            seen_targets = {(item["target"], item["target_name"]) for item in bucket["target_name_to_ids"]}
            for ref in target_refs:
                target_id = raw_to_new.get(ref)
                if not target_id or target_id not in info_by_new:
                    continue
                target_node = info_by_new[target_id]
                pair = (target_id, target_node["name"])
                if pair in seen_targets:
                    continue
                seen_targets.add(pair)
                bucket["target_name_to_ids"].append({"target": target_id, "target_name": target_node["name"]})
            continue

        target_refs = normalize_ref(raw_edge.get("target"))
        for ref in target_refs:
            target_id = raw_to_new.get(ref)
            if not target_id or target_id not in info_by_new:
                continue
            target_node = info_by_new[target_id]
            standard_edges.append(
                build_standard_edge(
                    source_id=source_id,
                    target_id=target_id,
                    edge_type=edge_type,
                    source_node=source_node,
                    target_node=target_node,
                    properties=properties,
                )
            )

    standard_edges.extend(edge for edge in aggregated.values() if edge.get("target_name_to_ids"))
    return standard_edges


def validate_graph_payload(nodes: Sequence[Dict[str, Any]], edges: Sequence[Dict[str, Any]]) -> None:
    if not nodes:
        raise ExtractionValidationError("empty_graph", "LLM output contains no nodes")

    node_ids: set[str] = set()
    for node in nodes:
        node_id = str(node.get("id", "")).strip()
        if not node_id:
            raise ExtractionValidationError("invalid_node", "Encountered node without id")
        if node_id in node_ids:
            raise ExtractionValidationError("duplicate_node_id", f"Duplicate node id: {node_id}")
        node_ids.add(node_id)

    for edge in edges:
        edge_type = str(edge.get("type", "")).strip()
        source_id = str(edge.get("source", "")).strip()
        if not edge_type or not source_id:
            raise ExtractionValidationError("invalid_edge", "Encountered edge without source/type")
        if source_id not in node_ids:
            raise ExtractionValidationError("dangling_edge", f"Edge source missing from nodes: {source_id}")
        if edge_type in AGGREGATED_EDGE_TYPES:
            targets = edge.get("target_name_to_ids")
            if not isinstance(targets, list) or not targets:
                raise ExtractionValidationError("empty_aggregated_edge", f"{edge_type} has no resolved targets")
            for item in targets:
                if not isinstance(item, dict):
                    raise ExtractionValidationError("invalid_aggregated_edge", f"{edge_type} target item is not an object")
                target_id = str(item.get("target", "")).strip()
                if not target_id or target_id not in node_ids:
                    raise ExtractionValidationError("dangling_edge", f"{edge_type} target missing from nodes: {target_id}")
            continue
        target_id = str(edge.get("target", "")).strip()
        if not target_id:
            raise ExtractionValidationError("invalid_edge", "Encountered edge without target")
        if target_id not in node_ids:
            raise ExtractionValidationError("dangling_edge", f"Edge target missing from nodes: {target_id}")


def selected_sections(sections: Sequence[Dict[str, Any]], chapters_arg: Optional[str]) -> List[Dict[str, Any]]:
    if not chapters_arg:
        return [item for item in sections if isinstance(item, dict)]
    wanted = {item.strip() for item in chapters_arg.split(",") if item.strip()}
    selected: List[Dict[str, Any]] = []
    for item in sections:
        if not isinstance(item, dict):
            continue
        stem = Path(str(item.get("file", ""))).stem
        if stem in wanted:
            selected.append(item)
    return selected


def create_client(config: PipelineConfig) -> LLMClient:
    llm_cfg = config.llm
    return create_llm_client(
        provider=str(llm_cfg.get("provider", "openai")),
        model=str(llm_cfg.get("model", "gpt-4.1-mini")),
        api_key=str(llm_cfg.get("api_key", "")),
        base_url=str(llm_cfg.get("base_url", "")) or None,
    )


def safe_unlink(path: Path) -> None:
    if path.exists():
        path.unlink()


def load_sections(config: PipelineConfig, book: Dict[str, Any]) -> List[Dict[str, Any]]:
    sections_index_path = config.sections_index_for(book)
    if not sections_index_path.exists():
        raise FileNotFoundError(f"missing sections index: {sections_index_path}")
    sections_index = read_json(sections_index_path)
    sections = sections_index.get("sections", [])
    if not isinstance(sections, list):
        raise ValueError(f"invalid sections_index for {book['book_prefix']}")
    return sections


def prepare_book_dirs(
    config: PipelineConfig,
    book_prefix: str,
    *,
    overwrite: bool,
    full_book_run: bool,
) -> Tuple[Path, Path]:
    raw_dir = config.build_graph_book_dir(book_prefix)
    output_dir = config.chapter_kg_dir / book_prefix
    if overwrite and full_book_run:
        if raw_dir.exists():
            shutil.rmtree(raw_dir)
        if output_dir.exists():
            shutil.rmtree(output_dir)
    raw_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)
    return raw_dir, output_dir


def classify_exception(exc: Exception) -> str:
    if isinstance(exc, ExtractionValidationError):
        return exc.code
    if isinstance(exc, FileNotFoundError):
        return "missing_input_file"
    if isinstance(exc, ValueError):
        return "invalid_llm_output"
    return exc.__class__.__name__.lower()


def process_section(
    config: PipelineConfig,
    client: LLMClient,
    book_prefix: str,
    section_record: Dict[str, Any],
    prompt_template: str,
    max_tokens: int,
    overwrite: bool,
) -> Dict[str, Any]:
    file_name = str(section_record.get("file", "")).strip()
    if not file_name:
        return {
            "status": "invalid_section_record",
            "file": "",
            "message": "section record missing file",
        }

    stem = Path(file_name).stem
    raw_dir = config.build_graph_book_dir(book_prefix)
    output_dir = config.chapter_kg_dir / book_prefix
    output_path = output_dir / f"{stem}.json"
    raw_path = raw_dir / f"{stem}_llm_raw.txt"
    error_path = raw_dir / f"{stem}_error.json"

    if not overwrite and output_path.exists():
        return {
            "status": "skipped_existing",
            "file": file_name,
            "stem": stem,
            "output_path": str(output_path),
        }

    if overwrite:
        safe_unlink(output_path)
        safe_unlink(raw_path)
        safe_unlink(error_path)

    response: Optional[str] = None
    try:
        section_path = config.sections_dir_for(book_prefix) / file_name
        if not section_path.exists():
            raise FileNotFoundError(f"missing section markdown: {section_path}")

        content = section_path.read_text(encoding="utf-8")
        section = materialize_section(section_record, content)
        prompt = build_prompt(prompt_template, section)
        llm_cfg = config.llm
        response = client.generate(
            prompt,
            temperature=float(llm_cfg.get("temperature", 0.0)),
            max_tokens=max_tokens,
        )
        raw_path.write_text(response, encoding="utf-8")

        parsed = client.parse_response(response)
        nodes, raw_to_new, info_by_new = build_nodes(book_prefix, parsed.get("nodes", []))
        edges = build_edges(parsed.get("edges", []), raw_to_new, info_by_new)
        validate_graph_payload(nodes, edges)

        payload = {
            "section_info": {
                "chapter": section.chapter_num,
                "chapter_title": section.chapter_title,
                "section": section.section_num,
                "section_title": section.section_title,
                "file": section.file_name,
            },
            "nodes": nodes,
            "edges": edges,
        }
        write_json(output_path, payload)
        safe_unlink(error_path)
        return {
            "status": "ok",
            "file": file_name,
            "stem": stem,
            "nodes": len(nodes),
            "edges": len(edges),
            "output_path": str(output_path),
        }
    except Exception as exc:
        if response is not None and not raw_path.exists():
            raw_path.write_text(response, encoding="utf-8")
        safe_unlink(output_path)
        failure = {
            "status": classify_exception(exc),
            "file": file_name,
            "stem": stem,
            "message": str(exc),
            "exception_type": type(exc).__name__,
        }
        write_json(error_path, failure)
        return failure


def process_book(
    config: PipelineConfig,
    client: LLMClient,
    book: Dict[str, Any],
    prompt_template: str,
    chapters_arg: Optional[str],
    max_tokens: int,
    overwrite: bool,
) -> Dict[str, Any]:
    book_prefix = str(book["book_prefix"])
    sections = load_sections(config, book)
    selected = selected_sections(sections, chapters_arg)

    summary: Dict[str, Any] = {
        "book_prefix": book_prefix,
        "selected": len(selected),
        "written": 0,
        "skipped": 0,
        "failed": 0,
        "failures": [],
    }
    if not selected:
        return summary

    prepare_book_dirs(
        config,
        book_prefix,
        overwrite=overwrite,
        full_book_run=(chapters_arg is None),
    )

    for section_record in selected:
        result = process_section(
            config=config,
            client=client,
            book_prefix=book_prefix,
            section_record=section_record,
            prompt_template=prompt_template,
            max_tokens=max_tokens,
            overwrite=overwrite,
        )
        status = str(result.get("status", "")).strip()
        if status == "ok":
            summary["written"] += 1
            continue
        if status == "skipped_existing":
            summary["skipped"] += 1
            continue
        summary["failed"] += 1
        summary["failures"].append(result)

    raw_dir = config.build_graph_book_dir(book_prefix)
    write_json(raw_dir / "extract_summary.json", summary)
    return summary


def process_all(
    config_path: Optional[str],
    prompt_path: Optional[str],
    filter_prefixes: Optional[Sequence[str]],
    chapters_arg: Optional[str],
    limit: Optional[int],
    max_tokens: int,
    overwrite: bool,
) -> Dict[str, Any]:
    config = load_config(config_path)
    prompt_path_obj = Path(prompt_path) if prompt_path else THIS_DIR / "prompt.txt"
    prompt_template = load_prompt(prompt_path_obj)
    wanted = {item.strip() for item in (filter_prefixes or []) if item.strip()}
    client = create_client(config)

    summary: Dict[str, Any] = {
        "processed": 0,
        "success": 0,
        "skipped": 0,
        "failed": 0,
        "section_written": 0,
        "section_skipped": 0,
        "section_failed": 0,
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
            result = process_book(
                config=config,
                client=client,
                book=book,
                prompt_template=prompt_template,
                chapters_arg=chapters_arg,
                max_tokens=max_tokens,
                overwrite=overwrite,
            )
        except Exception as exc:
            failure = {
                "book_prefix": book_prefix,
                "status": classify_exception(exc),
                "message": str(exc),
                "exception_type": type(exc).__name__,
            }
            summary["failed"] += 1
            summary["failures"].append(failure)
            print(f"[FAIL] {book_prefix} ({failure['status']})")
            count += 1
            if limit and count >= limit:
                break
            continue

        summary["books"].append(result)
        summary["section_written"] += int(result.get("written", 0))
        summary["section_skipped"] += int(result.get("skipped", 0))
        summary["section_failed"] += int(result.get("failed", 0))

        if int(result.get("failed", 0)) > 0:
            summary["failed"] += 1
            for item in result.get("failures", []):
                failure = dict(item)
                failure["book_prefix"] = book_prefix
                summary["failures"].append(failure)
            print(
                f"[FAIL] {book_prefix} -> {result.get('written', 0)} written, "
                f"{result.get('failed', 0)} failed"
            )
        elif int(result.get("written", 0)) == 0:
            summary["skipped"] += 1
            print(f"[SKIP] {book_prefix} -> {result.get('skipped', 0)} skipped")
        else:
            summary["success"] += 1
            print(
                f"[OK] {book_prefix} -> {result.get('written', 0)} written, "
                f"{result.get('skipped', 0)} skipped"
            )

        count += 1
        if limit and count >= limit:
            break

    return summary


def main() -> None:
    args = parse_args()
    summary = process_all(
        config_path=args.config,
        prompt_path=args.prompt,
        filter_prefixes=args.filter_prefix,
        chapters_arg=args.chapters,
        limit=args.limit,
        max_tokens=args.max_tokens,
        overwrite=not args.no_overwrite,
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
