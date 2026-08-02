#!/usr/bin/env python3
"""Prepare graph JSON for Neo4j retrieval and produce baseline reports."""

from __future__ import annotations

import argparse
import sys
from collections import Counter, defaultdict
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from utils.bootstrap import ensure_src_on_path

ensure_src_on_path(__file__)

from retrieval.embeddings import make_embedder  # noqa: E402
from retrieval.settings import RetrievalSettings  # noqa: E402
from retrieval.text import (  # noqa: E402
    build_search_text,
    clean_list,
    normalize_math_text,
)
from utils.config import load_config  # noqa: E402
from utils.io import read_json, write_json  # noqa: E402

BOOK_CODE_META = {
    "1a": ("1", "上册"),
    "1b": ("1", "下册"),
    "2a": ("2", "上册"),
    "2b": ("2", "下册"),
    "3a": ("3", "上册"),
    "3b": ("3", "下册"),
    "4a": ("4", "上册"),
    "4b": ("4", "下册"),
    "5a": ("5", "上册"),
    "5b": ("5", "下册"),
    "6a": ("6", "上册"),
    "6b": ("6", "下册"),
}

EDITION_NAMES = {
    "rjb": "人教版",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=None)
    parser.add_argument("--input", default=None, help="Graph JSON file, or directory containing *.json graphs")
    parser.add_argument("--output", default="data/retrieval/graph_enriched.json")
    parser.add_argument("--baseline-output", default="data/retrieval/baseline_report.json")
    parser.add_argument("--embed", action="store_true", help="Generate vectors into *_embedding properties")
    parser.add_argument("--offline-hash-embeddings", action="store_true", help="Use deterministic hash vectors instead of fastembed")
    parser.add_argument("--subject", default=None, help="Keep only one subject, for example 数学")
    parser.add_argument("--stage", default=None, help="Keep only one stage, for example 小学")
    parser.add_argument("--grade", action="append", default=None, help="Keep a grade number; repeat for multiple grades")
    parser.add_argument("--semester", action="append", default=None, help="Keep a semester; repeat for multiple semesters")
    parser.add_argument("--edition", default=None, help="Keep one textbook edition")
    parser.add_argument("--book-id", action="append", default=None, help="Keep a book ID; repeat for multiple books")
    return parser.parse_args()


def graph_paths(input_path: Optional[str]) -> List[Path]:
    if input_path:
        p = Path(input_path)
        if p.is_dir():
            return sorted(p.glob("*.json"))
        return [p]
    config = load_config()
    candidates = [
        config.global_kg_dir / "nodes.json",
        config.subject_stage_kg_dir / "math_primaryschool.json",
        Path("demo/kg/math_7a_rjb.json"),
    ]
    return [p for p in candidates if p.exists()]


def load_graphs(paths: Iterable[Path]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    nodes: List[Dict[str, Any]] = []
    edges: List[Dict[str, Any]] = []
    for path in paths:
        payload = read_json(path)
        if isinstance(payload, dict):
            nodes.extend(payload.get("nodes", []))
            edges.extend(payload.get("edges", []))
        elif isinstance(payload, list):
            if path.name.startswith("node"):
                nodes.extend(payload)
            else:
                edges.extend(payload)
    return nodes, edges


def metadata_from_id(node_id: str) -> Dict[str, str]:
    parts = str(node_id or "").split("_")
    if len(parts) < 3:
        return {}
    subject_code, book_code, edition = parts[0], parts[1], parts[2]
    subject = {"math": "数学", "physics": "物理", "chemistry": "化学", "biology": "生物"}.get(subject_code, subject_code)
    grade, semester = BOOK_CODE_META.get(book_code, ("", ""))
    stage = "小学" if book_code in BOOK_CODE_META else ""
    return {
        "subject": subject,
        "stage": stage,
        "grade": grade,
        "semester": semester,
        "edition": EDITION_NAMES.get(edition, edition),
        "book_id": "_".join(parts[:3]),
    }


def normalize_node(node: Dict[str, Any]) -> Dict[str, Any]:
    out = deepcopy(node)
    props = dict(out.get("properties", {}) if isinstance(out.get("properties"), dict) else {})
    out["name"] = normalize_math_text(out.get("name"))
    props["search_text"] = build_search_text(out, student_safe=True)
    props["teacher_search_text"] = build_search_text(out, student_safe=False)
    if "aliases" in props:
        props["aliases"] = clean_list(props["aliases"])
    for key, value in metadata_from_id(str(out.get("id", ""))).items():
        if value:
            props[key] = value
    out["properties"] = props
    return out


def filter_graph(
    nodes: Iterable[Dict[str, Any]],
    edges: Iterable[Dict[str, Any]],
    *,
    subject: Optional[str] = None,
    stage: Optional[str] = None,
    grades: Optional[Iterable[str]] = None,
    semesters: Optional[Iterable[str]] = None,
    edition: Optional[str] = None,
    book_ids: Optional[Iterable[str]] = None,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Select a curriculum scope while preserving only fully valid edges."""

    grade_set = {str(value) for value in grades or []}
    semester_set = {str(value) for value in semesters or []}
    book_id_set = {str(value) for value in book_ids or []}

    def selected(node: Dict[str, Any]) -> bool:
        props = node.get("properties", {}) if isinstance(node.get("properties"), dict) else {}
        inferred = metadata_from_id(str(node.get("id", "")))
        scope = {
            "subject": inferred.get("subject") or props.get("subject"),
            "stage": inferred.get("stage") or props.get("stage"),
            "grade": inferred.get("grade") or props.get("grade"),
            "semester": inferred.get("semester") or props.get("semester"),
            "edition": inferred.get("edition") or props.get("edition") or props.get("publisher"),
            "book_id": inferred.get("book_id") or props.get("book_id"),
        }
        return (
            (subject is None or scope["subject"] == subject)
            and (stage is None or scope["stage"] == stage)
            and (not grade_set or str(scope["grade"]) in grade_set)
            and (not semester_set or str(scope["semester"]) in semester_set)
            and (edition is None or scope["edition"] == edition)
            and (not book_id_set or str(scope["book_id"]) in book_id_set)
        )

    selected_nodes = [deepcopy(node) for node in nodes if selected(node)]
    selected_ids = {str(node.get("id", "")) for node in selected_nodes}
    selected_edges: List[Dict[str, Any]] = []
    for edge in edges:
        source = str(edge.get("source", ""))
        if source not in selected_ids:
            continue
        targets = edge.get("target_name_to_ids")
        if isinstance(targets, list):
            kept_targets = [
                deepcopy(target)
                for target in targets
                if isinstance(target, dict) and str(target.get("target", "")) in selected_ids
            ]
            if kept_targets:
                item = deepcopy(edge)
                item["target_name_to_ids"] = kept_targets
                selected_edges.append(item)
            continue
        if str(edge.get("target", "")) in selected_ids:
            selected_edges.append(deepcopy(edge))
    return selected_nodes, selected_edges


def expand_edges(edges: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Expand aggregated exercise edges into ordinary source/target edges."""

    expanded: List[Dict[str, Any]] = []
    for edge in edges:
        targets = edge.get("target_name_to_ids")
        if not isinstance(targets, list):
            expanded.append(deepcopy(edge))
            continue
        for target in targets:
            if not isinstance(target, dict) or not target.get("target"):
                continue
            item = deepcopy(edge)
            item.pop("target_name_to_ids", None)
            item["target"] = target["target"]
            item["target_name"] = target.get("target_name")
            expanded.append(item)
    return expanded


def apply_location_metadata(nodes: List[Dict[str, Any]], edges: Iterable[Dict[str, Any]]) -> None:
    section_ids_by_source: Dict[str, List[str]] = defaultdict(list)
    chapter_ids_by_source: Dict[str, List[str]] = defaultdict(list)
    labels_by_id = {
        str(node.get("id", "")): str(node.get("label", ""))
        for node in nodes
    }
    for edge in edges:
        if edge.get("type") != "appears_in":
            continue
        source = str(edge.get("source", ""))
        target = str(edge.get("target", ""))
        target_label = labels_by_id.get(target, "")
        is_section = target_label == "Section" or "_ch" in target and "_s" in target.rsplit("_ch", 1)[-1]
        is_chapter = target_label == "Chapter" or target.rsplit("_", 1)[-1].startswith("ch")
        if source and target and is_section and target not in section_ids_by_source[source]:
            section_ids_by_source[source].append(target)
        elif source and target and is_chapter and target not in chapter_ids_by_source[source]:
            chapter_ids_by_source[source].append(target)

    for node in nodes:
        node_id = str(node.get("id", ""))
        section_ids = section_ids_by_source.get(node_id, [])
        chapter_ids = list(chapter_ids_by_source.get(node_id, []))
        props = node.setdefault("properties", {})
        if section_ids:
            props["section_ids"] = section_ids
            props.setdefault("section_id", section_ids[0])
            for section_id in section_ids:
                chapter_id = section_id.rsplit("_s", 1)[0]
                if chapter_id and chapter_id not in chapter_ids:
                    chapter_ids.append(chapter_id)
        if chapter_ids:
            props["chapter_ids"] = chapter_ids
            props.setdefault("chapter_id", chapter_ids[0])


def enrich_graph(nodes: List[Dict[str, Any]], edges: List[Dict[str, Any]], *, embed: bool, settings: RetrievalSettings, offline_hash: bool) -> Dict[str, Any]:
    out_nodes = [normalize_node(node) for node in nodes]
    out_edges = expand_edges(edges)
    apply_location_metadata(out_nodes, out_edges)
    if embed:
        embedder = make_embedder(settings.embedding_model, settings.embedding_dimension, offline_hash=offline_hash)
        for label in ("Concept", "Skill", "Exercise"):
            selected = [node for node in out_nodes if node.get("label") == label]
            vectors = embedder.embed([node.get("properties", {}).get("search_text", "") for node in selected])
            for node, vector in zip(selected, vectors, strict=True):
                node.setdefault("properties", {})["embedding"] = vector
    return {"nodes": out_nodes, "edges": out_edges}


def baseline_report(nodes: List[Dict[str, Any]], edges: List[Dict[str, Any]]) -> Dict[str, Any]:
    label_counts = Counter(str(node.get("label", "")) for node in nodes)
    edge_counts = Counter(str(edge.get("type", "")) for edge in edges)
    required_by_label = {
        "Concept": ["name", "definition", "search_text", "subject", "stage", "grade", "semester", "edition", "book_id"],
        "Skill": ["name", "description", "search_text", "subject", "stage", "grade", "semester", "edition", "book_id"],
        "Exercise": ["name", "stem", "search_text", "subject", "stage", "grade", "semester", "edition", "book_id"],
    }
    completeness: Dict[str, Dict[str, float]] = defaultdict(dict)
    for label, fields in required_by_label.items():
        selected = [node for node in nodes if node.get("label") == label]
        denom = len(selected) or 1
        for field in fields:
            present = 0
            for node in selected:
                props = node.get("properties", {}) if isinstance(node.get("properties"), dict) else {}
                value = node.get(field) if field == "name" else props.get(field)
                if value not in (None, "", [], {}):
                    present += 1
            completeness[label][field] = round(present / denom, 4)
    duplicate_ids = [node_id for node_id, count in Counter(str(node.get("id", "")) for node in nodes).items() if count > 1]
    return {
        "node_counts": dict(label_counts),
        "edge_counts": dict(edge_counts),
        "node_total": len(nodes),
        "edge_total": len(edges),
        "duplicate_node_ids": duplicate_ids,
        "property_completeness": dict(completeness),
        "textbook_coverage": {
            "nodes_with_book_id": sum(1 for node in nodes if node.get("properties", {}).get("book_id")),
            "nodes_with_section_id": sum(1 for node in nodes if node.get("properties", {}).get("section_id")),
        },
    }


def main() -> None:
    args = parse_args()
    settings = RetrievalSettings.from_env()
    nodes, edges = load_graphs(graph_paths(args.input))
    nodes, edges = filter_graph(
        nodes,
        edges,
        subject=args.subject,
        stage=args.stage,
        grades=args.grade,
        semesters=args.semester,
        edition=args.edition,
        book_ids=args.book_id,
    )
    enriched = enrich_graph(nodes, edges, embed=args.embed, settings=settings, offline_hash=args.offline_hash_embeddings)
    write_json(args.output, enriched)
    write_json(args.baseline_output, baseline_report(enriched["nodes"], enriched["edges"]))


if __name__ == "__main__":
    main()
