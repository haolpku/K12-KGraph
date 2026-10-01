#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Step 2 of the multimodal pipeline: extract figure nodes and figure relations.

Three vision stages per figure:

1. is the figure worth putting in the graph at all
2. what role does it play in the textbook, and which visual elements does it contain
3. where are those elements (bounding boxes) and what do they point at

Results are merged onto the text-side book graph, so every edge written here
connects two nodes that exist in the output graph.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple


# ``python src/mm/extract_figures.py`` puts src/mm (not src) on sys.path.
_SRC_DIR = Path(__file__).resolve().parents[1]
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from utils.config import PipelineConfig, load_config  # noqa: E402
from utils.graph import dangling_edges, load_graph  # noqa: E402
from utils.io import read_json, write_json  # noqa: E402
from utils.k12_ids import normalize_book_prefix, section_node_id  # noqa: E402
from utils.llm_client import OpenAIClient  # noqa: E402
from utils.prompts import load_prompt  # noqa: E402
from utils.schema import (  # noqa: E402
    BBOX_CONFIDENCE_THRESHOLD,
    EDGE_CONFIDENCE_THRESHOLDS,
    FIGURE_ROLES,
    MM_TARGET_LABELS,
)


PIPELINE_VERSION = "mmkg_v2"
JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)
PROMPT_DIR = Path(__file__).resolve().parent / "prompts"

def read_prompt_template(filename: str) -> str:
    return load_prompt(PROMPT_DIR / filename)


STAGE1_SYSTEM_PROMPT = read_prompt_template("stage1_system.txt")
STAGE1_USER_PROMPT_TEMPLATE = read_prompt_template("stage1_user.txt")
STAGE2_SYSTEM_PROMPT = read_prompt_template("stage2_system.txt")
STAGE2_USER_PROMPT_TEMPLATE = read_prompt_template("stage2_user.txt")
STAGE3_SYSTEM_PROMPT = read_prompt_template("stage3_system.txt")
STAGE3_USER_PROMPT_TEMPLATE = read_prompt_template("stage3_user.txt")


@dataclass
class CandidateNode:
    node_id: str
    label: str
    name: str


@dataclass
class CandidateEdge:
    edge_ref: str
    source: str
    target: str
    edge_type: str
    evidence: str


@dataclass
class ExerciseCandidate:
    node_id: str
    name: str
    stem: str
    answer: str
    analysis: str
    source_md: str
    question_no: Optional[int]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Extract figure nodes and figure relations for one book")
    parser.add_argument("--config", default=None, help="Pipeline config path; defaults to config/default.yaml")
    parser.add_argument("--book-prefix", default=None, help="Book prefix, e.g. math_7a_rjb")
    parser.add_argument("--aligned-sections", default=None, help="Defaults to <workspace>/mm/<book>/aligned_sections.json")
    parser.add_argument("--output", default=None, help="Output graph; defaults to <data>/mmkg/<book>.json")
    parser.add_argument("--book-graph", default=None, help="Text-side book graph; defaults to <data>/book_kg/<book>.json")
    parser.add_argument("--exercises", default=None, help="Afterclass exercises; defaults to <data>/afterclass_exercises/<book>.json")
    parser.add_argument("--model", default=os.environ.get("OPENAI_MODEL", "gpt-4o-mini"), help="Multimodal model name")
    parser.add_argument("--api-base", default=os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"), help="OpenAI-compatible API base URL")
    parser.add_argument("--api-key-env", default="OPENAI_API_KEY", help="Environment variable holding the API key")
    parser.add_argument("--limit", type=int, default=None, help="Process at most N sections in this run")
    parser.add_argument("--dry-run", action="store_true", help="Build the graph without calling the model")
    parser.add_argument("--resume", action="store_true", help="Skip sections that already have a partial output")
    parser.add_argument("--section-id", action="append", default=None, help="Only run these section ids (repeatable)")
    return parser.parse_args()


def partial_output_path(partials_dir: Path, section_id: str) -> Path:
    return partials_dir / f"{section_id}.json"


def short_text(text: str, limit: int = 240) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    return text[: limit - 1] + "…" if len(text) > limit else text


def escape_invalid_json_backslashes(text: str) -> str:
    r"""Escape single backslashes that would break JSON decoding.

    This is a narrow fallback for model outputs that copy textbook LaTeX like
    ``\mathrm`` or ``\angle`` into JSON strings without escaping the backslash.
    """

    result: List[str] = []
    in_string = False
    escaped = False
    i = 0
    length = len(text)

    while i < length:
        ch = text[i]

        if not in_string:
            result.append(ch)
            if ch == '"':
                in_string = True
            i += 1
            continue

        if escaped:
            result.append(ch)
            escaped = False
            i += 1
            continue

        if ch == '\\':
            next_ch = text[i + 1] if i + 1 < length else ''
            if next_ch in {'"', '\\', '/', 'b', 'f', 'n', 'r', 't'}:
                result.append(ch)
                escaped = True
            elif next_ch == 'u' and i + 5 < length and all(c in '0123456789abcdefABCDEF' for c in text[i + 2 : i + 6]):
                result.append(ch)
                escaped = True
            else:
                result.append('\\\\')
            i += 1
            continue

        result.append(ch)
        if ch == '"':
            in_string = False
        i += 1

    return ''.join(result)


def parse_json_response(text: str) -> Dict[str, Any]:
    text = text.strip()
    fence = JSON_BLOCK_RE.search(text)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        if "Invalid \\escape" not in str(exc):
            raise
        return json.loads(escape_invalid_json_backslashes(text))


class VisionExtractor:
    """One JSON-returning multimodal call, on top of the shared LLM client.

    The request shape (system prompt + user prompt + zero or more images) lives in
    ``utils.llm_client``; this class only adds the JSON convention used by the
    three extraction stages.
    """

    def __init__(self, client: OpenAIClient) -> None:
        self.client = client

    def extract_json(self, system_prompt: str, user_prompt: str, image_paths: Sequence[Path]) -> Dict[str, Any]:
        content = self.client.generate_with_images(
            system_prompt,
            user_prompt,
            image_paths,
            temperature=0.0,
            max_tokens=4000,
            json_mode=True,
        )
        return parse_json_response(content)


def section_candidates(
    book_graph: Dict[str, Any],
    book_prefix: str,
    section_id: str,
) -> Tuple[List[CandidateNode], List[CandidateEdge]]:
    """Knowledge points and edges of the book graph that belong to *section_id*.

    Membership comes from the ``appears_in`` edges built by ``src/kg/merge_kg.py``.
    Because the ids are taken from the book graph itself, every relation the model
    proposes resolves against the same graph the figures are merged into.
    """
    nodes_by_id = {
        str(node["id"]): node
        for node in book_graph.get("nodes", [])
        if isinstance(node, dict) and node.get("id")
    }
    target_section = section_node_id(book_prefix, section_id)

    member_ids = {
        str(edge.get("source"))
        for edge in book_graph.get("edges", [])
        if isinstance(edge, dict)
        and edge.get("type") == "appears_in"
        and str(edge.get("target")) == target_section
    }
    if not member_ids:
        # No section membership available: fall back to the whole book.
        member_ids = {
            node_id
            for node_id, node in nodes_by_id.items()
            if str(node.get("label")) in MM_TARGET_LABELS
        }

    candidate_nodes = [
        CandidateNode(
            node_id=node_id,
            label=str(nodes_by_id[node_id].get("label", "")),
            name=str(nodes_by_id[node_id].get("name") or node_id),
        )
        for node_id in sorted(member_ids)
        if node_id in nodes_by_id and str(nodes_by_id[node_id].get("label")) in MM_TARGET_LABELS
    ]

    member_set = {node.node_id for node in candidate_nodes}
    candidate_edges: List[CandidateEdge] = []
    for idx, edge in enumerate(book_graph.get("edges", []), start=1):
        if not isinstance(edge, dict):
            continue
        source = str(edge.get("source", ""))
        target = str(edge.get("target", ""))
        if source not in member_set or target not in member_set:
            continue
        props = edge.get("properties", {}) or {}
        evidence = props.get("evidence") or props.get("original_text") or ""
        candidate_edges.append(
            CandidateEdge(
                edge_ref=f"edge_{idx:03d}",
                source=source,
                target=target,
                edge_type=str(edge.get("type", "")),
                evidence=short_text(str(evidence), limit=160),
            )
        )
    return candidate_nodes, candidate_edges


def format_candidates(nodes: Sequence[CandidateNode], edges: Sequence[CandidateEdge]) -> Tuple[str, str]:
    node_lines = [f"- {node.node_id} | {node.label} | {node.name}" for node in nodes]
    edge_lines = [f"- {edge.edge_ref} | {edge.source} -[{edge.edge_type}]-> {edge.target} | 证据: {edge.evidence or 'N/A'}" for edge in edges]
    return "\n".join(node_lines) or "(none)", "\n".join(edge_lines) or "(none)"


def load_exercise_candidates(exercise_path: Optional[Path]) -> Dict[str, List[ExerciseCandidate]]:
    """Read ``data/afterclass_exercises/<book>.json`` grouped by section."""
    by_section: Dict[str, List[ExerciseCandidate]] = {}
    if exercise_path is None or not Path(exercise_path).exists():
        return by_section

    payload = read_json(exercise_path)
    questions = payload.get("questions", []) if isinstance(payload, dict) else payload
    for raw in questions or []:
        if not isinstance(raw, dict):
            continue
        meta = raw.get("meta", {}) or {}
        source_md = str(meta.get("source_md", "") or "")
        if not source_md:
            continue
        section_id = Path(source_md).stem
        by_section.setdefault(section_id, []).append(
            ExerciseCandidate(
                node_id=str(raw.get("id", "")),
                name=str(raw.get("id", "")),
                stem=str(raw.get("stem", "")),
                answer=str(raw.get("answer", "")),
                analysis=str(raw.get("analysis", "")),
                source_md=source_md,
                question_no=meta.get("question_no_in_source"),
            )
        )
    return by_section


def exercise_nodes(exercises: Sequence[ExerciseCandidate]) -> List[Dict[str, Any]]:
    """Exercise nodes to inject, so ``requires_figure`` edges have a real target."""
    return [
        {
            "id": exercise.node_id,
            "label": "Exercise",
            "name": exercise.name,
            "properties": {
                "stem": exercise.stem,
                "answer": exercise.answer,
                "analysis": exercise.analysis,
                "source_section": Path(exercise.source_md).stem,
            },
        }
        for exercise in exercises
        if exercise.node_id
    ]


def merge_exercise_candidates(candidate_nodes: Sequence[CandidateNode], exercises: Sequence[ExerciseCandidate]) -> List[CandidateNode]:
    merged = {node.node_id: node for node in candidate_nodes}
    for exercise in exercises:
        merged[exercise.node_id] = CandidateNode(exercise.node_id, "Exercise", exercise.name)
    return list(merged.values())


def format_exercise_catalog(exercises: Sequence[ExerciseCandidate]) -> str:
    lines = []
    for exercise in exercises:
        lines.append(
            json.dumps(
                {
                    "exercise_id": exercise.node_id,
                    "name": exercise.name,
                    "stem": exercise.stem,
                    "answer": exercise.answer,
                    "analysis": exercise.analysis,
                    "source_md": exercise.source_md,
                    "question_no_in_source": exercise.question_no,
                },
                ensure_ascii=False,
            )
        )
    return "\n".join(lines) or "(none)"


def normalize_yes_no(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text in {"yes", "y", "true", "1", "是", "相关"}:
        return "yes"
    return "no"


def build_stage1_prompt(record: Dict[str, Any], figure: Dict[str, Any]) -> str:
    section_metadata = json.dumps({"book_prefix": record["book_prefix"], "section_id": record["section_id"], "section_title": record["section_title"]}, ensure_ascii=False, indent=2)
    figure_hint = json.dumps(
        {
            "figure_id": figure["figure_id"],
            "image_count": len(figure.get("image_paths", [])),
            "image_rel_paths": figure.get("image_rel_paths", []),
        },
        ensure_ascii=False,
        indent=2,
    )
    return STAGE1_USER_PROMPT_TEMPLATE.format(section_metadata=section_metadata, section_text=record["raw_chunk"], figure_hint=figure_hint, figure_id=figure["figure_id"])


def build_stage2_prompt(record: Dict[str, Any], figure: Dict[str, Any], analysis: Dict[str, Any]) -> str:
    section_metadata = json.dumps({"book_prefix": record["book_prefix"], "section_id": record["section_id"], "section_title": record["section_title"]}, ensure_ascii=False, indent=2)
    figure_info = json.dumps(
        {
            "figure_id": figure["figure_id"],
            "name": analysis.get("name", ""),
            "textual_evidence": analysis.get("textual_evidence", ""),
            "description": analysis.get("description", ""),
        },
        ensure_ascii=False,
        indent=2,
    )
    role_choices = "\n".join(f"- {role}" for role in FIGURE_ROLES)
    role_hints = """六个合法 figure_role 的含义：
- 说明概念：主要用于可视化呈现某个概念、结构、原理或过程，让读者直观看到教材正在讲什么。
- 引入生活情境：主要用于可视化呈现生活现象、真实场景或直观问题，用来把读者带入后续要学习的内容。
- 展示实验：主要用于可视化呈现实验装置、实验过程、实验现象或实验观察结果。
- 辅助解题：主要用于可视化呈现题目中的条件、关系或几何/示意信息，帮助理解题意并完成作答或推理。
- 总结归纳：主要用于可视化呈现对前面知识的回顾、整理、对比或归纳。
- 呈现数据：主要用于可视化呈现统计图、表格、对比现象、变化趋势或观察数据。"""
    return STAGE2_USER_PROMPT_TEMPLATE.format(
        section_metadata=section_metadata,
        section_text=record["raw_chunk"],
        figure_info=figure_info,
        role_choices=role_choices,
        role_hints=role_hints,
        figure_id=figure["figure_id"],
    )


def build_stage3_prompt(record: Dict[str, Any], figure: Dict[str, Any], analysis: Dict[str, Any], enrichment: Dict[str, Any], candidate_nodes: Sequence[CandidateNode], candidate_edges: Sequence[CandidateEdge], exercises: Sequence[ExerciseCandidate]) -> str:
    section_metadata = json.dumps({"book_prefix": record["book_prefix"], "section_id": record["section_id"], "section_title": record["section_title"]}, ensure_ascii=False, indent=2)
    figure_info = json.dumps(
        {
            "figure_id": figure["figure_id"],
            "name": analysis.get("name", ""),
            "textual_evidence": analysis.get("textual_evidence", ""),
            "description": analysis.get("description", ""),
            "figure_role": enrichment.get("figure_role", ""),
            "figure_role_rationale": enrichment.get("figure_role_rationale", ""),
        },
        ensure_ascii=False,
        indent=2,
    )
    visual_elements_json = json.dumps(enrichment.get("visual_elements", []), ensure_ascii=False, indent=2)
    candidate_node_text, candidate_edge_text = format_candidates(candidate_nodes, candidate_edges)
    return STAGE3_USER_PROMPT_TEMPLATE.format(
        section_metadata=section_metadata,
        section_text=record["raw_chunk"],
        figure_info=figure_info,
        visual_elements_json=visual_elements_json,
        candidate_nodes=candidate_node_text,
        candidate_edges=candidate_edge_text,
        exercise_catalog=format_exercise_catalog(exercises),
        figure_id=figure["figure_id"],
    )


def analyze_figure(client: VisionExtractor, record: Dict[str, Any], figure: Dict[str, Any]) -> Dict[str, Any]:
    prompt = build_stage1_prompt(record, figure)
    image_paths = [Path(path) for path in figure.get("image_paths", [])]
    result = client.extract_json(STAGE1_SYSTEM_PROMPT, prompt, image_paths)
    result.setdefault("figure_id", figure["figure_id"])
    result["figure_id"] = figure["figure_id"]
    result["image_paths"] = figure.get("image_paths", [])
    result["name"] = str(result.get("name", "") or figure["figure_id"]).strip()
    result["textual_evidence"] = str(result.get("textual_evidence", "") or "").strip()
    result["description"] = str(result.get("description", "") or "").strip()
    result["is_kg_relevant"] = normalize_yes_no(result.get("is_kg_relevant", "no"))
    result["irrelevant_reason"] = str(result.get("irrelevant_reason", "") or "").strip()
    return result


def extract_figure_structure(client: VisionExtractor, record: Dict[str, Any], figure: Dict[str, Any], analysis: Dict[str, Any]) -> Dict[str, Any]:
    prompt = build_stage2_prompt(record, figure, analysis)
    image_paths = [Path(path) for path in figure.get("image_paths", [])]
    result = client.extract_json(STAGE2_SYSTEM_PROMPT, prompt, image_paths)
    role = str(result.get("figure_role", "") or "").strip()
    if role not in FIGURE_ROLES:
        raise ValueError(f"Invalid figure_role for {figure['figure_id']}: {role!r}")

    visual_elements: List[Dict[str, Any]] = []
    for idx, item in enumerate(result.get("visual_elements", []) or [], start=1):
        if not isinstance(item, dict):
            continue
        visual_elements.append(
            {
                "ve_id": build_visual_element_id(record["book_prefix"], figure, idx),
                "name": str(item.get("name", f"视觉元素{idx}") or f"视觉元素{idx}").strip(),
                "description": str(item.get("description", "") or "").strip(),
                "text_on_image": str(item.get("text_on_image", "") or "").strip(),
            }
        )
    return {
        "figure_id": figure["figure_id"],
        "figure_role": role,
        "figure_role_rationale": str(result.get("figure_role_rationale", "") or "").strip(),
        "visual_elements": visual_elements,
    }


def is_valid_bbox(value: Any) -> bool:
    return (
        isinstance(value, list)
        and len(value) == 4
        and all(isinstance(item, (int, float)) and not isinstance(item, bool) for item in value)
        and 0 <= value[0] < value[2] <= 1000
        and 0 <= value[1] < value[3] <= 1000
    )


def confidence_value(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def extract_figure_relations(client: VisionExtractor, record: Dict[str, Any], figure: Dict[str, Any], analysis: Dict[str, Any], enrichment: Dict[str, Any], candidate_nodes: Sequence[CandidateNode], candidate_edges: Sequence[CandidateEdge], exercises: Sequence[ExerciseCandidate]) -> Dict[str, Any]:
    prompt = build_stage3_prompt(record, figure, analysis, enrichment, candidate_nodes, candidate_edges, exercises)
    image_paths = [Path(path) for path in figure.get("image_paths", [])]
    result = client.extract_json(STAGE3_SYSTEM_PROMPT, prompt, image_paths)
    returned_elements = {
        str(item.get("ve_id", "")): item
        for item in result.get("visual_elements", []) or []
        if isinstance(item, dict) and item.get("ve_id")
    }
    visual_elements: List[Dict[str, Any]] = []
    for source in enrichment.get("visual_elements", []) or []:
        item = dict(source)
        annotation = returned_elements.get(str(item.get("ve_id", "")), {})
        bbox = annotation.get("bbox_2d")
        item["bbox_2d"] = bbox if is_valid_bbox(bbox) else None
        item["bbox_confidence"] = confidence_value(annotation.get("confidence"))
        item["bbox_rationale"] = str(annotation.get("rationale", "") or "").strip()
        item["refers_to"] = annotation.get("refers_to", []) if isinstance(annotation.get("refers_to", []), list) else []
        visual_elements.append(item)
    return {
        "figure_id": figure["figure_id"],
        "illustrates": result.get("illustrates", []) if isinstance(result.get("illustrates", []), list) else [],
        "visual_elements": visual_elements,
        "supports_edges": result.get("supports_edges", []) if isinstance(result.get("supports_edges", []), list) else [],
        "required_by_exercises": result.get("required_by_exercises", []) if isinstance(result.get("required_by_exercises", []), list) else [],
    }


def build_section_node(record: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": section_node_id(record["book_prefix"], record["section_id"]),
        "label": "Section",
        "name": record["section_title"],
        "properties": {
            "section_id": record["section_id"],
            "name": record["section_title"],
            "source_section_path": record.get("section_markdown_path"),
            "source_markdown_path": record.get("source_markdown_path"),
        },
    }


def build_figure_node(record: Dict[str, Any], figure: Dict[str, Any], analysis: Dict[str, Any], enrichment: Dict[str, Any]) -> Dict[str, Any]:
    figure_node_id = figure["figure_id"]
    image_paths = list(figure.get("image_paths", []))
    image_rel_paths = list(figure.get("image_rel_paths", []))
    return {
        "id": figure_node_id,
        "label": "Figure",
        "properties": {
            "id": figure_node_id,
            "name": analysis.get("name") or figure["figure_id"],
            "img_path": image_paths[0] if image_paths else "",
            "image_ids": figure.get("image_ids", []),
            "image_paths": image_paths,
            "image_rel_paths": image_rel_paths,
            "textual_evidence": analysis.get("textual_evidence", ""),
            "description": analysis.get("description", ""),
            "figure_role": enrichment.get("figure_role", ""),
            "figure_role_rationale": enrichment.get("figure_role_rationale", ""),
            "source_section": record["section_id"],
            "source_section_title": record["section_title"],
        },
    }


def build_appears_edge(record: Dict[str, Any], figure: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "source": figure["figure_id"],
        "target": section_node_id(record["book_prefix"], record["section_id"]),
        "type": "appears_in",
        "properties": {
            "deterministic": True,
            "source_section": record["section_id"],
            "source_section_title": record["section_title"],
        },
    }


def add_node(nodes_by_id: Dict[str, Dict[str, Any]], node: Dict[str, Any]) -> None:
    nodes_by_id.setdefault(node["id"], node)


def edge_key(edge: Dict[str, Any]) -> Tuple[str, str, str]:
    return (str(edge.get("source", "")), str(edge.get("type", "")), str(edge.get("target", "")))


def add_edge(edges_by_key: Dict[Tuple[str, str, str], Dict[str, Any]], edge: Dict[str, Any]) -> None:
    edges_by_key.setdefault(edge_key(edge), edge)


def build_edge_ref_node(book_prefix: str, section_id: str, candidate: CandidateEdge) -> Dict[str, Any]:
    return {"id": f"{book_prefix}_{section_id}_{candidate.edge_ref}", "label": "Edge", "properties": {"edge_ref": candidate.edge_ref, "source": candidate.source, "target": candidate.target, "edge_type": candidate.edge_type, "evidence": candidate.evidence, "source_section": section_id}}


def build_visual_element_id(book_prefix: str, figure: Dict[str, Any], index: int) -> str:
    figure_index = figure.get("figure_index")
    if isinstance(figure_index, int):
        return f"{book_prefix}_ve{figure_index}_{index}"
    figure_num = re.sub(r"^\D+", "", str(figure.get("figure_id", "")))
    suffix = figure_num or str(index)
    return f"{book_prefix}_ve{suffix}_{index}"


def build_incremental_graph_for_section(record: Dict[str, Any], candidate_nodes: Sequence[CandidateNode], candidate_edges: Sequence[CandidateEdge], figure_analyses: Sequence[Dict[str, Any]], figure_enrichments: Sequence[Dict[str, Any]], relation_results: Sequence[Dict[str, Any]], book_prefix: str) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    nodes_by_id: Dict[str, Dict[str, Any]] = {}
    edges_by_key: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
    add_node(nodes_by_id, build_section_node(record))

    figure_by_id = {fig["figure_id"]: fig for fig in record.get("figures", [])}
    analysis_by_id = {item["figure_id"]: item for item in figure_analyses if item.get("is_kg_relevant") == "yes"}
    enrichment_by_id = {item["figure_id"]: item for item in figure_enrichments}
    relation_by_id = {item["figure_id"]: item for item in relation_results}
    candidate_node_ids = {node.node_id for node in candidate_nodes}
    candidate_edge_by_ref = {edge.edge_ref: edge for edge in candidate_edges}

    for figure_id, analysis in analysis_by_id.items():
        figure = figure_by_id.get(figure_id)
        if figure is None:
            continue
        figure_node_id = figure["figure_id"]
        enrichment = enrichment_by_id.get(figure_id, {})
        add_node(nodes_by_id, build_figure_node(record, figure, analysis, enrichment))
        add_edge(edges_by_key, build_appears_edge(record, figure))

        relation = relation_by_id.get(figure_id, {})
        for idx, visual_element in enumerate(relation.get("visual_elements", []), start=1):
            ve_id = str(visual_element.get("ve_id") or build_visual_element_id(book_prefix, figure, idx))
            ve_properties = {
                "id": ve_id,
                "name": str(visual_element.get("name", f"视觉元素{idx}")),
                "description": str(visual_element.get("description", "")),
                "text_on_image": str(visual_element.get("text_on_image", "")),
                "source_figure": figure_node_id,
                "source_section": record["section_id"],
            }
            bbox = visual_element.get("bbox_2d")
            bbox_confidence = confidence_value(visual_element.get("bbox_confidence"))
            if is_valid_bbox(bbox) and bbox_confidence >= BBOX_CONFIDENCE_THRESHOLD:
                ve_properties["bbox_2d"] = bbox
                ve_properties["bbox_confidence"] = bbox_confidence
                bbox_rationale = str(visual_element.get("bbox_rationale", "") or "").strip()
                if bbox_rationale:
                    ve_properties["bbox_rationale"] = bbox_rationale
            ve_node = {
                "id": ve_id,
                "label": "VisualElement",
                "properties": ve_properties,
            }
            add_node(nodes_by_id, ve_node)
            add_edge(edges_by_key, {"source": figure_node_id, "target": ve_id, "type": "contains_visual_element", "properties": {"source_section": record["section_id"]}})
            for link in visual_element.get("refers_to", []):
                target_id = link.get("target_id")
                if target_id not in candidate_node_ids or confidence_value(link.get("confidence")) < EDGE_CONFIDENCE_THRESHOLDS["refers_to"]:
                    continue
                add_edge(edges_by_key, {"source": ve_id, "target": target_id, "type": "refers_to", "properties": {"rationale": str(link.get("rationale", "")), "confidence": confidence_value(link.get("confidence")), "source_section": record["section_id"]}})

        for link in relation.get("illustrates", []):
            target_id = link.get("target_id")
            if target_id not in candidate_node_ids or confidence_value(link.get("confidence")) < EDGE_CONFIDENCE_THRESHOLDS["illustrates"]:
                continue
            add_edge(edges_by_key, {"source": figure_node_id, "target": target_id, "type": "illustrates", "properties": {"rationale": str(link.get("rationale", "")), "confidence": confidence_value(link.get("confidence")), "source_section": record["section_id"]}})

        for supported in relation.get("supports_edges", []):
            edge_ref = supported.get("edge_ref")
            candidate = candidate_edge_by_ref.get(edge_ref)
            if candidate is None or confidence_value(supported.get("confidence")) < EDGE_CONFIDENCE_THRESHOLDS["supports_edge"]:
                continue
            edge_node = build_edge_ref_node(book_prefix, record["section_id"], candidate)
            add_node(nodes_by_id, edge_node)
            add_edge(edges_by_key, {"source": figure_node_id, "target": edge_node["id"], "type": "supports_edge", "properties": {"rationale": str(supported.get("rationale", "")), "confidence": confidence_value(supported.get("confidence")), "source_section": record["section_id"]}})

        for req in relation.get("required_by_exercises", []):
            exercise_id = req.get("exercise_id")
            if exercise_id not in candidate_node_ids or confidence_value(req.get("confidence")) < EDGE_CONFIDENCE_THRESHOLDS["requires_figure"]:
                continue
            add_edge(edges_by_key, {"source": exercise_id, "target": figure_node_id, "type": "requires_figure", "properties": {"rationale": str(req.get("rationale", "")), "confidence": confidence_value(req.get("confidence")), "source_section": record["section_id"]}})

    return prune_isolated_figures(nodes_by_id, edges_by_key)


def prune_isolated_figures(nodes_by_id: Dict[str, Dict[str, Any]], edges_by_key: Dict[Tuple[str, str, str], Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    local_node_ids = set(nodes_by_id.keys())
    labels = {node_id: node["label"] for node_id, node in nodes_by_id.items()}
    refers_from_ve: Dict[str, bool] = {}
    figure_to_section: Dict[str, str] = {}
    kept_figures: set[str] = set()
    kept_ve: set[str] = set()
    kept_edge_nodes: set[str] = set()

    for edge in edges_by_key.values():
        if edge["type"] == "refers_to" and labels.get(edge["source"]) == "VisualElement":
            refers_from_ve[edge["source"]] = True

    for edge in edges_by_key.values():
        src, dst, etype = edge["source"], edge["target"], edge["type"]
        src_label, dst_label = labels.get(src), labels.get(dst)
        if etype == "appears_in" and src_label == "Figure" and dst_label == "Section":
            figure_to_section[src] = dst
        if src_label == "Figure" and etype == "illustrates":
            kept_figures.add(src)
        elif src_label == "Figure" and etype == "supports_edge" and dst_label == "Edge":
            kept_figures.add(src)
            kept_edge_nodes.add(dst)
        elif etype == "requires_figure" and dst_label == "Figure":
            kept_figures.add(dst)
        elif src_label == "Figure" and etype == "contains_visual_element" and dst_label == "VisualElement" and refers_from_ve.get(dst, False):
            kept_figures.add(src)
            kept_ve.add(dst)

    kept_sections = {figure_to_section[fig] for fig in kept_figures if fig in figure_to_section}
    kept_node_ids = kept_figures | kept_sections | kept_ve | kept_edge_nodes

    filtered_nodes = [node for node_id, node in nodes_by_id.items() if node_id in kept_node_ids]
    filtered_edges: List[Dict[str, Any]] = []
    for edge in edges_by_key.values():
        src, dst = edge["source"], edge["target"]
        src_ok = (src not in local_node_ids) or (src in kept_node_ids)
        dst_ok = (dst not in local_node_ids) or (dst in kept_node_ids)
        if src_ok and dst_ok:
            filtered_edges.append(edge)
    return filtered_nodes, filtered_edges


def build_section_result(record: Dict[str, Any], nodes: Sequence[Dict[str, Any]], edges: Sequence[Dict[str, Any]], report_item: Dict[str, Any], figure_analyses: Sequence[Dict[str, Any]], figure_enrichments: Sequence[Dict[str, Any]], relation_results: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    return {
        "pipeline_version": PIPELINE_VERSION,
        "section_id": record["section_id"],
        "section_title": record["section_title"],
        "book_prefix": record["book_prefix"],
        "figure_analyses": list(figure_analyses),
        "figure_enrichments": list(figure_enrichments),
        "relation_results": list(relation_results),
        "nodes": list(nodes),
        "edges": list(edges),
        "report": report_item,
    }


def load_section_result(path: Path) -> Dict[str, Any]:
    return read_json(path)


def collect_partial_results(partials_dir: Path) -> Tuple[Dict[str, Dict[str, Any]], Dict[Tuple[str, str, str], Dict[str, Any]], List[Dict[str, Any]]]:
    """Merge the per-section partial files into one figure graph."""
    nodes_by_id: Dict[str, Dict[str, Any]] = {}
    edges_by_key: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
    extraction_report: List[Dict[str, Any]] = []
    if not partials_dir.exists():
        return nodes_by_id, edges_by_key, extraction_report
    for path in sorted(partials_dir.glob("*.json")):
        try:
            result = load_section_result(path)
        except (OSError, ValueError):
            continue
        if result.get("pipeline_version") != PIPELINE_VERSION:
            continue
        for node in result.get("nodes", []):
            add_node(nodes_by_id, node)
        for edge in result.get("edges", []):
            add_edge(edges_by_key, edge)
        report_item = result.get("report")
        if isinstance(report_item, dict):
            extraction_report.append(report_item)
    extraction_report.sort(key=lambda item: item.get("section_id", ""))
    return nodes_by_id, edges_by_key, extraction_report


def assemble_book_graph(
    book_graph: Dict[str, Any],
    exercises_by_section: Dict[str, List[ExerciseCandidate]],
    figure_nodes: Dict[str, Dict[str, Any]],
    figure_edges: Dict[Tuple[str, str, str], Dict[str, Any]],
) -> Dict[str, Any]:
    """One complete graph per book: text nodes + exercise nodes + figure nodes.

    Starting from the text-side graph is what keeps the result self-consistent --
    every ``illustrates`` / ``refers_to`` target is already a node in here.
    """
    nodes: Dict[str, Dict[str, Any]] = {}
    for node in book_graph.get("nodes", []):
        if isinstance(node, dict) and node.get("id"):
            nodes.setdefault(str(node["id"]), node)
    for group in exercises_by_section.values():
        for node in exercise_nodes(group):
            nodes.setdefault(node["id"], node)
    for node_id, node in figure_nodes.items():
        nodes.setdefault(node_id, node)

    edges: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
    for edge in book_graph.get("edges", []):
        if isinstance(edge, dict):
            edges.setdefault(edge_key(edge), edge)
    for key, edge in figure_edges.items():
        edges.setdefault(key, edge)

    return {"nodes": list(nodes.values()), "edges": list(edges.values())}


def main() -> None:
    args = parse_args()
    config = load_config(args.config)

    if args.book_prefix:
        book_prefix = normalize_book_prefix(args.book_prefix)
    elif args.aligned_sections:
        probe = read_json(Path(args.aligned_sections))
        book_prefix = normalize_book_prefix(str(probe[0].get("book_prefix", "")) if probe else "")
    else:
        raise SystemExit("Provide --book-prefix, or --aligned-sections so the book can be read from it")
    if not book_prefix:
        raise SystemExit("Cannot determine the book prefix")

    aligned_path = Path(args.aligned_sections).resolve() if args.aligned_sections else config.aligned_figures_for(book_prefix)
    if not aligned_path.exists():
        raise SystemExit(f"Aligned sections not found: {aligned_path}\nRun src/mm/align_figures.py first.")
    records = read_json(aligned_path)
    if not isinstance(records, list) or not records:
        raise SystemExit(f"aligned_sections must be a non-empty JSON array: {aligned_path}")

    book_graph_path = Path(args.book_graph).resolve() if args.book_graph else config.book_kg_dir / f"{book_prefix}.json"
    if not book_graph_path.exists():
        raise SystemExit(
            f"Text-side book graph not found: {book_graph_path}\n"
            f"Run `python src/kg/run_pipeline.py --filter-prefix {book_prefix}` first."
        )
    book_graph = load_graph(book_graph_path)

    exercises_path = Path(args.exercises).resolve() if args.exercises else config.afterclass_exercises_output_for(book_prefix)
    exercise_by_section = load_exercise_candidates(exercises_path)

    output_path = Path(args.output).resolve() if args.output else config.mmkg_book_graph_for(book_prefix)
    partials_dir = config.mm_partials_dir_for(book_prefix)

    if args.section_id:
        wanted = set(args.section_id)
        records = [record for record in records if record.get("section_id") in wanted]

    client: Optional[VisionExtractor] = None
    if not args.dry_run:
        api_key = os.environ.get(args.api_key_env, "")
        if not api_key:
            raise SystemExit(
                f"Missing API key in ${args.api_key_env}. "
                f"Use --dry-run to build the graph without calling the model."
            )
        client = VisionExtractor(OpenAIClient(model=args.model, api_key=api_key, base_url=args.api_base))

    if args.resume:
        already_done = set()
        if partials_dir.exists():
            for path in partials_dir.glob("*.json"):
                try:
                    result = load_section_result(path)
                except (OSError, ValueError):
                    continue
                if result.get("pipeline_version") == PIPELINE_VERSION:
                    already_done.add(path.stem)
        records = [record for record in records if record.get("section_id") not in already_done]

    if args.limit is not None:
        records = records[: args.limit]

    processed_this_run = 0
    for record in records:
        candidate_nodes, candidate_edges = section_candidates(book_graph, book_prefix, record["section_id"])
        section_exercises = exercise_by_section.get(record["section_id"], [])
        prompt_nodes = merge_exercise_candidates(candidate_nodes, section_exercises)
        selected_figures = list(record.get("figures", []))

        figure_analyses: List[Dict[str, Any]] = []
        figure_enrichments: List[Dict[str, Any]] = []
        relation_results: List[Dict[str, Any]] = []
        stage1_errors: List[Dict[str, str]] = []
        stage2_errors: List[Dict[str, str]] = []
        stage3_errors: List[Dict[str, str]] = []

        for figure in selected_figures:
            if client is None:
                analysis = {
                    "figure_id": figure["figure_id"],
                    "image_paths": figure.get("image_paths", []),
                    "name": figure["figure_id"],
                    "textual_evidence": "",
                    "description": "",
                    "is_kg_relevant": "yes",
                    "irrelevant_reason": "",
                }
            else:
                try:
                    analysis = analyze_figure(client, record, figure)
                except Exception as exc:
                    analysis = {"figure_id": figure["figure_id"], "image_paths": figure.get("image_paths", []), "name": figure["figure_id"], "textual_evidence": "", "description": "", "is_kg_relevant": "no", "irrelevant_reason": f"stage1_error: {exc}"}
                    stage1_errors.append({"figure_id": figure["figure_id"], "error": str(exc)})
            figure_analyses.append(analysis)

        analysis_by_id = {item["figure_id"]: item for item in figure_analyses}
        relevant_figures = [fig for fig in selected_figures if analysis_by_id.get(fig["figure_id"], {}).get("is_kg_relevant") == "yes"]

        if client is None:
            figure_enrichments = [
                {
                    "figure_id": figure["figure_id"],
                    "figure_role": "",
                    "figure_role_rationale": "",
                    "visual_elements": [],
                }
                for figure in relevant_figures
            ]
        else:
            for figure in relevant_figures:
                analysis = analysis_by_id[figure["figure_id"]]
                try:
                    figure_enrichments.append(extract_figure_structure(client, record, figure, analysis))
                except Exception as exc:
                    figure_enrichments.append({"figure_id": figure["figure_id"], "figure_role": "", "figure_role_rationale": "", "visual_elements": []})
                    stage2_errors.append({"figure_id": figure["figure_id"], "error": str(exc)})

            enrichment_by_id = {item["figure_id"]: item for item in figure_enrichments}
            for figure in relevant_figures:
                analysis = analysis_by_id[figure["figure_id"]]
                enrichment = enrichment_by_id[figure["figure_id"]]
                try:
                    relation_results.append(extract_figure_relations(client, record, figure, analysis, enrichment, prompt_nodes, candidate_edges, section_exercises))
                except Exception as exc:
                    fallback_elements = []
                    for item in enrichment.get("visual_elements", []) or []:
                        fallback = dict(item)
                        fallback.update({"bbox_2d": None, "bbox_confidence": 0.0, "bbox_rationale": "", "refers_to": []})
                        fallback_elements.append(fallback)
                    relation_results.append({"figure_id": figure["figure_id"], "illustrates": [], "visual_elements": fallback_elements, "supports_edges": [], "required_by_exercises": []})
                    stage3_errors.append({"figure_id": figure["figure_id"], "error": str(exc)})

        nodes, edges = build_incremental_graph_for_section(record, candidate_nodes, candidate_edges, figure_analyses, figure_enrichments, relation_results, record["book_prefix"])
        linked_figure_count = sum(1 for node in nodes if node.get("label") == "Figure")
        report_item = {
            "section_id": record["section_id"],
            "section_title": record["section_title"],
            "figure_count": len(record.get("figures", [])),
            "analyzed_figures": len(selected_figures),
            "kg_relevant_figures": sum(1 for item in figure_analyses if item.get("is_kg_relevant") == "yes"),
            "linked_figures": linked_figure_count,
            "candidate_node_count": len(candidate_nodes),
            "candidate_edge_count": len(candidate_edges),
            "book_exercise_candidate_count": len(section_exercises),
            "vlm_enabled": client is not None,
            "stage1_error_count": len(stage1_errors),
            "stage2_error_count": len(stage2_errors),
            "stage3_error_count": len(stage3_errors),
            "stage1_errors": stage1_errors,
            "stage2_errors": stage2_errors,
            "stage3_errors": stage3_errors,
        }
        section_result = build_section_result(record, nodes, edges, report_item, figure_analyses, figure_enrichments, relation_results)
        section_output = partial_output_path(partials_dir, record["section_id"])
        write_json(section_output, section_result)
        processed_this_run += 1
        print(f"Saved section {record['section_id']}")

    figure_nodes, figure_edges, extraction_report = collect_partial_results(partials_dir)
    graph = assemble_book_graph(book_graph, exercise_by_section, figure_nodes, figure_edges)
    graph["metadata"] = {
        "pipeline_version": PIPELINE_VERSION,
        "book_prefix": book_prefix,
        "source_aligned_sections": str(aligned_path),
        "source_book_graph": str(book_graph_path),
        "source_exercises": str(exercises_path) if exercises_path.exists() else None,
        "dry_run": args.dry_run,
        "model": None if args.dry_run else args.model,
        "sections_processed_this_run": processed_this_run,
        "sections_with_figures": len(extraction_report),
        "node_count": len(graph["nodes"]),
        "edge_count": len(graph["edges"]),
        "thresholds": {"bbox": BBOX_CONFIDENCE_THRESHOLD, **EDGE_CONFIDENCE_THRESHOLDS},
        "generated_at_unix": int(time.time()),
    }
    graph["report"] = extraction_report
    write_json(output_path, graph)

    dangling = dangling_edges(graph)
    print(f"Wrote {output_path}")
    print(f"  sections: {processed_this_run} processed this run, {len(extraction_report)} with figures")
    print(f"  nodes: {len(graph['nodes'])} | edges: {len(graph['edges'])}")
    if dangling:
        print(f"  WARNING: {len(dangling)} edge(s) point outside this graph")
    else:
        print("  all edges resolve to nodes in this graph")


if __name__ == "__main__":
    main()
