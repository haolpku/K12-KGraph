"""Full-text, vector, hybrid, and graph-expansion retrievers."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional

from retrieval.embeddings import Embedder
from retrieval.graphrag_adapter import make_hybrid_cypher_retriever
from retrieval.models import EvidenceNode, RetrievalFilters
from retrieval.store import Neo4jStore
from retrieval.text import response_properties

INDEX_BY_LABEL = {
    "Concept": ("concept_fulltext", "concept_vector", "embedding"),
    "Skill": ("skill_fulltext", "skill_vector", "embedding"),
    "Exercise": ("exercise_fulltext", "exercise_vector", "embedding"),
}
LOGGER = logging.getLogger(__name__)


@dataclass
class FulltextRetriever:
    store: Neo4jStore
    timeout: float = 5.0

    def search(self, query: str, *, labels: Iterable[str], filters: RetrievalFilters, top_k: int, student_safe: bool) -> List[EvidenceNode]:
        results: List[EvidenceNode] = []
        for label in labels:
            index = INDEX_BY_LABEL[label][0]
            rows = self.store.run_read(
                f"""
                CALL db.index.fulltext.queryNodes($index, $query, {{limit: $limit}}) YIELD node, score
                WHERE node:{label}
                  AND ($subject IS NULL OR node.subject = $subject)
                  AND ($stage IS NULL OR node.stage = $stage)
                  AND ($grade IS NULL OR node.grade = $grade)
                  AND ($semester IS NULL OR node.semester = $semester)
                  AND ($edition IS NULL OR node.edition = $edition)
                  AND ($book_id IS NULL OR node.book_id = $book_id)
                  AND ($section_id IS NULL OR node.section_id = $section_id OR $section_id IN coalesce(node.section_ids, []))
                  AND ($difficulty IS NULL OR node.difficulty = $difficulty)
                  AND ($exercise_type IS NULL OR node.type = $exercise_type)
                RETURN node.id AS id, labels(node)[0] AS label, node.name AS name,
                       properties(node) AS properties, score AS score
                ORDER BY score DESC
                LIMIT $top_k
                """,
                {
                    **_filter_params(filters),
                    "index": index,
                    "query": _lucene_escape(query),
                    "limit": top_k * 3,
                    "top_k": top_k,
                },
                timeout=self.timeout,
            )
            results.extend(_nodes(rows, student_safe=student_safe))
        return _dedupe_rank(results, top_k)


@dataclass
class VectorRetriever:
    store: Neo4jStore
    embedder: Embedder
    timeout: float = 5.0

    def search(
        self,
        query: str,
        *,
        labels: Iterable[str],
        filters: RetrievalFilters,
        top_k: int,
        student_safe: bool,
        query_vector: Optional[List[float]] = None,
    ) -> List[EvidenceNode]:
        vector = list(query_vector) if query_vector is not None else self.embedder.embed([query])[0]
        results: List[EvidenceNode] = []
        for label in labels:
            _, index, _prop = INDEX_BY_LABEL[label]
            rows = self.store.run_read(
                f"""
                CALL db.index.vector.queryNodes($index, $limit, $embedding) YIELD node, score
                WHERE node:{label}
                  AND ($subject IS NULL OR node.subject = $subject)
                  AND ($stage IS NULL OR node.stage = $stage)
                  AND ($grade IS NULL OR node.grade = $grade)
                  AND ($semester IS NULL OR node.semester = $semester)
                  AND ($edition IS NULL OR node.edition = $edition)
                  AND ($book_id IS NULL OR node.book_id = $book_id)
                  AND ($section_id IS NULL OR node.section_id = $section_id OR $section_id IN coalesce(node.section_ids, []))
                  AND ($difficulty IS NULL OR node.difficulty = $difficulty)
                  AND ($exercise_type IS NULL OR node.type = $exercise_type)
                RETURN node.id AS id, labels(node)[0] AS label, node.name AS name,
                       properties(node) AS properties, score AS score
                ORDER BY score DESC
                LIMIT $top_k
                """,
                {**_filter_params(filters), "index": index, "embedding": vector, "limit": top_k * 5, "top_k": top_k},
                timeout=self.timeout,
            )
            results.extend(_nodes(rows, student_safe=student_safe))
        return _dedupe_rank(results, top_k)


@dataclass
class HybridRetriever:
    fulltext: FulltextRetriever
    vector: Optional[VectorRetriever] = None

    def search(
        self,
        query: str,
        *,
        labels: Iterable[str],
        filters: RetrievalFilters,
        top_k: int,
        student_safe: bool,
        query_vector: Optional[List[float]] = None,
    ) -> List[EvidenceNode]:
        fulltext_hits = self.fulltext.search(query, labels=labels, filters=filters, top_k=top_k, student_safe=student_safe)
        vector_hits = (
            self.vector.search(
                query,
                labels=labels,
                filters=filters,
                top_k=top_k,
                student_safe=student_safe,
                query_vector=query_vector,
            )
            if self.vector
            else []
        )
        by_id: Dict[str, EvidenceNode] = {}
        for rank, hit in enumerate(fulltext_hits, start=1):
            hit.score = 1.0 / (60 + rank)
            by_id[hit.id] = hit
        for rank, hit in enumerate(vector_hits, start=1):
            if hit.id in by_id:
                by_id[hit.id].score += 1.0 / (60 + rank)
            else:
                hit.score = 1.0 / (60 + rank)
                by_id[hit.id] = hit
        return sorted(by_id.values(), key=lambda item: item.score, reverse=True)[:top_k]


@dataclass
class GraphRAGHybridRetriever:
    """Official Neo4j GraphRAG hybrid retrieval with a local safe fallback."""

    store: Neo4jStore
    embedder: Embedder
    fallback: HybridRetriever
    database: Optional[str] = None

    def __post_init__(self) -> None:
        self._retrievers: Dict[str, Any] = {}

    def search(
        self,
        query: str,
        *,
        labels: Iterable[str],
        filters: RetrievalFilters,
        top_k: int,
        student_safe: bool,
        warning_sink: Optional[List[str]] = None,
    ) -> List[EvidenceNode]:
        query_vector: Optional[List[float]] = None
        try:
            results: List[EvidenceNode] = []
            candidate_k = min(max(top_k * 5, top_k), 100)
            query_vector = list(self.embedder.embed([query])[0])
            for label in labels:
                retriever = self._retrievers.get(label)
                if retriever is None:
                    retriever = make_hybrid_cypher_retriever(
                        self.store.driver,
                        label=label,
                        embedder=self.embedder,
                        database=self.database,
                    )
                    self._retrievers[label] = retriever
                response = retriever.search(
                    query_text=_lucene_escape(query),
                    query_vector=query_vector,
                    top_k=candidate_k,
                    effective_search_ratio=5,
                    query_params=_filter_params(filters),
                )
                for item in response.items:
                    content = item.content if isinstance(item.content, dict) else {}
                    metadata = item.metadata if isinstance(item.metadata, dict) else {}
                    label_name = str(content.get("label", label))
                    props = content.get("properties", {})
                    if not isinstance(props, dict):
                        props = {}
                    results.append(
                        EvidenceNode(
                            id=str(content.get("id", "")),
                            label=label_name,
                            name=str(content.get("name", "")),
                            score=float(metadata.get("score", 0.0) or 0.0),
                            properties=response_properties(
                                label_name,
                                props,
                                student_safe=student_safe,
                            ),
                        )
                    )
            official_results = _dedupe_rank(results, top_k)
            local_results = self.fallback.search(
                query,
                labels=labels,
                filters=filters,
                top_k=top_k,
                student_safe=student_safe,
                query_vector=query_vector,
            )
            return _weighted_rrf(
                ((official_results, 2.0), (local_results, 1.0)),
                top_k,
            )
        except Exception:
            LOGGER.exception("Neo4j GraphRAG hybrid retrieval failed; using local fallback")
            fallback_results = self.fallback.search(
                query,
                labels=labels,
                filters=filters,
                top_k=top_k,
                student_safe=student_safe,
                query_vector=query_vector,
            )
            if warning_sink is not None:
                warning_sink.append("GRAPHRAG_FALLBACK_LOCAL")
            return fallback_results


@dataclass
class GraphExpansionRetriever:
    store: Neo4jStore
    timeout: float = 5.0

    def expand(self, node_ids: List[str], *, top_k: int, student_safe: bool) -> List[EvidenceNode]:
        if not node_ids:
            return []
        rows = self.store.run_read(
            """
            MATCH (n)
            WHERE n.id IN $node_ids
            OPTIONAL MATCH (n)-[:appears_in|tests_concept|tests_skill|relates_to|is_a|prerequisites_for]-(m)
            WHERE m.id IS NOT NULL
            RETURN DISTINCT m.id AS id, labels(m)[0] AS label, m.name AS name, properties(m) AS properties, 0.5 AS score
            LIMIT $top_k
            """,
            {"node_ids": node_ids, "top_k": top_k},
            timeout=self.timeout,
        )
        return _nodes(rows, student_safe=student_safe)


def _filter_params(filters: RetrievalFilters) -> Dict[str, Any]:
    values = filters.as_params()
    return {
        key: values.get(key)
        for key in (
            "subject",
            "stage",
            "grade",
            "semester",
            "edition",
            "book_id",
            "section_id",
            "difficulty",
            "exercise_type",
        )
    }


def _nodes(rows: Iterable[Dict[str, Any]], *, student_safe: bool) -> List[EvidenceNode]:
    out: List[EvidenceNode] = []
    for row in rows:
        label = str(row.get("label", ""))
        props = row.get("properties", {}) if isinstance(row.get("properties"), dict) else {}
        props = response_properties(label, props, student_safe=student_safe)
        out.append(EvidenceNode(str(row.get("id", "")), label, str(row.get("name", "")), float(row.get("score", 0.0) or 0.0), dict(props)))
    return out


def _dedupe_rank(items: Iterable[EvidenceNode], top_k: int) -> List[EvidenceNode]:
    by_id: Dict[str, EvidenceNode] = {}
    for item in items:
        old = by_id.get(item.id)
        if old is None or item.score > old.score:
            by_id[item.id] = item
    return sorted(by_id.values(), key=lambda item: item.score, reverse=True)[:top_k]


def _weighted_rrf(
    ranked_lists: Iterable[tuple[List[EvidenceNode], float]],
    top_k: int,
) -> List[EvidenceNode]:
    nodes: Dict[str, EvidenceNode] = {}
    scores: Dict[str, float] = {}
    for ranked, weight in ranked_lists:
        for rank, node in enumerate(ranked, start=1):
            nodes.setdefault(node.id, node)
            scores[node.id] = scores.get(node.id, 0.0) + weight / (60 + rank)
    for node_id, score in scores.items():
        nodes[node_id].score = score
    return sorted(nodes.values(), key=lambda node: node.score, reverse=True)[:top_k]


def _lucene_escape(query: str) -> str:
    text = re.sub(r'([-+\\!(){}\[\]^"~*?:/]|&&|\|\|)', r"\\\1", str(query).strip())
    return text or "*"
