"""Parameterized Cypher templates for deterministic K12 retrieval."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List

from retrieval.models import EvidenceNode, RelationPath, RetrievalFilters
from retrieval.store import Neo4jStore
from retrieval.text import response_properties

FILTER_CLAUSE = """
($subject IS NULL OR n.subject = $subject)
AND ($stage IS NULL OR n.stage = $stage)
AND ($grade IS NULL OR n.grade = $grade)
AND ($semester IS NULL OR n.semester = $semester)
AND ($edition IS NULL OR n.edition = $edition)
AND ($book_id IS NULL OR n.book_id = $book_id)
AND ($section_id IS NULL OR n.section_id = $section_id OR $section_id IN coalesce(n.section_ids, []))
"""


@dataclass
class CypherTemplates:
    store: Neo4jStore
    max_depth: int = 3
    timeout: float = 5.0

    def concept_detail(self, name: str, filters: RetrievalFilters, *, top_k: int, student_safe: bool) -> List[EvidenceNode]:
        return self.entity_detail(name, filters, labels=("Concept",), top_k=top_k, student_safe=student_safe)

    def entity_detail(
        self,
        name: str,
        filters: RetrievalFilters,
        *,
        labels: tuple[str, ...],
        top_k: int,
        student_safe: bool,
    ) -> List[EvidenceNode]:
        allowed = {"Concept", "Skill", "Exercise"}
        selected = tuple(label for label in labels if label in allowed)
        if not selected:
            return []
        label_clause = " OR ".join(f"n:{label}" for label in selected)
        rows = self.store.run_read(
            f"""
            MATCH (n)
            WHERE ({label_clause})
              AND ({FILTER_CLAUSE})
              AND (n.name CONTAINS $name OR $name IN coalesce(n.aliases, []))
            RETURN n.id AS id, labels(n)[0] AS label, n.name AS name, properties(n) AS properties, 1.0 AS score
            ORDER BY CASE
                       WHEN n.name = $name THEN 0
                       WHEN $name IN coalesce(n.aliases, []) THEN 1
                       ELSE 2
                     END,
                     size(n.name), n.name
            LIMIT $top_k
            """,
            {**filters.as_params(), **_null_filters(filters), "name": name, "top_k": top_k},
            timeout=self.timeout,
        )
        return [_node_from_row(row, student_safe=student_safe) for row in rows]

    def textbook_locations(self, node_id: str, *, top_k: int) -> List[Dict[str, Any]]:
        return self.textbook_locations_for_nodes([node_id], top_k=top_k)

    def textbook_locations_for_nodes(
        self,
        node_ids: List[str],
        *,
        top_k: int,
    ) -> List[Dict[str, Any]]:
        bounded_ids = list(dict.fromkeys(node_id for node_id in node_ids if node_id))[
            : max(1, top_k)
        ]
        if not bounded_ids:
            return []
        rows = self.store.run_read(
            """
            UNWIND $node_ids AS requested_id
            MATCH (n {id: requested_id})
            OPTIONAL MATCH (n)-[:appears_in]->(s:Section)
            OPTIONAL MATCH (s)-[:is_part_of]->(section_chapter:Chapter)
            OPTIONAL MATCH (n)-[:appears_in]->(direct_chapter:Chapter)
            WITH n, s, coalesce(section_chapter, direct_chapter) AS c
            OPTIONAL MATCH (c)-[:is_part_of]->(b:Book)
            RETURN n.id AS node_id,
                   collect(DISTINCT {
                     section_id: s.id, section_name: s.name,
                     chapter_id: c.id, chapter_name: c.name,
                     book_id: coalesce(b.id, n.book_id), book_name: b.name,
                     grade: coalesce(b.grade, n.grade), semester: coalesce(b.semester, n.semester),
                     edition: coalesce(b.edition, n.edition)
                   }) AS locations
            ORDER BY node_id
            LIMIT $top_k
            """,
            {"node_ids": bounded_ids, "top_k": top_k},
            timeout=self.timeout,
        )
        return [
            item
            for row in rows
            for item in row.get("locations", [])
            if isinstance(item, dict) and any(item.values())
        ]

    def prerequisites(self, name: str, filters: RetrievalFilters, *, top_k: int, depth: int, student_safe: bool) -> List[RelationPath]:
        return self._path_query(name, filters, direction="incoming", top_k=top_k, depth=depth, student_safe=student_safe)

    def successors(self, name: str, filters: RetrievalFilters, *, top_k: int, depth: int, student_safe: bool) -> List[RelationPath]:
        return self._path_query(name, filters, direction="outgoing", top_k=top_k, depth=depth, student_safe=student_safe)

    def exercises_for(self, name: str, filters: RetrievalFilters, *, top_k: int, student_safe: bool) -> List[EvidenceNode]:
        params = {**filters.as_params(), **_null_filters(filters), "name": name, "top_k": top_k}
        rows = self.store.run_read(
            f"""
            MATCH (n)
            WHERE (n:Concept OR n:Skill)
              AND ({FILTER_CLAUSE})
              AND (n.name CONTAINS $name OR $name IN coalesce(n.aliases, []))
            WITH n
            ORDER BY CASE
                       WHEN n.name = $name THEN 0
                       WHEN $name IN coalesce(n.aliases, []) THEN 1
                       ELSE 2
                     END,
                     size(n.name), n.name
            LIMIT 1
            MATCH (e:Exercise)-[:tests_concept|tests_skill]->(n)
            WHERE ($subject IS NULL OR e.subject = $subject)
              AND ($stage IS NULL OR e.stage = $stage)
              AND ($grade IS NULL OR e.grade = $grade)
              AND ($semester IS NULL OR e.semester = $semester)
              AND ($edition IS NULL OR e.edition = $edition)
              AND ($book_id IS NULL OR e.book_id = $book_id)
              AND ($section_id IS NULL OR e.section_id = $section_id OR $section_id IN coalesce(e.section_ids, []))
              AND ($difficulty IS NULL OR e.difficulty = $difficulty)
              AND ($exercise_type IS NULL OR e.type = $exercise_type)
            RETURN DISTINCT e.id AS id, labels(e)[0] AS label, e.name AS name,
                   properties(e) AS properties, 1.0 AS score
            ORDER BY score DESC, id
            LIMIT $top_k
            """,
            params,
            timeout=self.timeout,
        )
        return [_node_from_row(row, student_safe=student_safe) for row in rows]

    def _path_query(
        self,
        name: str,
        filters: RetrievalFilters,
        *,
        direction: str,
        top_k: int,
        depth: int,
        student_safe: bool,
    ) -> List[RelationPath]:
        actual_depth = max(1, min(depth, self.max_depth))
        pattern = (
            f"(m)-[rels:prerequisites_for*1..{actual_depth}]->(n)"
            if direction == "incoming"
            else f"(n)-[rels:prerequisites_for*1..{actual_depth}]->(m)"
        )
        rows = self.store.run_read(
            f"""
            MATCH (n)
            WHERE (n:Concept OR n:Skill)
              AND ({FILTER_CLAUSE})
              AND (n.name CONTAINS $name OR $name IN coalesce(n.aliases, []))
            WITH n
            ORDER BY CASE
                       WHEN n.name = $name THEN 0
                       WHEN $name IN coalesce(n.aliases, []) THEN 1
                       ELSE 2
                     END,
                     size(n.name), n.name
            LIMIT 1
            MATCH p = {pattern}
            WHERE (m:Concept OR m:Skill)
            RETURN n.id AS start_id, m.id AS end_id,
                   [r IN rels | type(r)] AS relationships,
                   [x IN nodes(p) | {{id: x.id, label: labels(x)[0], name: x.name, properties: properties(x), score: 1.0}}] AS nodes
            ORDER BY length(p), m.name
            LIMIT $top_k
            """,
            {**filters.as_params(), **_null_filters(filters), "name": name, "top_k": top_k},
            timeout=self.timeout,
        )
        return [
            RelationPath(
                start_id=str(row.get("start_id", "")),
                end_id=str(row.get("end_id", "")),
                relationships=list(row.get("relationships", [])),
                nodes=[_node_from_row(node, student_safe=student_safe) for node in row.get("nodes", [])],
            )
            for row in rows
        ]


def _null_filters(filters: RetrievalFilters) -> Dict[str, Any]:
    values = filters.as_params()
    out = {
        key: values.get(key)
        for key in (
            "subject",
            "stage",
            "grade",
            "semester",
            "edition",
            "exercise_type",
            "difficulty",
            "book_id",
            "section_id",
        )
    }
    return out


def _node_from_row(row: Dict[str, Any], *, student_safe: bool) -> EvidenceNode:
    label = str(row.get("label", ""))
    props = row.get("properties", {}) if isinstance(row.get("properties"), dict) else {}
    props = response_properties(label, props, student_safe=student_safe)
    return EvidenceNode(
        id=str(row.get("id", "")),
        label=label,
        name=str(row.get("name", "")),
        score=float(row.get("score", 0.0) or 0.0),
        properties=dict(props),
    )
