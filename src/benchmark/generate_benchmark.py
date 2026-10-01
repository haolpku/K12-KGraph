#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Generate layered multiple-choice benchmark candidates from merged KGs.

Reads ``global_kg`` (optionally augmented with ``subject_kg`` node metadata),
scores distractors with embeddings, and writes per-task JSONL plus a summary.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import defaultdict, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, DefaultDict, Dict, Iterable, List, Optional, Sequence, Set, Tuple

import numpy as np
from fastembed import TextEmbedding

_SRC_DIR = Path(__file__).resolve().parents[1]
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from utils.io import read_json, write_json, write_jsonl  # noqa: E402
from utils.k12_ids import (
    BOOK_CODE_ORDER,
    BOOK_CODE_ORDER_INDEX,
    CHAPTER_ID_RE,
    HIGH_SCHOOL_BOOK_CODES,
    MIDDLE_SCHOOL_BOOK_CODES,
    PRIMARY_MATH_BOOK_CODES,
    SECTION_ID_RE,
    TYPE_CODE_PREFIX_RE,
)


BOOK_ORDER = BOOK_CODE_ORDER
BOOK_ORDER_INDEX = BOOK_CODE_ORDER_INDEX

SUBJECT_CN = {
    "math": "数学",
    "physics": "物理",
    "chemistry": "化学",
    "biology": "生物",
}
STAGE_CN = {
    "primaryschool": "小学",
    "middleschool": "初中",
    "highschool": "高中",
}

CONTENT_LABELS = {"Concept", "Skill", "Experiment", "Exercise"}
TASK2_LABELS = {"Concept", "Skill", "Experiment"}
TYPE_CODE_RE = TYPE_CODE_PREFIX_RE


@dataclass(frozen=True)
class NodeInfo:
    node_id: str
    label: str
    name: str
    stem: str = ""
    properties: Dict[str, Any] = field(default_factory=dict)


@dataclass
class LocationInfo:
    sections: Set[str] = field(default_factory=set)
    chapters: Set[str] = field(default_factory=set)
    books: Set[str] = field(default_factory=set)
    book_codes: Set[str] = field(default_factory=set)
    subjects: Set[str] = field(default_factory=set)
    subject_stages: Set[str] = field(default_factory=set)

    @property
    def is_primary_math(self) -> bool:
        return "math" in self.subjects and any(code in PRIMARY_MATH_BOOK_CODES for code in self.book_codes)


@dataclass
class CandidateItem:
    node_id: str
    name: str
    label: str
    sim_score: float
    sim_question: float
    sim_answer: float


class EmbeddingScorer:
    def __init__(self, model_name: str, batch_size: int = 256) -> None:
        self.model_name = model_name
        self.batch_size = batch_size
        self.model = TextEmbedding(model_name=model_name)
        self._cache: Dict[str, np.ndarray] = {}

    def _normalize_text(self, text: str) -> str:
        return clean_name(text)

    def embed_texts(self, texts: Iterable[str]) -> None:
        missing: List[str] = []
        seen: Set[str] = set()
        for text in texts:
            normalized = self._normalize_text(text)
            if not normalized or normalized in self._cache or normalized in seen:
                continue
            seen.add(normalized)
            missing.append(normalized)
        if not missing:
            return
        vectors = list(self.model.embed(missing, batch_size=self.batch_size))
        for text, vector in zip(missing, vectors):
            self._cache[text] = np.asarray(vector, dtype=np.float32)

    def vector(self, text: str) -> np.ndarray:
        normalized = self._normalize_text(text)
        if normalized not in self._cache:
            self.embed_texts([normalized])
        return self._cache.get(normalized, np.zeros(1, dtype=np.float32))

    @staticmethod
    def normalized_cosine(vec_a: np.ndarray, vec_b: np.ndarray) -> float:
        if vec_a.size == 0 or vec_b.size == 0:
            return 0.0
        denom = float(np.linalg.norm(vec_a) * np.linalg.norm(vec_b))
        if denom == 0.0:
            return 0.0
        cosine = float(np.dot(vec_a, vec_b) / denom)
        cosine = max(-1.0, min(1.0, cosine))
        return (cosine + 1.0) / 2.0


def load_json_list(path: Path) -> List[Dict[str, Any]]:
    data = read_json(path)
    if not isinstance(data, list):
        raise ValueError(f"{path} must be a JSON list")
    return [item for item in data if isinstance(item, dict)]


def load_subject_node_records(subject_kg_dir: Path) -> List[Dict[str, Any]]:
    node_records: List[Dict[str, Any]] = []
    for path in sorted(subject_kg_dir.glob("*.json")):
        payload = read_json(path)
        if not isinstance(payload, dict):
            continue
        nodes = payload.get("nodes", [])
        if not isinstance(nodes, list):
            continue
        for item in nodes:
            if isinstance(item, dict):
                node_records.append(item)
    return node_records


def append_unique(bucket: DefaultDict[str, List[str]], key: str, value: str) -> None:
    lst = bucket[key]
    if value not in lst:
        lst.append(value)


def clean_name(text: str) -> str:
    return " ".join(str(text or "").strip().split())


def ordered_unique(values: Iterable[str]) -> List[str]:
    out: List[str] = []
    seen: Set[str] = set()
    for value in values:
        if value not in seen:
            seen.add(value)
            out.append(value)
    return out


def stage_family_from_book_code(book_code: str) -> str:
    if book_code in PRIMARY_MATH_BOOK_CODES:
        return "primaryschool"
    if book_code in MIDDLE_SCHOOL_BOOK_CODES:
        return "middleschool"
    if book_code in HIGH_SCHOOL_BOOK_CODES:
        return "highschool"
    return ""


def parse_subject_and_book_code(node_id: str) -> Tuple[str, str]:
    match = TYPE_CODE_RE.match(str(node_id or "").strip())
    if not match:
        return ("", "")
    return (match.group("subject"), match.group("book_code"))


def parse_chapter_sort_key(node_id: str) -> Tuple[int, int, int, str]:
    match = CHAPTER_ID_RE.match(str(node_id or "").strip())
    if not match:
        return (10**9, 10**9, 0, str(node_id))
    return (
        BOOK_ORDER_INDEX.get(match.group("book_code"), 10**9),
        int(match.group("chapter")),
        0,
        str(node_id),
    )


def parse_section_sort_key(node_id: str) -> Tuple[int, int, int, str]:
    match = SECTION_ID_RE.match(str(node_id or "").strip())
    if not match:
        return (10**9, 10**9, 10**9, str(node_id))
    return (
        BOOK_ORDER_INDEX.get(match.group("book_code"), 10**9),
        int(match.group("chapter")),
        int(match.group("section")),
        str(node_id),
    )


def location_sort_key(node_id: str, label: str) -> Tuple[int, int, int, str]:
    if label == "Section":
        return parse_section_sort_key(node_id)
    if label == "Chapter":
        return parse_chapter_sort_key(node_id)
    return (10**9, 10**9, 10**9, str(node_id))


def stable_template(prefix: str, sample_key: str, templates: Sequence[str], value: str) -> str:
    digest = hashlib.sha256(f"{prefix}|{sample_key}".encode("utf-8")).hexdigest()
    index = int(digest[:8], 16) % len(templates)
    return templates[index].replace("【X】", value)


def node_display_name(node: NodeInfo) -> str:
    if node.label == "Exercise" and clean_name(node.stem):
        return clean_name(node.stem)
    return clean_name(node.name) or node.node_id


def names_for_ids(nodes: Dict[str, NodeInfo], node_ids: Iterable[str]) -> List[str]:
    out: List[str] = []
    seen: Set[str] = set()
    for node_id in node_ids:
        node = nodes.get(node_id)
        if not node:
            continue
        name = node_display_name(node)
        if not name or name in seen:
            continue
        seen.add(name)
        out.append(name)
    return out


def build_node_map(nodes: List[Dict[str, Any]]) -> Dict[str, NodeInfo]:
    out: Dict[str, NodeInfo] = {}
    for item in nodes:
        node_id = str(item.get("id", "")).strip()
        label = str(item.get("label", "")).strip()
        name = clean_name(str(item.get("name", "")))
        properties = item.get("properties", {})
        if not isinstance(properties, dict):
            properties = {}
        stem = clean_name(str(properties.get("stem", "")))
        if not node_id or not label:
            continue
        out[node_id] = NodeInfo(
            node_id=node_id,
            label=label,
            name=name,
            stem=stem,
            properties=properties,
        )
    return out


def build_indexes(nodes: Dict[str, NodeInfo], edges: List[Dict[str, Any]]) -> Dict[str, Any]:
    rel_adj: DefaultDict[str, Set[str]] = defaultdict(set)
    isa_adj: DefaultDict[str, Set[str]] = defaultdict(set)
    prereq_in: DefaultDict[str, Set[str]] = defaultdict(set)
    prereq_out: DefaultDict[str, Set[str]] = defaultdict(set)
    leads_to_in: DefaultDict[str, Set[str]] = defaultdict(set)
    leads_to_out: DefaultDict[str, Set[str]] = defaultdict(set)
    tests_concept_by_ex: DefaultDict[str, List[str]] = defaultdict(list)
    tests_skill_by_ex: DefaultDict[str, List[str]] = defaultdict(list)
    ex_by_concept: DefaultDict[str, List[str]] = defaultdict(list)
    ex_by_skill: DefaultDict[str, List[str]] = defaultdict(list)
    verifies_exp_to_concept: DefaultDict[str, List[str]] = defaultdict(list)
    verifies_concept_to_exp: DefaultDict[str, List[str]] = defaultdict(list)
    appears_src_to_targets: DefaultDict[str, List[str]] = defaultdict(list)
    part_of_parent: DefaultDict[str, Set[str]] = defaultdict(set)
    part_of_children: DefaultDict[str, Set[str]] = defaultdict(set)

    order_maps: Dict[str, Dict[str, int]] = {
        "task1_subtask1_concept": {},
        "task1_subtask1_skill": {},
        "task1_subtask2": {},
        "task2_subtask1": {},
        "task2_subtask2": {},
        "task3": {},
        "task4_subtask1": {},
        "task4_subtask2": {},
        "task5_subtask1": {},
        "task5_subtask2": {},
    }

    def touch(order_key: str, query_key: str, edge_index: int) -> None:
        if query_key not in order_maps[order_key]:
            order_maps[order_key][query_key] = edge_index

    for edge_index, edge in enumerate(edges):
        source = str(edge.get("source", "")).strip()
        target = str(edge.get("target", "")).strip()
        edge_type = str(edge.get("type", "")).strip()
        if not source or not target or not edge_type:
            continue

        if edge_type == "relates_to":
            rel_adj[source].add(target)
            rel_adj[target].add(source)
            touch("task3", source, edge_index)
            touch("task3", target, edge_index)
        elif edge_type == "is_a":
            isa_adj[source].add(target)
            isa_adj[target].add(source)
            touch("task3", source, edge_index)
            touch("task3", target, edge_index)
        elif edge_type == "prerequisites_for":
            prereq_out[source].add(target)
            prereq_in[target].add(source)
            touch("task2_subtask1", target, edge_index)
            touch("task2_subtask2", source, edge_index)
        elif edge_type == "tests_concept":
            append_unique(tests_concept_by_ex, source, target)
            append_unique(ex_by_concept, target, source)
            touch("task1_subtask1_concept", source, edge_index)
            touch("task1_subtask2", target, edge_index)
        elif edge_type == "tests_skill":
            append_unique(tests_skill_by_ex, source, target)
            append_unique(ex_by_skill, target, source)
            touch("task1_subtask1_skill", source, edge_index)
            touch("task1_subtask2", target, edge_index)
        elif edge_type == "verifies":
            append_unique(verifies_exp_to_concept, source, target)
            append_unique(verifies_concept_to_exp, target, source)
            touch("task4_subtask1", target, edge_index)
            touch("task4_subtask2", source, edge_index)
        elif edge_type == "appears_in":
            append_unique(appears_src_to_targets, source, target)
            touch("task5_subtask1", source, edge_index)
        elif edge_type == "leads_to":
            leads_to_out[source].add(target)
            leads_to_in[target].add(source)
            touch("task5_subtask2", target, edge_index)
        elif edge_type == "is_part_of":
            part_of_parent[source].add(target)
            part_of_children[target].add(source)

    return {
        "rel_adj": rel_adj,
        "isa_adj": isa_adj,
        "prereq_in": prereq_in,
        "prereq_out": prereq_out,
        "leads_to_in": leads_to_in,
        "leads_to_out": leads_to_out,
        "tests_concept_by_ex": tests_concept_by_ex,
        "tests_skill_by_ex": tests_skill_by_ex,
        "ex_by_concept": ex_by_concept,
        "ex_by_skill": ex_by_skill,
        "verifies_exp_to_concept": verifies_exp_to_concept,
        "verifies_concept_to_exp": verifies_concept_to_exp,
        "appears_src_to_targets": appears_src_to_targets,
        "part_of_parent": part_of_parent,
        "part_of_children": part_of_children,
        "order_maps": order_maps,
    }


def build_ancestors(part_of_parent: Dict[str, Set[str]], nodes: Dict[str, NodeInfo]) -> Dict[str, Dict[str, Set[str]]]:
    cache: Dict[str, Dict[str, Set[str]]] = {}

    def resolve(node_id: str) -> Dict[str, Set[str]]:
        if node_id in cache:
            return cache[node_id]
        out: Dict[str, Set[str]] = defaultdict(set)
        for parent in part_of_parent.get(node_id, set()):
            parent_node = nodes.get(parent)
            if not parent_node:
                continue
            out[parent_node.label].add(parent)
            parent_info = resolve(parent)
            for label, values in parent_info.items():
                out[label].update(values)
        cache[node_id] = out
        return out

    for node_id in nodes:
        resolve(node_id)
    return cache


def build_location_info(
    nodes: Dict[str, NodeInfo],
    appears_src_to_targets: Dict[str, List[str]],
    ancestors: Dict[str, Dict[str, Set[str]]],
) -> Dict[str, LocationInfo]:
    location_info: Dict[str, LocationInfo] = {}

    for node_id, node in nodes.items():
        info = LocationInfo()

        if node.label == "Section":
            info.sections.add(node_id)
            info.chapters.update(ancestors.get(node_id, {}).get("Chapter", set()))
            info.books.update(ancestors.get(node_id, {}).get("Book", set()))
        elif node.label == "Chapter":
            info.chapters.add(node_id)
            info.books.update(ancestors.get(node_id, {}).get("Book", set()))
        elif node.label == "Book":
            info.books.add(node_id)
        else:
            for target in appears_src_to_targets.get(node_id, []):
                target_node = nodes.get(target)
                if not target_node:
                    continue
                if target_node.label == "Section":
                    info.sections.add(target)
                    info.chapters.update(ancestors.get(target, {}).get("Chapter", set()))
                    info.books.update(ancestors.get(target, {}).get("Book", set()))
                elif target_node.label == "Chapter":
                    info.chapters.add(target)
                    info.books.update(ancestors.get(target, {}).get("Book", set()))
                elif target_node.label == "Book":
                    info.books.add(target)

        for chapter_id in list(info.chapters):
            info.books.update(ancestors.get(chapter_id, {}).get("Book", set()))

        subject, book_code = parse_subject_and_book_code(node_id)
        if subject:
            info.subjects.add(subject)
        if book_code:
            info.book_codes.add(book_code)
            stage = stage_family_from_book_code(book_code)
            if stage:
                info.subject_stages.add(f"{subject}:{stage}")

        for book_id in info.books:
            book_subject, book_code = parse_subject_and_book_code(book_id)
            if book_subject:
                info.subjects.add(book_subject)
            if book_code:
                info.book_codes.add(book_code)
                stage = stage_family_from_book_code(book_code)
                if stage and book_subject:
                    info.subject_stages.add(f"{book_subject}:{stage}")

        location_info[node_id] = info

    return location_info


def build_location_pools(
    nodes: Dict[str, NodeInfo],
    location_info: Dict[str, LocationInfo],
) -> Dict[str, Any]:
    by_section: DefaultDict[str, DefaultDict[str, Set[str]]] = defaultdict(lambda: defaultdict(set))
    by_chapter: DefaultDict[str, DefaultDict[str, Set[str]]] = defaultdict(lambda: defaultdict(set))
    by_book: DefaultDict[str, DefaultDict[str, Set[str]]] = defaultdict(lambda: defaultdict(set))
    by_subject_stage: DefaultDict[str, DefaultDict[str, Set[str]]] = defaultdict(lambda: defaultdict(set))
    by_subject: DefaultDict[str, DefaultDict[str, Set[str]]] = defaultdict(lambda: defaultdict(set))

    for node_id, node in nodes.items():
        info = location_info.get(node_id, LocationInfo())
        for section_id in info.sections:
            by_section[section_id][node.label].add(node_id)
        for chapter_id in info.chapters:
            by_chapter[chapter_id][node.label].add(node_id)
        for book_id in info.books:
            by_book[book_id][node.label].add(node_id)
        for subject_stage in info.subject_stages:
            by_subject_stage[subject_stage][node.label].add(node_id)
        for subject in info.subjects:
            by_subject[subject][node.label].add(node_id)

    return {
        "section": by_section,
        "chapter": by_chapter,
        "book": by_book,
        "subject_stage": by_subject_stage,
        "subject": by_subject,
    }


def undirected_three_rel_neighbors(idx: Dict[str, Any], node_id: str) -> Set[str]:
    return (
        set(idx["rel_adj"].get(node_id, set()))
        | set(idx["isa_adj"].get(node_id, set()))
        | set(idx["prereq_in"].get(node_id, set()))
        | set(idx["prereq_out"].get(node_id, set()))
    )


def exact_hop_nodes(idx: Dict[str, Any], start_ids: Iterable[str], hops: int) -> Set[str]:
    if hops <= 0:
        return set()
    start_set = {node_id for node_id in start_ids if node_id}
    if not start_set:
        return set()
    distances: Dict[str, int] = {}
    queue: deque[Tuple[str, int]] = deque((node_id, 0) for node_id in start_set)
    for node_id in start_set:
        distances[node_id] = 0
    while queue:
        node_id, depth = queue.popleft()
        if depth >= hops:
            continue
        for neighbor in undirected_three_rel_neighbors(idx, node_id):
            if neighbor not in distances or depth + 1 < distances[neighbor]:
                distances[neighbor] = depth + 1
                queue.append((neighbor, depth + 1))
    return {node_id for node_id, depth in distances.items() if depth == hops and node_id not in start_set}


def one_hop_union(idx: Dict[str, Any], node_ids: Iterable[str]) -> Set[str]:
    out: Set[str] = set()
    for node_id in node_ids:
        out.update(undirected_three_rel_neighbors(idx, node_id))
    return out


def collect_predecessor_closure(prereq_in: Dict[str, Set[str]], start_id: str) -> Set[str]:
    visited: Set[str] = set()
    stack = list(prereq_in.get(start_id, set()))
    while stack:
        node_id = stack.pop()
        if node_id in visited:
            continue
        visited.add(node_id)
        stack.extend(prereq_in.get(node_id, set()))
    return visited


def collect_successor_closure(prereq_out: Dict[str, Set[str]], start_id: str) -> Set[str]:
    visited: Set[str] = set()
    stack = list(prereq_out.get(start_id, set()))
    while stack:
        node_id = stack.pop()
        if node_id in visited:
            continue
        visited.add(node_id)
        stack.extend(prereq_out.get(node_id, set()))
    return visited


def min_location_key(location_info: Dict[str, LocationInfo], node_id: str, label: str) -> Tuple[int, int, int, str]:
    info = location_info.get(node_id, LocationInfo())
    if label == "Section":
        keys = [location_sort_key(section_id, "Section") for section_id in info.sections]
        return min(keys) if keys else (10**9, 10**9, 10**9, node_id)
    if label == "Chapter":
        keys = [location_sort_key(chapter_id, "Chapter") for chapter_id in info.chapters]
        return min(keys) if keys else (10**9, 10**9, 10**9, node_id)
    section_keys = [location_sort_key(section_id, "Section") for section_id in info.sections]
    chapter_keys = [location_sort_key(chapter_id, "Chapter") for chapter_id in info.chapters]
    keys = section_keys + chapter_keys
    return min(keys) if keys else (10**9, 10**9, 10**9, node_id)


def gather_pool_from_levels(
    location_pools: Dict[str, Any],
    location_info: Dict[str, LocationInfo],
    anchor_ids: Iterable[str],
    level: str,
    allowed_labels: Set[str],
) -> Set[str]:
    out: Set[str] = set()
    level_values: Set[str] = set()
    for anchor_id in anchor_ids:
        info = location_info.get(anchor_id, LocationInfo())
        if level == "section":
            level_values.update(info.sections)
        elif level == "chapter":
            level_values.update(info.chapters)
        elif level == "book":
            level_values.update(info.books)
        elif level == "subject_stage":
            level_values.update(info.subject_stages)
        elif level == "subject":
            level_values.update(info.subjects)

    for value in level_values:
        label_map = location_pools[level].get(value, {})
        for label in allowed_labels:
            out.update(label_map.get(label, set()))
    return out


def earlier_locations_same_scope(
    location_pools: Dict[str, Any],
    location_info: Dict[str, LocationInfo],
    answer_id: str,
    scope: str,
    granularity: str,
) -> Set[str]:
    answer_key = location_sort_key(answer_id, granularity)
    out: Set[str] = set()
    answer_loc = location_info.get(answer_id, LocationInfo())

    scope_values: Set[str] = set()
    if scope == "book":
        scope_values.update(answer_loc.books)
    elif scope == "subject_stage":
        scope_values.update(answer_loc.subject_stages)
    elif scope == "subject":
        scope_values.update(answer_loc.subjects)

    for value in scope_values:
        out.update(location_pools[scope].get(value, {}).get(granularity, set()))
    return {
        node_id
        for node_id in out
        if location_sort_key(node_id, granularity) < answer_key
    }


def later_locations_same_scope(
    location_pools: Dict[str, Any],
    location_info: Dict[str, LocationInfo],
    answer_id: str,
    scope: str,
    granularity: str,
) -> Set[str]:
    answer_key = location_sort_key(answer_id, granularity)
    out: Set[str] = set()
    answer_loc = location_info.get(answer_id, LocationInfo())

    scope_values: Set[str] = set()
    if scope == "book":
        scope_values.update(answer_loc.books)
    elif scope == "subject_stage":
        scope_values.update(answer_loc.subject_stages)
    elif scope == "subject":
        scope_values.update(answer_loc.subjects)

    for value in scope_values:
        out.update(location_pools[scope].get(value, {}).get(granularity, set()))
    return {
        node_id
        for node_id in out
        if location_sort_key(node_id, granularity) > answer_key
    }


def rank_candidates(
    nodes: Dict[str, NodeInfo],
    scorer: EmbeddingScorer,
    question_text: str,
    answer_ids: Sequence[str],
    candidate_ids: Iterable[str],
) -> List[CandidateItem]:
    answer_texts = names_for_ids(nodes, answer_ids)
    scorer.embed_texts([question_text, *answer_texts])
    question_vector = scorer.vector(question_text)
    answer_vectors = [scorer.vector(text) for text in answer_texts]

    items: List[CandidateItem] = []
    for candidate_id in candidate_ids:
        node = nodes.get(candidate_id)
        if not node:
            continue
        candidate_text = node_display_name(node)
        scorer.embed_texts([candidate_text])
        candidate_vector = scorer.vector(candidate_text)
        sim_question = scorer.normalized_cosine(candidate_vector, question_vector)
        sim_answer = 0.0
        for answer_vector in answer_vectors:
            sim_answer = max(sim_answer, scorer.normalized_cosine(candidate_vector, answer_vector))
        sim_score = (sim_question + sim_answer) / 2.0
        items.append(
            CandidateItem(
                node_id=candidate_id,
                name=candidate_text,
                label=node.label,
                sim_score=sim_score,
                sim_question=sim_question,
                sim_answer=sim_answer,
            )
        )
    items.sort(
        key=lambda item: (
            -item.sim_score,
            -item.sim_question,
            -item.sim_answer,
            item.name,
            item.node_id,
        )
    )
    return items


def finalize_candidate_layers(
    nodes: Dict[str, NodeInfo],
    scorer: EmbeddingScorer,
    question_text: str,
    layer_specs: Sequence[Tuple[str, str, Iterable[str], Iterable[str]]],
    answer_ids: Sequence[str],
    blocked_ids: Set[str],
    allowed_labels: Optional[Set[str]],
    stop_at: int = 10,
) -> Tuple[List[Dict[str, Any]], List[str]]:
    answer_names = set(names_for_ids(nodes, answer_ids))
    seen_ids: Set[str] = set(answer_ids) | set(blocked_ids)
    seen_names: Set[str] = set(answer_names)
    flat_candidates: List[str] = []
    out_layers: List[Dict[str, Any]] = []

    for layer_index, (rule, description, raw_candidate_ids, anchor_ids) in enumerate(layer_specs, start=1):
        deduped_raw = ordered_unique(raw_candidate_ids)
        ranked = rank_candidates(nodes, scorer, question_text, answer_ids, deduped_raw)
        kept_items: List[CandidateItem] = []
        layer_seen_names: Set[str] = set()
        layer_seen_ids: Set[str] = set()
        for item in ranked:
            if item.node_id in seen_ids or item.node_id in layer_seen_ids:
                continue
            if item.name in seen_names or item.name in layer_seen_names:
                continue
            if allowed_labels and item.label not in allowed_labels:
                continue
            layer_seen_ids.add(item.node_id)
            layer_seen_names.add(item.name)
            kept_items.append(item)

        if kept_items:
            candidate_ids = [item.node_id for item in kept_items]
            candidate_names = [item.name for item in kept_items]
            out_layers.append(
                {
                    "layer": layer_index,
                    "rule": rule,
                    "description": description,
                    "candidate_ids": candidate_ids,
                    "candidate_names": candidate_names,
                    "candidates": [
                        {
                            "id": item.node_id,
                            "name": item.name,
                            "label": item.label,
                            "sim_score": round(item.sim_score, 4),
                            "sim_question": round(item.sim_question, 4),
                            "sim_answer": round(item.sim_answer, 4),
                        }
                        for item in kept_items
                    ],
                }
            )
            flat_candidates.extend(candidate_ids)
            seen_ids.update(candidate_ids)
            seen_names.update(candidate_names)

        if len(flat_candidates) >= stop_at:
            break

    return out_layers, flat_candidates


def query_subject_cn(location_info: Dict[str, LocationInfo], query_id: str) -> str:
    info = location_info.get(query_id, LocationInfo())
    subjects = sorted(info.subjects)
    if not subjects:
        subject, _ = parse_subject_and_book_code(query_id)
        return SUBJECT_CN.get(subject, "")
    return SUBJECT_CN.get(subjects[0], "")


def format_subject_prefix(subject_cn: str) -> str:
    return f"{subject_cn}中的" if subject_cn else ""


def node_subject_stage_cn(node_id: str) -> str:
    subject, book_code = parse_subject_and_book_code(node_id)
    stage = STAGE_CN.get(stage_family_from_book_code(book_code), "")
    subject_cn = SUBJECT_CN.get(subject, "")
    return f"{stage}{subject_cn}".strip()


def ordered_subject_chapters(
    location_pools: Dict[str, Any],
    subject: str,
) -> List[str]:
    chapter_ids = location_pools["subject"].get(subject, {}).get("Chapter", set())
    return sorted(chapter_ids, key=parse_chapter_sort_key)


def previous_subject_chapters(
    location_pools: Dict[str, Any],
    location_info: Dict[str, LocationInfo],
    chapter_id: str,
    count: int,
) -> List[str]:
    info = location_info.get(chapter_id, LocationInfo())
    subjects = sorted(info.subjects)
    if not subjects:
        return []
    ordered = ordered_subject_chapters(location_pools, subjects[0])
    try:
        index = ordered.index(chapter_id)
    except ValueError:
        return []
    start = max(0, index - count)
    return list(reversed(ordered[start:index]))


def make_record(
    *,
    sample_id: str,
    task: str,
    subtask: str,
    question: str,
    query_id: str,
    query_label: str,
    query_text: str,
    answer_ids: Sequence[str],
    candidate_layers: List[Dict[str, Any]],
    flat_candidate_ids: List[str],
    nodes: Dict[str, NodeInfo],
    meta: Dict[str, Any],
) -> Dict[str, Any]:
    return {
        "id": sample_id,
        "task": task,
        "subtask": subtask,
        "query_id": query_id,
        "query_label": query_label,
        "query_text": query_text,
        "answer_ids": list(answer_ids),
        "answer_names": names_for_ids(nodes, answer_ids),
        "candidate_layers": candidate_layers,
        "candidate_ids": flat_candidate_ids,
        "candidate_names": names_for_ids(nodes, flat_candidate_ids),
        "meta": meta,
    }


def gen_task1_subtask1(
    nodes: Dict[str, NodeInfo],
    idx: Dict[str, Any],
    location_info: Dict[str, LocationInfo],
    location_pools: Dict[str, Any],
    scorer: EmbeddingScorer,
) -> List[Dict[str, Any]]:
    templates_concept = [
        "【X】这道题考察了什么核心概念？",
        "要解决【X】这道题，需要用到哪些知识？",
    ]
    templates_skill = [
        "【X】这道题考察了什么核心方法？",
        "要解决【X】这道题，需要用到哪些知识？",
    ]
    records: List[Dict[str, Any]] = []

    query_order: List[Tuple[int, str, str]] = []
    for ex_id, order in idx["order_maps"]["task1_subtask1_concept"].items():
        query_order.append((order, ex_id, "Concept"))
    for ex_id, order in idx["order_maps"]["task1_subtask1_skill"].items():
        query_order.append((order, ex_id, "Skill"))
    query_order.sort(key=lambda item: (item[0], item[1], item[2]))

    for _, ex_id, answer_label in query_order:
        exercise = nodes.get(ex_id)
        if not exercise or exercise.label != "Exercise":
            continue

        if answer_label == "Concept":
            answer_ids = [
                node_id
                for node_id in idx["tests_concept_by_ex"].get(ex_id, [])
                if node_id in nodes and nodes[node_id].label == "Concept"
            ]
            templates = templates_concept
        else:
            answer_ids = [
                node_id
                for node_id in idx["tests_skill_by_ex"].get(ex_id, [])
                if node_id in nodes and nodes[node_id].label == "Skill"
            ]
            templates = templates_skill

        answer_ids = ordered_unique(answer_ids)
        if not answer_ids:
            continue

        one_hop_answers = one_hop_union(idx, answer_ids)
        allowed_labels = {answer_label}
        layer_specs = [
            (
                "answer_two_hop",
                "正确答案的 2-hop 节点",
                exact_hop_nodes(idx, answer_ids, 2),
                answer_ids,
            ),
            (
                "same_section",
                "和正确答案属于同个 section、同类型的其他节点",
                gather_pool_from_levels(location_pools, location_info, answer_ids, "section", allowed_labels),
                answer_ids,
            ),
            (
                "same_chapter",
                "和正确答案属于同个 chapter、同类型的其他节点",
                gather_pool_from_levels(location_pools, location_info, answer_ids, "chapter", allowed_labels),
                answer_ids,
            ),
            (
                "same_book",
                "和正确答案属于同本 book、同类型的其他节点",
                gather_pool_from_levels(location_pools, location_info, answer_ids, "book", allowed_labels),
                answer_ids,
            ),
            (
                "same_subject_stage",
                "和正确答案属于同个 subject_stage、同类型的其他节点",
                gather_pool_from_levels(location_pools, location_info, answer_ids, "subject_stage", allowed_labels),
                answer_ids,
            ),
            (
                "same_subject",
                "和正确答案属于同个 subject、同类型的其他节点",
                gather_pool_from_levels(location_pools, location_info, answer_ids, "subject", allowed_labels),
                answer_ids,
            ),
        ]
        if all(location_info.get(answer_id, LocationInfo()).is_primary_math for answer_id in answer_ids):
            layer_specs = [spec for spec in layer_specs if spec[0] != "same_section"]

        sample_id = f"task1::subtask1::{answer_label.lower()}::{ex_id}"
        question = stable_template("task1_subtask1", sample_id, templates, f"【{node_display_name(exercise)}】")
        candidate_layers, flat_candidate_ids = finalize_candidate_layers(
            nodes=nodes,
            scorer=scorer,
            question_text=question,
            layer_specs=layer_specs,
            answer_ids=answer_ids,
            blocked_ids=one_hop_answers,
            allowed_labels=allowed_labels,
        )
        records.append(
            make_record(
                sample_id=sample_id,
                task="task1",
                subtask="subtask1",
                question=question,
                query_id=ex_id,
                query_label="Exercise",
                query_text=node_display_name(exercise),
                answer_ids=answer_ids,
                candidate_layers=candidate_layers,
                flat_candidate_ids=flat_candidate_ids,
                nodes=nodes,
                meta={
                    "query_answer_type": answer_label,
                    "relation": "tests_concept" if answer_label == "Concept" else "tests_skill",
                },
            )
        )

    return records


def gen_task1_subtask2(
    nodes: Dict[str, NodeInfo],
    idx: Dict[str, Any],
    location_info: Dict[str, LocationInfo],
    location_pools: Dict[str, Any],
    scorer: EmbeddingScorer,
) -> List[Dict[str, Any]]:
    templates = [
        "哪些题目考察了【X】这个知识点？",
        "以下哪些题目会用到【X】？",
        "围绕【X】这一知识点，教材里有哪些例题？",
    ]
    records: List[Dict[str, Any]] = []
    ordered_queries = sorted(idx["order_maps"]["task1_subtask2"].items(), key=lambda item: (item[1], item[0]))

    for query_id, _ in ordered_queries:
        query = nodes.get(query_id)
        if not query or query.label not in {"Concept", "Skill"}:
            continue

        if query.label == "Concept":
            answer_ids = [
                ex_id
                for ex_id in idx["ex_by_concept"].get(query_id, [])
                if ex_id in nodes and nodes[ex_id].label == "Exercise"
            ]
            two_hop_nodes = [node_id for node_id in exact_hop_nodes(idx, [query_id], 2) if nodes.get(node_id, NodeInfo("", "", "")).label == "Concept"]
            one_hop_nodes = [node_id for node_id in undirected_three_rel_neighbors(idx, query_id) if nodes.get(node_id, NodeInfo("", "", "")).label == "Concept"]
            layer1_candidates = []
            blocked_by_one_hop = []
            for node_id in two_hop_nodes:
                layer1_candidates.extend(idx["ex_by_concept"].get(node_id, []))
            for node_id in one_hop_nodes:
                blocked_by_one_hop.extend(idx["ex_by_concept"].get(node_id, []))
        else:
            answer_ids = [
                ex_id
                for ex_id in idx["ex_by_skill"].get(query_id, [])
                if ex_id in nodes and nodes[ex_id].label == "Exercise"
            ]
            two_hop_nodes = [node_id for node_id in exact_hop_nodes(idx, [query_id], 2) if nodes.get(node_id, NodeInfo("", "", "")).label == "Skill"]
            one_hop_nodes = [node_id for node_id in undirected_three_rel_neighbors(idx, query_id) if nodes.get(node_id, NodeInfo("", "", "")).label == "Skill"]
            layer1_candidates = []
            blocked_by_one_hop = []
            for node_id in two_hop_nodes:
                layer1_candidates.extend(idx["ex_by_skill"].get(node_id, []))
            for node_id in one_hop_nodes:
                blocked_by_one_hop.extend(idx["ex_by_skill"].get(node_id, []))

        answer_ids = ordered_unique(answer_ids)
        if not answer_ids:
            continue

        layer_specs = [
            (
                "query_two_hop_to_exercises",
                "query 节点的 2-hop 节点对应的 exercise",
                layer1_candidates,
                answer_ids,
            ),
            (
                "same_section",
                "和正确答案属于同个 section 的其他 exercise",
                gather_pool_from_levels(location_pools, location_info, answer_ids, "section", {"Exercise"}),
                answer_ids,
            ),
            (
                "same_chapter",
                "和正确答案属于同个 chapter 的其他 exercise",
                gather_pool_from_levels(location_pools, location_info, answer_ids, "chapter", {"Exercise"}),
                answer_ids,
            ),
            (
                "same_book",
                "和正确答案属于同本 book 的其他 exercise",
                gather_pool_from_levels(location_pools, location_info, answer_ids, "book", {"Exercise"}),
                answer_ids,
            ),
            (
                "same_subject_stage",
                "和正确答案属于同个 subject_stage 的其他 exercise",
                gather_pool_from_levels(location_pools, location_info, answer_ids, "subject_stage", {"Exercise"}),
                answer_ids,
            ),
            (
                "same_subject",
                "和正确答案属于同个 subject 的其他 exercise",
                gather_pool_from_levels(location_pools, location_info, answer_ids, "subject", {"Exercise"}),
                answer_ids,
            ),
        ]
        if all(location_info.get(answer_id, LocationInfo()).is_primary_math for answer_id in answer_ids):
            layer_specs = [spec for spec in layer_specs if spec[0] != "same_section"]

        subject_prefix = format_subject_prefix(query_subject_cn(location_info, query_id))
        sample_id = f"task1::subtask2::{query_id}"
        question = stable_template("task1_subtask2", sample_id, templates, f"{subject_prefix}【{node_display_name(query)}】")
        candidate_layers, flat_candidate_ids = finalize_candidate_layers(
            nodes=nodes,
            scorer=scorer,
            question_text=question,
            layer_specs=layer_specs,
            answer_ids=answer_ids,
            blocked_ids=set(blocked_by_one_hop),
            allowed_labels={"Exercise"},
        )
        records.append(
            make_record(
                sample_id=sample_id,
                task="task1",
                subtask="subtask2",
                question=question,
                query_id=query_id,
                query_label=query.label,
                query_text=node_display_name(query),
                answer_ids=answer_ids,
                candidate_layers=candidate_layers,
                flat_candidate_ids=flat_candidate_ids,
                nodes=nodes,
                meta={"query_label": query.label},
            )
        )

    return records


def gen_task2_subtask1(
    nodes: Dict[str, NodeInfo],
    idx: Dict[str, Any],
    location_info: Dict[str, LocationInfo],
    location_pools: Dict[str, Any],
    scorer: EmbeddingScorer,
) -> List[Dict[str, Any]]:
    templates = [
        "要掌握【X】，应先具备哪些基础？",
        "以下哪些知识是【X】的前置知识？",
        "在学习【X】之前，需要学习哪些知识？",
    ]
    records: List[Dict[str, Any]] = []
    ordered_queries = sorted(idx["order_maps"]["task2_subtask1"].items(), key=lambda item: (item[1], item[0]))

    for query_id, _ in ordered_queries:
        query = nodes.get(query_id)
        if not query or query.label not in TASK2_LABELS:
            continue
        answer_ids = sorted(
            [node_id for node_id in collect_predecessor_closure(idx["prereq_in"], query_id) if node_id in nodes and nodes[node_id].label in TASK2_LABELS],
            key=lambda node_id: (min_location_key(location_info, node_id, nodes[node_id].label), node_display_name(nodes[node_id]), node_id),
        )
        if not answer_ids:
            continue

        answer_labels = {nodes[node_id].label for node_id in answer_ids}
        successor_tree = collect_successor_closure(idx["prereq_out"], query_id)
        perimeter_two_hop: Set[str] = set()
        for answer_id in answer_ids:
            perimeter_two_hop.update(exact_hop_nodes(idx, [answer_id], 2))
        perimeter_two_hop -= set(answer_ids)
        one_hop_answers = one_hop_union(idx, answer_ids)

        layer_specs = [
            (
                "query_successor_tree",
                "query 节点的整棵后置节点树",
                successor_tree,
                answer_ids,
            ),
            (
                "predecessor_tree_perimeter_two_hop",
                "前置节点树周围的 2-hop 节点",
                perimeter_two_hop,
                answer_ids,
            ),
            (
                "same_section",
                "和正确答案属于同个 section、同类型的其他节点",
                gather_pool_from_levels(location_pools, location_info, answer_ids, "section", answer_labels),
                answer_ids,
            ),
            (
                "same_chapter",
                "和正确答案属于同个 chapter、同类型的其他节点",
                gather_pool_from_levels(location_pools, location_info, answer_ids, "chapter", answer_labels),
                answer_ids,
            ),
            (
                "same_book",
                "和正确答案属于同本 book、同类型的其他节点",
                gather_pool_from_levels(location_pools, location_info, answer_ids, "book", answer_labels),
                answer_ids,
            ),
            (
                "same_subject_stage",
                "和正确答案属于同个 subject_stage、同类型的其他节点",
                gather_pool_from_levels(location_pools, location_info, answer_ids, "subject_stage", answer_labels),
                answer_ids,
            ),
            (
                "same_subject",
                "和正确答案属于同个 subject、同类型的其他节点",
                gather_pool_from_levels(location_pools, location_info, answer_ids, "subject", answer_labels),
                answer_ids,
            ),
        ]
        if all(location_info.get(answer_id, LocationInfo()).is_primary_math for answer_id in answer_ids):
            layer_specs = [spec for spec in layer_specs if spec[0] != "same_section"]

        subject_prefix = format_subject_prefix(query_subject_cn(location_info, query_id))
        sample_id = f"task2::subtask1::{query_id}"
        question = stable_template("task2_subtask1", sample_id, templates, f"{subject_prefix}【{node_display_name(query)}】")
        candidate_layers, flat_candidate_ids = finalize_candidate_layers(
            nodes=nodes,
            scorer=scorer,
            question_text=question,
            layer_specs=layer_specs,
            answer_ids=answer_ids,
            blocked_ids=one_hop_answers | {query_id},
            allowed_labels=TASK2_LABELS,
        )
        records.append(
            make_record(
                sample_id=sample_id,
                task="task2",
                subtask="subtask1",
                question=question,
                query_id=query_id,
                query_label=query.label,
                query_text=node_display_name(query),
                answer_ids=answer_ids,
                candidate_layers=candidate_layers,
                flat_candidate_ids=flat_candidate_ids,
                nodes=nodes,
                meta={"query_label": query.label, "answer_labels": sorted(answer_labels)},
            )
        )

    return records


def gen_task2_subtask2(
    nodes: Dict[str, NodeInfo],
    idx: Dict[str, Any],
    location_info: Dict[str, LocationInfo],
    location_pools: Dict[str, Any],
    scorer: EmbeddingScorer,
) -> List[Dict[str, Any]]:
    templates = [
        "在学习了【X】之后，下一步最适合学习什么知识？",
        "以下哪些知识是【X】的最直接后置知识？",
        "掌握【X】后，通常会马上继续学习哪些内容？",
    ]
    records: List[Dict[str, Any]] = []
    ordered_queries = sorted(idx["order_maps"]["task2_subtask2"].items(), key=lambda item: (item[1], item[0]))

    for query_id, _ in ordered_queries:
        query = nodes.get(query_id)
        if not query or query.label not in TASK2_LABELS:
            continue
        answer_ids = [
            node_id
            for node_id in idx["prereq_out"].get(query_id, set())
            if node_id in nodes and nodes[node_id].label in TASK2_LABELS
        ]
        answer_ids = sorted(
            answer_ids,
            key=lambda node_id: (min_location_key(location_info, node_id, nodes[node_id].label), node_display_name(nodes[node_id]), node_id),
        )
        if not answer_ids:
            continue

        answer_labels = {nodes[node_id].label for node_id in answer_ids}
        predecessor_tree = collect_predecessor_closure(idx["prereq_in"], query_id)
        second_successors: Set[str] = set()
        for answer_id in answer_ids:
            second_successors.update(idx["prereq_out"].get(answer_id, set()))
        one_hop_answers = one_hop_union(idx, answer_ids)

        layer_specs = [
            (
                "query_predecessor_tree",
                "query 节点的完整前置闭包",
                predecessor_tree,
                answer_ids,
            ),
            (
                "answers_second_successors",
                "正确答案的后置节点的后置节点",
                second_successors,
                answer_ids,
            ),
            (
                "same_section",
                "和正确答案属于同个 section、同类型的其他节点",
                gather_pool_from_levels(location_pools, location_info, answer_ids, "section", answer_labels),
                answer_ids,
            ),
            (
                "same_chapter",
                "和正确答案属于同个 chapter、同类型的其他节点",
                gather_pool_from_levels(location_pools, location_info, answer_ids, "chapter", answer_labels),
                answer_ids,
            ),
            (
                "same_book",
                "和正确答案属于同本 book、同类型的其他节点",
                gather_pool_from_levels(location_pools, location_info, answer_ids, "book", answer_labels),
                answer_ids,
            ),
            (
                "same_subject_stage",
                "和正确答案属于同个 subject_stage、同类型的其他节点",
                gather_pool_from_levels(location_pools, location_info, answer_ids, "subject_stage", answer_labels),
                answer_ids,
            ),
            (
                "same_subject",
                "和正确答案属于同个 subject、同类型的其他节点",
                gather_pool_from_levels(location_pools, location_info, answer_ids, "subject", answer_labels),
                answer_ids,
            ),
        ]
        if all(location_info.get(answer_id, LocationInfo()).is_primary_math for answer_id in answer_ids):
            layer_specs = [spec for spec in layer_specs if spec[0] != "same_section"]

        subject_prefix = format_subject_prefix(query_subject_cn(location_info, query_id))
        sample_id = f"task2::subtask2::{query_id}"
        question = stable_template("task2_subtask2", sample_id, templates, f"{subject_prefix}【{node_display_name(query)}】")
        candidate_layers, flat_candidate_ids = finalize_candidate_layers(
            nodes=nodes,
            scorer=scorer,
            question_text=question,
            layer_specs=layer_specs,
            answer_ids=answer_ids,
            blocked_ids=one_hop_answers | {query_id},
            allowed_labels=TASK2_LABELS,
        )
        records.append(
            make_record(
                sample_id=sample_id,
                task="task2",
                subtask="subtask2",
                question=question,
                query_id=query_id,
                query_label=query.label,
                query_text=node_display_name(query),
                answer_ids=answer_ids,
                candidate_layers=candidate_layers,
                flat_candidate_ids=flat_candidate_ids,
                nodes=nodes,
                meta={"query_label": query.label, "answer_labels": sorted(answer_labels)},
            )
        )

    return records


def gen_task3(
    nodes: Dict[str, NodeInfo],
    idx: Dict[str, Any],
    location_info: Dict[str, LocationInfo],
    location_pools: Dict[str, Any],
    scorer: EmbeddingScorer,
) -> List[Dict[str, Any]]:
    templates = [
        "以下哪些概念与【X】直接相关（包括分类关系或紧密关联）？",
    ]
    records: List[Dict[str, Any]] = []
    ordered_queries = sorted(idx["order_maps"]["task3"].items(), key=lambda item: (item[1], item[0]))

    for query_id, _ in ordered_queries:
        query = nodes.get(query_id)
        if not query or query.label != "Concept":
            continue
        answer_ids = sorted(
            [
                node_id
                for node_id in (idx["rel_adj"].get(query_id, set()) | idx["isa_adj"].get(query_id, set()))
                if node_id in nodes and nodes[node_id].label == "Concept" and node_id != query_id
            ],
            key=lambda node_id: (min_location_key(location_info, node_id, "Concept"), node_display_name(nodes[node_id]), node_id),
        )
        if not answer_ids:
            continue

        one_hop_answers = one_hop_union(idx, answer_ids)
        rr2_nodes: Set[str] = set()
        for answer_id in answer_ids:
            for middle_id in idx["rel_adj"].get(answer_id, set()):
                rr2_nodes.update(idx["rel_adj"].get(middle_id, set()))

        layer_specs = [
            (
                "answer_two_hop",
                "正确答案的 2-hop 节点",
                exact_hop_nodes(idx, answer_ids, 2),
                answer_ids,
            ),
            (
                "same_section",
                "和正确答案属于同个 section、同类型的其他节点",
                gather_pool_from_levels(location_pools, location_info, answer_ids, "section", {"Concept"}),
                answer_ids,
            ),
            (
                "same_chapter",
                "和正确答案属于同个 chapter、同类型的其他节点",
                gather_pool_from_levels(location_pools, location_info, answer_ids, "chapter", {"Concept"}),
                answer_ids,
            ),
            (
                "same_book",
                "和正确答案属于同本 book、同类型的其他节点",
                gather_pool_from_levels(location_pools, location_info, answer_ids, "book", {"Concept"}),
                answer_ids,
            ),
            (
                "same_subject_stage",
                "和正确答案属于同个 subject_stage、同类型的其他节点",
                gather_pool_from_levels(location_pools, location_info, answer_ids, "subject_stage", {"Concept"}),
                answer_ids,
            ),
            (
                "same_subject",
                "和正确答案属于同个 subject、同类型的其他节点",
                gather_pool_from_levels(location_pools, location_info, answer_ids, "subject", {"Concept"}),
                answer_ids,
            ),
        ]
        if all(location_info.get(answer_id, LocationInfo()).is_primary_math for answer_id in answer_ids):
            layer_specs = [spec for spec in layer_specs if spec[0] != "same_section"]

        subject_prefix = format_subject_prefix(query_subject_cn(location_info, query_id))
        sample_id = f"task3::subtask3::{query_id}"
        question = stable_template("task3", sample_id, templates, f"{subject_prefix}【{node_display_name(query)}】")
        candidate_layers, flat_candidate_ids = finalize_candidate_layers(
            nodes=nodes,
            scorer=scorer,
            question_text=question,
            layer_specs=layer_specs,
            answer_ids=answer_ids,
            blocked_ids=one_hop_answers | rr2_nodes | {query_id},
            allowed_labels={"Concept"},
        )
        records.append(
            make_record(
                sample_id=sample_id,
                task="task3",
                subtask="subtask3",
                question=question,
                query_id=query_id,
                query_label="Concept",
                query_text=node_display_name(query),
                answer_ids=answer_ids,
                candidate_layers=candidate_layers,
                flat_candidate_ids=flat_candidate_ids,
                nodes=nodes,
                meta={"relation": "relates_to|is_a"},
            )
        )

    return records


def gen_task4_subtask1(
    nodes: Dict[str, NodeInfo],
    idx: Dict[str, Any],
    location_info: Dict[str, LocationInfo],
    location_pools: Dict[str, Any],
    scorer: EmbeddingScorer,
) -> List[Dict[str, Any]]:
    templates = [
        "围绕【X】，教材安排了哪些验证实验？",
        "教材中哪个实验验证了【X】原理？",
        "以下哪些实验可以验证【X】？",
    ]
    records: List[Dict[str, Any]] = []
    ordered_queries = sorted(idx["order_maps"]["task4_subtask1"].items(), key=lambda item: (item[1], item[0]))

    for query_id, _ in ordered_queries:
        query = nodes.get(query_id)
        if not query or query.label != "Concept":
            continue
        answer_ids = [
            node_id
            for node_id in idx["verifies_concept_to_exp"].get(query_id, [])
            if node_id in nodes and nodes[node_id].label == "Experiment"
        ]
        answer_ids = ordered_unique(answer_ids)
        if not answer_ids:
            continue

        two_hop_concepts = [
            node_id
            for node_id in exact_hop_nodes(idx, [query_id], 2)
            if node_id in nodes and nodes[node_id].label == "Concept"
        ]
        one_hop_concepts = [
            node_id
            for node_id in undirected_three_rel_neighbors(idx, query_id)
            if node_id in nodes and nodes[node_id].label == "Concept"
        ]
        layer1_candidates: List[str] = []
        blocked_candidates: List[str] = []
        for concept_id in two_hop_concepts:
            layer1_candidates.extend(idx["verifies_concept_to_exp"].get(concept_id, []))
        for concept_id in one_hop_concepts:
            blocked_candidates.extend(idx["verifies_concept_to_exp"].get(concept_id, []))

        layer_specs = [
            (
                "query_two_hop_to_experiments",
                "query 节点的 2-hop 节点对应的 experiment",
                layer1_candidates,
                answer_ids,
            ),
            (
                "same_section",
                "和正确答案属于同个 section 的其他 experiment",
                gather_pool_from_levels(location_pools, location_info, answer_ids, "section", {"Experiment"}),
                answer_ids,
            ),
            (
                "same_chapter",
                "和正确答案属于同个 chapter 的其他 experiment",
                gather_pool_from_levels(location_pools, location_info, answer_ids, "chapter", {"Experiment"}),
                answer_ids,
            ),
            (
                "same_book",
                "和正确答案属于同本 book 的其他 experiment",
                gather_pool_from_levels(location_pools, location_info, answer_ids, "book", {"Experiment"}),
                answer_ids,
            ),
            (
                "same_subject_stage",
                "和正确答案属于同个 subject_stage 的其他 experiment",
                gather_pool_from_levels(location_pools, location_info, answer_ids, "subject_stage", {"Experiment"}),
                answer_ids,
            ),
            (
                "same_subject",
                "和正确答案属于同个 subject 的其他 experiment",
                gather_pool_from_levels(location_pools, location_info, answer_ids, "subject", {"Experiment"}),
                answer_ids,
            ),
        ]
        if all(location_info.get(answer_id, LocationInfo()).is_primary_math for answer_id in answer_ids):
            layer_specs = [spec for spec in layer_specs if spec[0] != "same_section"]

        subject_prefix = format_subject_prefix(query_subject_cn(location_info, query_id))
        sample_id = f"task4::subtask1::{query_id}"
        question = stable_template("task4_subtask1", sample_id, templates, f"{subject_prefix}【{node_display_name(query)}】")
        candidate_layers, flat_candidate_ids = finalize_candidate_layers(
            nodes=nodes,
            scorer=scorer,
            question_text=question,
            layer_specs=layer_specs,
            answer_ids=answer_ids,
            blocked_ids=set(blocked_candidates),
            allowed_labels={"Experiment"},
        )
        records.append(
            make_record(
                sample_id=sample_id,
                task="task4",
                subtask="subtask1",
                question=question,
                query_id=query_id,
                query_label="Concept",
                query_text=node_display_name(query),
                answer_ids=answer_ids,
                candidate_layers=candidate_layers,
                flat_candidate_ids=flat_candidate_ids,
                nodes=nodes,
                meta={"relation": "verifies"},
            )
        )

    return records


def gen_task4_subtask2(
    nodes: Dict[str, NodeInfo],
    idx: Dict[str, Any],
    location_info: Dict[str, LocationInfo],
    location_pools: Dict[str, Any],
    scorer: EmbeddingScorer,
) -> List[Dict[str, Any]]:
    templates = [
        "以下哪些概念可由【X】实验验证？",
        "通过【X】实验，可以支持哪些核心概念？",
        "【X】实验验证了什么原理？",
    ]
    records: List[Dict[str, Any]] = []
    ordered_queries = sorted(idx["order_maps"]["task4_subtask2"].items(), key=lambda item: (item[1], item[0]))

    for query_id, _ in ordered_queries:
        query = nodes.get(query_id)
        if not query or query.label != "Experiment":
            continue
        answer_ids = [
            node_id
            for node_id in idx["verifies_exp_to_concept"].get(query_id, [])
            if node_id in nodes and nodes[node_id].label == "Concept"
        ]
        answer_ids = ordered_unique(answer_ids)
        if not answer_ids:
            continue

        one_hop_answers = one_hop_union(idx, answer_ids)
        layer1_nodes = [
            node_id
            for node_id in exact_hop_nodes(idx, answer_ids, 2)
            if node_id in nodes and nodes[node_id].label == "Concept"
        ]
        layer2_nodes: List[str] = []
        for concept_id in layer1_nodes:
            for experiment_id in idx["verifies_concept_to_exp"].get(concept_id, []):
                layer2_nodes.extend(idx["verifies_exp_to_concept"].get(experiment_id, []))

        layer_specs = [
            (
                "answer_two_hop",
                "正确答案的 2-hop 节点",
                layer1_nodes,
                answer_ids,
            ),
            (
                "layer1_concepts_to_other_verified_concepts",
                "第一层 concept 对应的 experiment 验证的其他 concept",
                layer2_nodes,
                answer_ids,
            ),
            (
                "same_section",
                "和正确答案属于同个 section、同类型的其他节点",
                gather_pool_from_levels(location_pools, location_info, answer_ids, "section", {"Concept"}),
                answer_ids,
            ),
            (
                "same_chapter",
                "和正确答案属于同个 chapter、同类型的其他节点",
                gather_pool_from_levels(location_pools, location_info, answer_ids, "chapter", {"Concept"}),
                answer_ids,
            ),
            (
                "same_book",
                "和正确答案属于同本 book、同类型的其他节点",
                gather_pool_from_levels(location_pools, location_info, answer_ids, "book", {"Concept"}),
                answer_ids,
            ),
            (
                "same_subject_stage",
                "和正确答案属于同个 subject_stage、同类型的其他节点",
                gather_pool_from_levels(location_pools, location_info, answer_ids, "subject_stage", {"Concept"}),
                answer_ids,
            ),
            (
                "same_subject",
                "和正确答案属于同个 subject、同类型的其他节点",
                gather_pool_from_levels(location_pools, location_info, answer_ids, "subject", {"Concept"}),
                answer_ids,
            ),
        ]
        if all(location_info.get(answer_id, LocationInfo()).is_primary_math for answer_id in answer_ids):
            layer_specs = [spec for spec in layer_specs if spec[0] != "same_section"]

        subject_prefix = format_subject_prefix(query_subject_cn(location_info, query_id))
        sample_id = f"task4::subtask2::{query_id}"
        question = stable_template("task4_subtask2", sample_id, templates, f"{subject_prefix}【{node_display_name(query)}】")
        candidate_layers, flat_candidate_ids = finalize_candidate_layers(
            nodes=nodes,
            scorer=scorer,
            question_text=question,
            layer_specs=layer_specs,
            answer_ids=answer_ids,
            blocked_ids=one_hop_answers,
            allowed_labels={"Concept"},
        )
        records.append(
            make_record(
                sample_id=sample_id,
                task="task4",
                subtask="subtask2",
                question=question,
                query_id=query_id,
                query_label="Experiment",
                query_text=node_display_name(query),
                answer_ids=answer_ids,
                candidate_layers=candidate_layers,
                flat_candidate_ids=flat_candidate_ids,
                nodes=nodes,
                meta={"relation": "verifies"},
            )
        )

    return records


def resolve_task5_locations(nodes: Dict[str, NodeInfo], location_info: Dict[str, LocationInfo], query_id: str) -> Tuple[str, List[str]]:
    query_loc = location_info.get(query_id, LocationInfo())
    if query_loc.is_primary_math:
        location_ids = sorted(query_loc.chapters, key=parse_chapter_sort_key)
        return ("Chapter", ordered_unique(location_ids))
    location_ids = sorted(query_loc.sections, key=parse_section_sort_key)
    return ("Section", ordered_unique(location_ids))


def gen_task5_subtask1(
    nodes: Dict[str, NodeInfo],
    idx: Dict[str, Any],
    location_info: Dict[str, LocationInfo],
    location_pools: Dict[str, Any],
    scorer: EmbeddingScorer,
) -> List[Dict[str, Any]]:
    templates = [
        "【X】这一知识点最早出现在教材的哪个章节？",
        "学生第一次学习【X】是在以下哪个章节？",
    ]
    records: List[Dict[str, Any]] = []
    ordered_queries = sorted(idx["order_maps"]["task5_subtask1"].items(), key=lambda item: (item[1], item[0]))

    for query_id, _ in ordered_queries:
        query = nodes.get(query_id)
        if not query or query.label not in {"Concept", "Skill", "Experiment"}:
            continue
        granularity, locations = resolve_task5_locations(nodes, location_info, query_id)
        if not locations:
            continue

        answer_ids = [locations[0]]
        other_locations = locations[1:]
        answer_id = answer_ids[0]
        answer_loc = location_info.get(answer_id, LocationInfo())

        if granularity == "Section":
            same_chapter_others = gather_pool_from_levels(location_pools, location_info, answer_ids, "chapter", {"Section"})
        else:
            same_chapter_others = set()

        base_layer_specs = [
            (
                "other_appears_in_locations",
                "query 节点 appears_in 的其他位置",
                other_locations,
                answer_ids,
            ),
            (
                "same_chapter_other_sections",
                "和正确答案属于同个 chapter 的其他 section",
                same_chapter_others,
                answer_ids,
            ),
            (
                "same_book_earlier_locations",
                "和正确答案属于同本 book、且比正确答案更早的位置",
                earlier_locations_same_scope(location_pools, location_info, answer_id, "book", granularity),
                answer_ids,
            ),
            (
                "same_subject_stage_earlier_locations",
                "和正确答案属于同个 subject_stage、且比正确答案更早的位置",
                earlier_locations_same_scope(location_pools, location_info, answer_id, "subject_stage", granularity),
                answer_ids,
            ),
            (
                "same_subject_earlier_locations",
                "和正确答案属于同个 subject、且比正确答案更早的位置",
                earlier_locations_same_scope(location_pools, location_info, answer_id, "subject", granularity),
                answer_ids,
            ),
        ]
        if granularity == "Chapter":
            base_layer_specs = [spec for spec in base_layer_specs if spec[0] != "same_chapter_other_sections"]

        subject_prefix = format_subject_prefix(query_subject_cn(location_info, query_id))
        sample_id = f"task5::subtask1::{query_id}"
        question = stable_template("task5_subtask1", sample_id, templates, f"{subject_prefix}【{node_display_name(query)}】")
        _, preview_candidate_ids = finalize_candidate_layers(
            nodes=nodes,
            scorer=scorer,
            question_text=question,
            layer_specs=base_layer_specs,
            answer_ids=answer_ids,
            blocked_ids=set(),
            allowed_labels={granularity},
            stop_at=50,
        )
        layer_specs = list(base_layer_specs)
        if len(preview_candidate_ids) < 3:
            layer_specs.append(
                (
                    "same_book_later_locations",
                    "和正确答案属于同本 book、且比正确答案更晚的位置",
                    later_locations_same_scope(location_pools, location_info, answer_id, "book", granularity),
                    answer_ids,
                )
            )
        candidate_layers, flat_candidate_ids = finalize_candidate_layers(
            nodes=nodes,
            scorer=scorer,
            question_text=question,
            layer_specs=layer_specs,
            answer_ids=answer_ids,
            blocked_ids=set(),
            allowed_labels={granularity},
        )
        records.append(
            make_record(
                sample_id=sample_id,
                task="task5",
                subtask="subtask1",
                question=question,
                query_id=query_id,
                query_label=query.label,
                query_text=node_display_name(query),
                answer_ids=answer_ids,
                candidate_layers=candidate_layers,
                flat_candidate_ids=flat_candidate_ids,
                nodes=nodes,
                meta={
                    "source_id": query_id,
                    "source_label": query.label,
                    "query_label": query.label,
                    "answer_granularity": granularity,
                    "all_sorted_locations": locations,
                    "other_locations": other_locations,
                    "answer_books": sorted(answer_loc.books),
                    "answer_subject_stages": sorted(answer_loc.subject_stages),
                },
            )
        )

    return records


def gen_task5_subtask2(
    nodes: Dict[str, NodeInfo],
    idx: Dict[str, Any],
    location_info: Dict[str, LocationInfo],
    location_pools: Dict[str, Any],
    scorer: EmbeddingScorer,
) -> List[Dict[str, Any]]:
    templates = [
        "以下哪些章节的知识是【X】的基础？",
    ]
    records: List[Dict[str, Any]] = []
    ordered_queries = sorted(idx["order_maps"]["task5_subtask2"].items(), key=lambda item: (item[1], item[0]))

    for query_id, _ in ordered_queries:
        query = nodes.get(query_id)
        if not query or query.label != "Chapter":
            continue

        answer_ids = [
            node_id
            for node_id in idx["leads_to_in"].get(query_id, set())
            if node_id in nodes and nodes[node_id].label == "Chapter"
        ]
        answer_ids = sorted(ordered_unique(answer_ids), key=parse_chapter_sort_key)
        if not answer_ids:
            continue

        first_layer_raw: List[str] = []
        first_layer_raw.extend(previous_subject_chapters(location_pools, location_info, query_id, 3))
        for answer_id in answer_ids:
            first_layer_raw.extend(previous_subject_chapters(location_pools, location_info, answer_id, 2))

        layer1_preview, layer1_candidate_ids = finalize_candidate_layers(
            nodes=nodes,
            scorer=scorer,
            question_text="",
            layer_specs=[
                (
                    "neighbor_previous_chapters",
                    "query 前 1/2/3 个 chapter，以及每个正确答案前 1/2 个 chapter",
                    ordered_unique(first_layer_raw),
                    answer_ids,
                )
            ],
            answer_ids=answer_ids,
            blocked_ids={query_id},
            allowed_labels={"Chapter"},
            stop_at=50,
        )

        layer_specs = [
            (
                "neighbor_previous_chapters",
                "query 前 1/2/3 个 chapter，以及每个正确答案前 1/2 个 chapter",
                ordered_unique(first_layer_raw),
                answer_ids,
            )
        ]
        if len(layer1_candidate_ids) < 3:
            query_subject_stages = sorted(location_info.get(query_id, LocationInfo()).subject_stages)
            second_layer_raw: Set[str] = set()
            for subject_stage in query_subject_stages:
                second_layer_raw.update(location_pools["subject_stage"].get(subject_stage, {}).get("Chapter", set()))
            layer_specs.append(
                (
                    "same_subject_stage_other_chapters",
                    "同学科同学段的其他 chapter（仅当第一层不足 3 个时补充）",
                    second_layer_raw,
                    answer_ids,
                )
            )

        subject_stage_cn = node_subject_stage_cn(query_id)
        sample_id = f"task5::subtask2::{query_id}"
        question = stable_template("task5_subtask2", sample_id, templates, f"{subject_stage_cn}【{node_display_name(query)}】")
        candidate_layers, flat_candidate_ids = finalize_candidate_layers(
            nodes=nodes,
            scorer=scorer,
            question_text=question,
            layer_specs=layer_specs,
            answer_ids=answer_ids,
            blocked_ids={query_id},
            allowed_labels={"Chapter"},
        )
        records.append(
            make_record(
                sample_id=sample_id,
                task="task5",
                subtask="subtask2",
                question=question,
                query_id=query_id,
                query_label="Chapter",
                query_text=node_display_name(query),
                answer_ids=answer_ids,
                candidate_layers=candidate_layers,
                flat_candidate_ids=flat_candidate_ids,
                nodes=nodes,
                meta={
                    "relation": "leads_to",
                    "query_subject_stage": subject_stage_cn,
                    "source_chapter_ids": answer_ids,
                },
            )
        )

    return records


def validate_records(records: List[Dict[str, Any]], file_tag: str) -> None:
    seen_ids: Set[str] = set()
    for line_no, record in enumerate(records, start=1):
        sample_id = str(record.get("id", "")).strip()
        if not sample_id:
            raise ValueError(f"{file_tag}: empty id at record {line_no}")
        if sample_id in seen_ids:
            raise ValueError(f"{file_tag}: duplicate id {sample_id}")
        seen_ids.add(sample_id)

        answer_names = set(record.get("answer_names", []))
        if not answer_names:
            raise ValueError(f"{file_tag}: empty answer_names at id={sample_id}")

        candidate_names = set(record.get("candidate_names", []))
        overlap = answer_names & candidate_names
        if overlap:
            raise ValueError(f"{file_tag}: answer/candidate overlap at id={sample_id}: {sorted(overlap)[:3]}")


def parse_args() -> argparse.Namespace:
    repo_root = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(description="Build layered benchmark candidates from global_kg.")
    parser.add_argument("--kg-dir", type=Path, default=repo_root / "data" / "global_kg")
    parser.add_argument("--subject-kg-dir", type=Path, default=repo_root / "data" / "subject_kg")
    parser.add_argument("--output-dir", type=Path, default=repo_root / "data" / "benchmark_candidates")
    parser.add_argument("--embedding-model", default="BAAI/bge-small-zh-v1.5")
    parser.add_argument("--embedding-batch-size", type=int, default=256)
    parser.add_argument(
        "--tasks",
        nargs="+",
        default=[
            "task1_subtask1",
            "task1_subtask2",
            "task2_subtask1",
            "task2_subtask2",
            "task3",
            "task4_subtask1",
            "task4_subtask2",
            "task5_subtask1",
            "task5_subtask2",
        ],
        choices=[
            "task1_subtask1",
            "task1_subtask2",
            "task2_subtask1",
            "task2_subtask2",
            "task3",
            "task4_subtask1",
            "task4_subtask2",
            "task5_subtask1",
            "task5_subtask2",
        ],
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    nodes_path = args.kg_dir / "nodes.json"
    edges_path = args.kg_dir / "edges.json"
    global_node_records = load_json_list(nodes_path)
    subject_node_records = load_subject_node_records(args.subject_kg_dir)
    node_records = subject_node_records if subject_node_records else global_node_records
    if subject_node_records:
        global_fallback = {
            str(item.get("id", "")).strip(): item
            for item in global_node_records
            if isinstance(item, dict) and str(item.get("id", "")).strip()
        }
        seen_ids = {str(item.get("id", "")).strip() for item in subject_node_records}
        for node_id, item in global_fallback.items():
            if node_id not in seen_ids:
                node_records.append(item)
    nodes = build_node_map(node_records)
    edges = load_json_list(edges_path)
    idx = build_indexes(nodes, edges)
    ancestors = build_ancestors(idx["part_of_parent"], nodes)
    location_info = build_location_info(nodes, idx["appears_src_to_targets"], ancestors)
    location_pools = build_location_pools(nodes, location_info)
    scorer = EmbeddingScorer(model_name=args.embedding_model, batch_size=args.embedding_batch_size)

    outputs: Dict[str, List[Dict[str, Any]]] = {}
    if "task1_subtask1" in args.tasks:
        outputs["task1_subtask1"] = gen_task1_subtask1(nodes, idx, location_info, location_pools, scorer)
    if "task1_subtask2" in args.tasks:
        outputs["task1_subtask2"] = gen_task1_subtask2(nodes, idx, location_info, location_pools, scorer)
    if "task2_subtask1" in args.tasks:
        outputs["task2_subtask1"] = gen_task2_subtask1(nodes, idx, location_info, location_pools, scorer)
    if "task2_subtask2" in args.tasks:
        outputs["task2_subtask2"] = gen_task2_subtask2(nodes, idx, location_info, location_pools, scorer)
    if "task3" in args.tasks:
        outputs["task3"] = gen_task3(nodes, idx, location_info, location_pools, scorer)
    if "task4_subtask1" in args.tasks:
        outputs["task4_subtask1"] = gen_task4_subtask1(nodes, idx, location_info, location_pools, scorer)
    if "task4_subtask2" in args.tasks:
        outputs["task4_subtask2"] = gen_task4_subtask2(nodes, idx, location_info, location_pools, scorer)
    if "task5_subtask1" in args.tasks:
        outputs["task5_subtask1"] = gen_task5_subtask1(nodes, idx, location_info, location_pools, scorer)
    if "task5_subtask2" in args.tasks:
        outputs["task5_subtask2"] = gen_task5_subtask2(nodes, idx, location_info, location_pools, scorer)

    summary: Dict[str, Any] = {
        "kg_dir": str(args.kg_dir),
        "subject_kg_dir": str(args.subject_kg_dir),
        "output_dir": str(args.output_dir),
        "embedding_model": args.embedding_model,
        "embedding_batch_size": args.embedding_batch_size,
        "tasks": args.tasks,
        "counts": {},
        "empty_candidate_counts": {},
    }

    for task_name, records in outputs.items():
        validate_records(records, task_name)
        out_path = args.output_dir / f"{task_name}.jsonl"
        write_jsonl(out_path, records)
        summary["counts"][task_name] = len(records)
        summary["empty_candidate_counts"][task_name] = sum(1 for record in records if not record.get("candidate_ids"))

    write_json(args.output_dir / "summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
