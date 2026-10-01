#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Step 3: merge chapter-level KGs into book, stage, subject, and global graphs.

Aggregates nodes/edges produced by chapter extraction, applies dedupe/normalization
rules from pipeline config, and writes merged ``nodes.json`` / ``edges.json`` trees.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from copy import deepcopy
from pathlib import Path
import re
import sys
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

_SRC_DIR = Path(__file__).resolve().parents[1]
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from utils.config import PipelineConfig, load_config
from utils.k12_ids import BOOK_CODE_ORDER_INDEX, BOOK_PREFIX_RE
from utils.io import read_json, write_json

SUBJECT_MAP = {
    "数学": "math",
    "物理": "physics",
    "化学": "chemistry",
    "生物学": "biology",
}

STAGE_MAP = {
    "小学": "primaryschool",
    "初中": "middleschool",
    "高中": "highschool",
}

KNOWLEDGE_EDGE_TYPES = {"relates_to", "prerequisites_for", "is_a", "verifies", "tests_concept", "tests_skill"}
AGGREGATED_EDGE_TYPES = {"tests_concept", "tests_skill"}
NODE_TYPE_CODE = {
    "Concept": "cpt",
    "Skill": "skl",
    "Experiment": "exp",
    "Exercise": "exe",
}

STAGE_SEQUENCE = ["primaryschool", "middleschool", "highschool"]
STAGE_SEQUENCE_INDEX = {stage: idx for idx, stage in enumerate(STAGE_SEQUENCE)}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Merge chapter KGs into higher-level outputs")
    parser.add_argument("--config", default=None, help="Pipeline config path")
    parser.add_argument("--stage", choices=["book", "subject_stage", "subject", "global"], default=None, help="Only run one stage")
    parser.add_argument("--filter-prefix", action="append", default=None, help="Only process specific book_prefix values at book stage")
    parser.add_argument("--dry-run", action="store_true", help="Print actions without writing files")
    return parser.parse_args()


def normalize_name(text: str) -> str:
    return " ".join(str(text or "").strip().lower().split())


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def parse_book_code(value: str) -> str:
    match = BOOK_PREFIX_RE.match(str(value or "").strip())
    if not match:
        return ""
    return match.group("book_code")


def book_sequence_key(book_prefix: str) -> Tuple[int, str]:
    code = parse_book_code(book_prefix)
    return (BOOK_CODE_ORDER_INDEX.get(code, 10**9), str(book_prefix))


def load_graph(path: Path) -> Dict[str, Any]:
    data = read_json(path)
    if not isinstance(data, dict):
        raise ValueError(f"invalid graph file: {path}")
    return data


def merge_properties(base: Dict[str, Any], other: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(base)
    for key, value in other.items():
        if key not in out or out[key] in (None, "", [], {}):
            out[key] = value
            continue
        if key in {"examples", "aliases"} and isinstance(out[key], list) and isinstance(value, list):
            merged = []
            seen = set()
            for item in out[key] + value:
                token = repr(item)
                if token not in seen:
                    seen.add(token)
                    merged.append(item)
            out[key] = merged
            continue
        if key in {"definition", "description"} and isinstance(out[key], str) and isinstance(value, str):
            if len(value) > len(out[key]):
                out[key] = value
    return out


def dedup_nodes(nodes: Sequence[Dict[str, Any]], dedup_labels: Sequence[str]) -> Tuple[List[Dict[str, Any]], Dict[str, str]]:
    dedup_label_set = set(dedup_labels)
    grouped: Dict[Tuple[str, str], List[Dict[str, Any]]] = defaultdict(list)
    passthrough: List[Dict[str, Any]] = []

    for node in nodes:
        label = str(node.get("label", "")).strip()
        name = str(node.get("name", "")).strip()
        if label in dedup_label_set and name:
            grouped[(label, normalize_name(name))].append(deepcopy(node))
        else:
            passthrough.append(deepcopy(node))

    output: List[Dict[str, Any]] = []
    id_mapping: Dict[str, str] = {}

    for group in grouped.values():
        canonical = group[0]
        canonical_id = canonical["id"]
        props = canonical.get("properties", {})
        for item in group[1:]:
            id_mapping[item["id"]] = canonical_id
            props = merge_properties(props, item.get("properties", {}))
        if props:
            canonical["properties"] = props
        output.append(canonical)
        id_mapping[canonical_id] = canonical_id

    for node in passthrough:
        output.append(node)
        if isinstance(node.get("id"), str):
            id_mapping[node["id"]] = node["id"]

    return output, id_mapping


def node_id_prefix(node_id: str) -> str:
    token = str(node_id)
    if "::" in token:
        token = token.split("::", 1)[1]
    parts = token.split("_")
    if len(parts) <= 1:
        return token
    return "_".join(parts[:-1])


def reindex_nodes(nodes: Sequence[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], Dict[str, str]]:
    counters: Dict[Tuple[str, str], int] = defaultdict(int)
    output: List[Dict[str, Any]] = []
    id_mapping: Dict[str, str] = {}

    for node in nodes:
        copied = deepcopy(node)
        old_id = str(copied.get("id", ""))
        label = str(copied.get("label", "")).strip()
        type_code = NODE_TYPE_CODE.get(label)
        if not old_id or not type_code:
            output.append(copied)
            if old_id:
                id_mapping[old_id] = old_id
            continue

        prefix = node_id_prefix(old_id)
        counters[(prefix, type_code)] += 1
        new_id = f"{prefix}_{type_code}{counters[(prefix, type_code)]}"
        copied["id"] = new_id
        output.append(copied)
        id_mapping[old_id] = new_id

    return output, id_mapping


def node_lookup(nodes: Sequence[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    return {str(node["id"]): node for node in nodes if isinstance(node.get("id"), str)}


def subject_from_node_id(node_id: str) -> str:
    return str(node_id).split("_", 1)[0]


def load_existing_global_leads_to(config: PipelineConfig) -> List[Dict[str, Any]]:
    path = config.global_kg_dir / "edges.json"
    if not path.exists():
        return []
    data = read_json(path)
    if not isinstance(data, list):
        return []
    return [edge for edge in data if isinstance(edge, dict) and str(edge.get("type", "")).strip() == "leads_to"]


def append_subject_leads_to_from_existing_global(
    edges: List[Dict[str, Any]],
    nodes_by_id: Dict[str, Dict[str, Any]],
    config: PipelineConfig,
    subject_en: str,
) -> None:
    seen = {(str(edge.get("source", "")), str(edge.get("target", "")), str(edge.get("type", ""))) for edge in edges}
    for edge in load_existing_global_leads_to(config):
        source = str(edge.get("source", "")).strip()
        target = str(edge.get("target", "")).strip()
        if subject_from_node_id(source) != subject_en or subject_from_node_id(target) != subject_en:
            continue
        if source not in nodes_by_id or target not in nodes_by_id or source == target:
            continue
        key = (source, target, "leads_to")
        if key in seen:
            continue
        seen.add(key)
        edges.append(
            {
                "source": source,
                "source_name": nodes_by_id[source]["name"],
                "target": target,
                "target_name": nodes_by_id[target]["name"],
                "type": "leads_to",
            }
        )


def append_cross_subject_leads_to_from_existing_global(
    slim_edges: List[Dict[str, str]],
    valid_node_ids: set[str],
    config: PipelineConfig,
) -> None:
    seen = {(str(edge.get("source", "")), str(edge.get("target", "")), str(edge.get("type", ""))) for edge in slim_edges}
    for edge in load_existing_global_leads_to(config):
        source = str(edge.get("source", "")).strip()
        target = str(edge.get("target", "")).strip()
        if subject_from_node_id(source) == subject_from_node_id(target):
            continue
        if source not in valid_node_ids or target not in valid_node_ids or source == target:
            continue
        key = (source, target, "leads_to")
        if key in seen:
            continue
        seen.add(key)
        slim_edges.append({"source": source, "target": target, "type": "leads_to"})


def rebuild_edges(
    edges: Sequence[Dict[str, Any]],
    id_mapping: Dict[str, str],
    nodes_by_id: Dict[str, Dict[str, Any]],
) -> List[Dict[str, Any]]:
    standard: List[Dict[str, Any]] = []
    aggregated: Dict[Tuple[str, str], Dict[str, Any]] = {}
    seen_standard: set[Tuple[str, str, str]] = set()

    for edge in edges:
        edge_type = str(edge.get("type", "")).strip()
        if not edge_type:
            continue

        source = id_mapping.get(str(edge.get("source", "")).strip(), str(edge.get("source", "")).strip())
        if source not in nodes_by_id:
            continue
        source_node = nodes_by_id[source]

        if edge_type in AGGREGATED_EDGE_TYPES:
            key = (source, edge_type)
            bucket = aggregated.setdefault(
                key,
                {
                    "source": source,
                    "source_stem": source_node.get("properties", {}).get("stem", source_node.get("name", "")),
                    "type": edge_type,
                    "target_name_to_ids": [],
                },
            )
            if "properties" in edge and "properties" not in bucket:
                bucket["properties"] = deepcopy(edge["properties"])
            seen_targets = {(item["target"], item["target_name"]) for item in bucket["target_name_to_ids"]}
            for item in edge.get("target_name_to_ids", []):
                if not isinstance(item, dict):
                    continue
                target = id_mapping.get(str(item.get("target", "")).strip(), str(item.get("target", "")).strip())
                if target not in nodes_by_id:
                    continue
                pair = (target, nodes_by_id[target]["name"])
                if pair in seen_targets:
                    continue
                seen_targets.add(pair)
                bucket["target_name_to_ids"].append({"target": target, "target_name": nodes_by_id[target]["name"]})
            continue

        target = id_mapping.get(str(edge.get("target", "")).strip(), str(edge.get("target", "")).strip())
        if target not in nodes_by_id or source == target:
            continue
        key = (source, target, edge_type)
        if key in seen_standard:
            continue
        seen_standard.add(key)
        if source_node["label"] == "Exercise":
            rebuilt = {
                "source": source,
                "source_stem": source_node.get("properties", {}).get("stem", source_node["name"]),
                "target": target,
                "target_name": nodes_by_id[target]["name"],
                "type": edge_type,
            }
        else:
            rebuilt = {
                "source": source,
                "source_name": source_node["name"],
                "target": target,
                "target_name": nodes_by_id[target]["name"],
                "type": edge_type,
            }
        if "properties" in edge:
            rebuilt["properties"] = deepcopy(edge["properties"])
        standard.append(rebuilt)

    standard.extend(aggregated.values())
    return standard


def build_structure(
    book: Dict[str, Any],
    sections_index: Dict[str, Any],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Dict[str, str]]:
    book_prefix = str(book["book_prefix"])
    nodes = [
        {
            "id": book_prefix,
            "label": "Book",
            "name": str(book["grade"]),
            "properties": {
                "subject": str(book["subject"]),
                "grade": str(book["grade"]),
                "publisher": str(book["publisher"]),
            },
        }
    ]
    edges: List[Dict[str, Any]] = []
    chapter_titles: Dict[str, str] = {}
    section_node_by_file: Dict[str, str] = {}

    sections = sections_index.get("sections", [])
    if not isinstance(sections, list):
        return nodes, edges, section_node_by_file

    normalized_sections: List[Dict[str, str]] = []
    for item in sections:
        if not isinstance(item, dict):
            continue
        normalized_sections.append(
            {
                "chapter_num": clean_text(item.get("chapter_num", item.get("chapter", ""))),
                "chapter_title": clean_text(item.get("chapter_title", "")),
                "section_num": clean_text(item.get("section_num", item.get("section", ""))),
                "section_title": clean_text(item.get("section_title", "")),
                "file": clean_text(item.get("file", "")),
            }
        )
    normalized_sections.sort(
        key=lambda item: (
            _numeric_order_key(item["chapter_num"]),
            0 if not item["section_num"] else 1,
            _numeric_order_key(item["section_num"]),
            item["file"],
        )
    )

    seen_sections: set[str] = set()
    for item in normalized_sections:
        chapter_num = item["chapter_num"]
        chapter_title = item["chapter_title"]
        section_num = item["section_num"]
        section_title = item["section_title"]
        file_name = item["file"]
        if chapter_num:
            chapter_titles.setdefault(chapter_num, chapter_title)
        if not chapter_num:
            continue
        if not section_num:
            if file_name:
                section_node_by_file[Path(file_name).stem] = f"{book_prefix}_ch{chapter_num}"
            continue
        chapter_id = f"{book_prefix}_ch{chapter_num}"
        section_id = f"{book_prefix}_ch{chapter_num}_s{section_num}"
        if section_id in seen_sections:
            continue
        seen_sections.add(section_id)
        section_node_by_file[Path(file_name).stem] = section_id
        nodes.append({"id": section_id, "label": "Section", "name": section_title})
        edges.append(
            {
                "source": section_id,
                "source_name": section_title,
                "target": chapter_id,
                "target_name": chapter_titles.get(chapter_num, chapter_title),
                "type": "is_part_of",
            }
        )

    for chapter_num, chapter_title in sorted(chapter_titles.items(), key=lambda item: _numeric_order_key(item[0])):
        chapter_id = f"{book_prefix}_ch{chapter_num}"
        nodes.append({"id": chapter_id, "label": "Chapter", "name": chapter_title})
        edges.append(
            {
                "source": chapter_id,
                "source_name": chapter_title,
                "target": book_prefix,
                "target_name": str(book["grade"]),
                "type": "is_part_of",
            }
        )

    return nodes, edges, section_node_by_file


_CHAPTER_FILE_RE = re.compile(r"^ch(?P<chapter>\d+)(?:_s(?P<section>\d+))?")
_MIXED_NUMERIC_RE = re.compile(r"^(?P<prefix>\D*?)(?P<number>\d+)(?P<suffix>\D*)$")


def _numeric_order_key(value: str) -> Tuple[int, str]:
    token = str(value or "").strip()
    if not token:
        return (10**9, token)
    if token.isdigit():
        return (int(token), token)
    match = _MIXED_NUMERIC_RE.match(token)
    if match:
        prefix = match.group("prefix")
        number = int(match.group("number"))
        suffix = match.group("suffix")
        return (10**9 + number, f"{prefix}\0{number:09d}\0{suffix}\0{token}")
    return (2 * 10**9, token)


def load_book_chapter_entries(chapter_dir: Path) -> List[Dict[str, Any]]:
    entries: List[Dict[str, Any]] = []
    for path in chapter_dir.glob("*.json"):
        chapter = load_graph(path)
        info = chapter.get("section_info", {})
        if not isinstance(info, dict):
            info = {}
        match = _CHAPTER_FILE_RE.match(path.stem)
        chapter_num = clean_text(info.get("chapter", (match.group("chapter") if match else "")))
        section_num = clean_text(info.get("section", (match.group("section") if match and match.group("section") else "")))
        entries.append(
            {
                "path": path,
                "graph": chapter,
                "chapter_num": chapter_num,
                "chapter_title": clean_text(info.get("chapter_title", "")),
                "section_num": section_num,
                "section_title": clean_text(info.get("section_title", "")),
            }
        )
    entries.sort(
        key=lambda item: (
            _numeric_order_key(item["chapter_num"]),
            0 if not item["section_num"] else 1,
            _numeric_order_key(item["section_num"]),
            item["path"].name,
        )
    )
    return entries


def scope_local_id(file_stem: str, node_id: str) -> str:
    return f"{file_stem}::{node_id}"


def scope_book_entry_graph(entry: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Dict[str, str]]:
    file_stem = entry["path"].stem
    graph = entry["graph"]

    local_to_scoped: Dict[str, str] = {}
    scoped_nodes: List[Dict[str, Any]] = []
    source_file_by_scoped_id: Dict[str, str] = {}

    for node in graph.get("nodes", []):
        if not isinstance(node, dict) or not isinstance(node.get("id"), str):
            continue
        local_id = str(node["id"])
        scoped_id = scope_local_id(file_stem, local_id)
        local_to_scoped[local_id] = scoped_id
        copied = deepcopy(node)
        copied["id"] = scoped_id
        scoped_nodes.append(copied)
        source_file_by_scoped_id[scoped_id] = file_stem

    scoped_edges: List[Dict[str, Any]] = []
    for edge in graph.get("edges", []):
        if not isinstance(edge, dict):
            continue
        copied = deepcopy(edge)
        source = str(copied.get("source", "")).strip()
        if source in local_to_scoped:
            copied["source"] = local_to_scoped[source]
        target = str(copied.get("target", "")).strip()
        if target in local_to_scoped:
            copied["target"] = local_to_scoped[target]
        if isinstance(copied.get("target_name_to_ids"), list):
            remapped_items: List[Dict[str, Any]] = []
            for item in copied["target_name_to_ids"]:
                if not isinstance(item, dict):
                    continue
                remapped = deepcopy(item)
                target_id = str(remapped.get("target", "")).strip()
                if target_id in local_to_scoped:
                    remapped["target"] = local_to_scoped[target_id]
                remapped_items.append(remapped)
            copied["target_name_to_ids"] = remapped_items
        scoped_edges.append(copied)

    return scoped_nodes, scoped_edges, source_file_by_scoped_id


def build_appears_in_edges(
    source_file_by_old_id: Dict[str, str],
    id_mapping: Dict[str, str],
    nodes_by_id: Dict[str, Dict[str, Any]],
    section_node_by_file: Dict[str, str],
) -> List[Dict[str, Any]]:
    seen: set[Tuple[str, str]] = set()
    edges: List[Dict[str, Any]] = []
    for old_id, file_stem in source_file_by_old_id.items():
        source = id_mapping.get(old_id, old_id)
        target = section_node_by_file.get(file_stem)
        if not target or source not in nodes_by_id:
            continue
        key = (source, target)
        if key in seen:
            continue
        seen.add(key)
        node = nodes_by_id[source]
        if node["label"] == "Exercise":
            edge = {
                "source": source,
                "source_stem": node.get("properties", {}).get("stem", node["name"]),
                "target": target,
                "target_name": nodes_by_id[target]["name"],
                "type": "appears_in",
            }
        else:
            edge = {
                "source": source,
                "source_name": node["name"],
                "target": target,
                "target_name": nodes_by_id[target]["name"],
                "type": "appears_in",
            }
        edges.append(edge)
    return edges


def merge_book(book: Dict[str, Any], config: PipelineConfig, dry_run: bool) -> Optional[Dict[str, Any]]:
    book_prefix = str(book["book_prefix"])
    chapter_dir = config.chapter_kg_dir / book_prefix
    if not chapter_dir.exists():
        return None

    chapter_entries = load_book_chapter_entries(chapter_dir)
    if not chapter_entries:
        return None

    sections_index_path = config.sections_index_for(book)
    if sections_index_path.exists():
        sections_index = load_graph(sections_index_path)
    else:
        sections_index = {
            "sections": [
                {
                    "chapter_num": entry["chapter_num"],
                    "chapter_title": entry["chapter_title"],
                    "section_num": entry["section_num"],
                    "section_title": entry["section_title"],
                    "file": entry["path"].name,
                }
                for entry in chapter_entries
            ]
        }
    structure_nodes, structure_edges, section_node_by_file = build_structure(book, sections_index)

    raw_nodes: List[Dict[str, Any]] = []
    raw_edges: List[Dict[str, Any]] = []
    source_file_by_old_id: Dict[str, str] = {}
    for entry in chapter_entries:
        scoped_nodes, scoped_edges, scoped_sources = scope_book_entry_graph(entry)
        raw_nodes.extend(scoped_nodes)
        raw_edges.extend(scoped_edges)
        source_file_by_old_id.update(scoped_sources)

    deduped_nodes, dedup_mapping = dedup_nodes(raw_nodes, config.merge_dedup_labels)
    reindexed_nodes, reindex_mapping = reindex_nodes(deduped_nodes)
    id_mapping = {old_id: reindex_mapping.get(canonical_id, canonical_id) for old_id, canonical_id in dedup_mapping.items()}
    all_nodes = reindexed_nodes + structure_nodes
    nodes_by_id = node_lookup(all_nodes)
    all_edges = rebuild_edges(raw_edges, id_mapping, nodes_by_id)
    all_edges.extend(structure_edges)
    all_edges.extend(build_appears_in_edges(source_file_by_old_id, id_mapping, nodes_by_id, section_node_by_file))

    payload = {"nodes": all_nodes, "edges": all_edges}
    if not dry_run:
        write_json(config.book_kg_dir / f"{book_prefix}.json", payload)
    return payload


def group_books(books: Sequence[Dict[str, Any]]) -> Dict[Tuple[str, str], List[Dict[str, Any]]]:
    grouped: Dict[Tuple[str, str], List[Dict[str, Any]]] = defaultdict(list)
    for book in books:
        subject_en = SUBJECT_MAP[str(book["subject"])]
        stage_en = STAGE_MAP[str(book["stage"])]
        grouped[(subject_en, stage_en)].append(book)
    for key in grouped:
        grouped[key].sort(key=lambda book: book_sequence_key(str(book["book_prefix"])))
    return grouped


def merge_graphs(
    graph_paths: Iterable[Path],
    config: PipelineConfig,
    dry_run: bool,
    output_path: Path,
) -> Optional[Dict[str, Any]]:
    nodes: List[Dict[str, Any]] = []
    edges: List[Dict[str, Any]] = []
    for path in graph_paths:
        graph = load_graph(path)
        nodes.extend(node for node in graph.get("nodes", []) if isinstance(node, dict))
        edges.extend(edge for edge in graph.get("edges", []) if isinstance(edge, dict))

    if not nodes and not edges:
        return None

    deduped_nodes, dedup_mapping = dedup_nodes(nodes, config.merge_dedup_labels)
    reindexed_nodes, reindex_mapping = reindex_nodes(deduped_nodes)
    id_mapping = {old_id: reindex_mapping.get(canonical_id, canonical_id) for old_id, canonical_id in dedup_mapping.items()}
    nodes_by_id = node_lookup(reindexed_nodes)
    rebuilt_edges = rebuild_edges(edges, id_mapping, nodes_by_id)
    if output_path.parent == config.subject_kg_dir:
        append_subject_leads_to_from_existing_global(rebuilt_edges, nodes_by_id, config, output_path.stem)
    payload = {"nodes": reindexed_nodes, "edges": rebuilt_edges}
    if not dry_run:
        write_json(output_path, payload)
    return payload


def write_global(config: PipelineConfig, dry_run: bool) -> Optional[Dict[str, Any]]:
    nodes: List[Dict[str, Any]] = []
    edges: List[Dict[str, Any]] = []
    for path in sorted(config.subject_kg_dir.glob("*.json")):
        graph = load_graph(path)
        nodes.extend(node for node in graph.get("nodes", []) if isinstance(node, dict))
        edges.extend(edge for edge in graph.get("edges", []) if isinstance(edge, dict))
    if not nodes and not edges:
        return None

    id_mapping = {
        str(node["id"]): str(node["id"])
        for node in nodes
        if isinstance(node, dict) and isinstance(node.get("id"), str)
    }
    nodes_by_id = node_lookup(nodes)
    rebuilt_edges = rebuild_edges(edges, id_mapping, nodes_by_id)

    slim_nodes = [{"id": node["id"], "label": node["label"], "name": node["name"]} for node in nodes]
    slim_edges: List[Dict[str, str]] = []
    for edge in rebuilt_edges:
        edge_type = str(edge["type"])
        if edge_type in AGGREGATED_EDGE_TYPES:
            for item in edge.get("target_name_to_ids", []):
                if isinstance(item, dict) and isinstance(item.get("target"), str):
                    slim_edges.append({"source": edge["source"], "target": item["target"], "type": edge_type})
            continue
        slim_edges.append({"source": edge["source"], "target": edge["target"], "type": edge_type})
    append_cross_subject_leads_to_from_existing_global(slim_edges, {node["id"] for node in slim_nodes}, config)

    if not dry_run:
        write_json(config.global_kg_dir / "nodes.json", slim_nodes)
        write_json(config.global_kg_dir / "edges.json", slim_edges)
    return {"nodes": slim_nodes, "edges": slim_edges}


def run_all(
    config_path: Optional[str],
    *,
    stage: Optional[str],
    filter_prefixes: Optional[Sequence[str]],
    dry_run: bool,
) -> List[str]:
    config = load_config(config_path)
    books = config.load_books(require_source=False)
    books.sort(key=lambda book: book_sequence_key(str(book["book_prefix"])))
    wanted = {item.strip() for item in (filter_prefixes or []) if item.strip()}
    completed: List[str] = []

    if stage in (None, "book"):
        for book in books:
            if wanted and book["book_prefix"] not in wanted:
                continue
            if merge_book(book, config, dry_run):
                print(f"[OK][book] {book['book_prefix']}")
                completed.append(f"book:{book['book_prefix']}")
        if stage == "book":
            return completed

    if stage in (None, "subject_stage"):
        grouped_books = group_books(books)
        for subject_en, stage_en in sorted(
            grouped_books,
            key=lambda item: (
                item[0],
                STAGE_SEQUENCE_INDEX.get(item[1], 10**9),
            ),
        ):
            group = grouped_books[(subject_en, stage_en)]
            paths = [config.book_kg_dir / f"{book['book_prefix']}.json" for book in group]
            output = config.subject_stage_kg_dir / f"{subject_en}_{stage_en}.json"
            if merge_graphs(paths, config, dry_run, output):
                print(f"[OK][subject_stage] {subject_en}_{stage_en}")
                completed.append(f"subject_stage:{subject_en}_{stage_en}")
        if stage == "subject_stage":
            return completed

    if stage in (None, "subject"):
        grouped: Dict[str, List[Path]] = defaultdict(list)
        for path in config.subject_stage_kg_dir.glob("*.json"):
            subject_en = path.stem.split("_", 1)[0]
            grouped[subject_en].append(path)
        for subject_en in sorted(grouped):
            paths = sorted(
                grouped[subject_en],
                key=lambda path: (
                    STAGE_SEQUENCE_INDEX.get(path.stem.split("_", 1)[1], 10**9),
                    path.name,
                ),
            )
            output = config.subject_kg_dir / f"{subject_en}.json"
            if merge_graphs(paths, config, dry_run, output):
                print(f"[OK][subject] {subject_en}")
                completed.append(f"subject:{subject_en}")
        if stage == "subject":
            return completed

    if write_global(config, dry_run):
        print("[OK][global]")
        completed.append("global")
    return completed


def main() -> None:
    args = parse_args()
    run_all(
        config_path=args.config,
        stage=args.stage,
        filter_prefixes=args.filter_prefix,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    main()
