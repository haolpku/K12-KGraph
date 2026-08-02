"""Lazy adapters for the official ``neo4j-graphrag`` retrievers.

The core service keeps a small driver-based retriever for deterministic tests.
Deployments that want the official GraphRAG orchestration can construct the
retrievers here without changing the graph schema or index names.
"""

from __future__ import annotations

from typing import Any, Optional

from retrieval.embeddings import Embedder as LocalEmbedder

INDEXES = {
    "Concept": ("concept_vector", "concept_fulltext"),
    "Skill": ("skill_vector", "skill_fulltext"),
    "Exercise": ("exercise_vector", "exercise_fulltext"),
}

K12_SCHEMA = """
Node properties:
Concept {id: STRING, name: STRING, aliases: LIST, definition: STRING, formula: STRING,
         subject: STRING, stage: STRING, grade: STRING, semester: STRING, edition: STRING}
Skill {id: STRING, name: STRING, description: STRING, subject: STRING, stage: STRING,
       grade: STRING, semester: STRING, edition: STRING}
Exercise {id: STRING, name: STRING, stem: STRING, type: STRING, difficulty: INTEGER,
          subject: STRING, stage: STRING, grade: STRING, semester: STRING, edition: STRING}
Book {id: STRING, name: STRING, grade: STRING, semester: STRING, edition: STRING}
Chapter {id: STRING, name: STRING}
Section {id: STRING, name: STRING}
Relationships:
(:Concept|Skill)-[:prerequisites_for]->(:Concept|Skill)
(:Exercise)-[:tests_concept]->(:Concept)
(:Exercise)-[:tests_skill]->(:Skill)
(:Concept|Skill|Exercise)-[:appears_in]->(:Section|Chapter)
(:Section)-[:is_part_of]->(:Chapter)
(:Chapter)-[:is_part_of]->(:Book)
""".strip()


def make_hybrid_cypher_retriever(
    driver: Any,
    *,
    label: str,
    embedder: LocalEmbedder,
    database: Optional[str] = None,
) -> Any:
    """Create Neo4j's official HybridCypherRetriever for one K12 node label."""

    try:
        from neo4j_graphrag.embeddings.base import Embedder as GraphRAGEmbedder
        from neo4j_graphrag.retrievers import HybridCypherRetriever
        from neo4j_graphrag.types import RetrieverResultItem
    except ImportError as exc:  # pragma: no cover - dependency error path
        raise RuntimeError("Install neo4j-graphrag>=1.18,<2 to enable GraphRAG") from exc

    if label not in INDEXES:
        raise ValueError(f"unsupported GraphRAG label: {label}")

    class Adapter(GraphRAGEmbedder):
        def embed_query(self, text: str) -> list[float]:
            return list(embedder.embed([text])[0])

    vector_index, fulltext_index = INDEXES[label]
    exercise_filters = """
      AND ($difficulty IS NULL OR node.difficulty = $difficulty)
      AND ($exercise_type IS NULL OR node.type = $exercise_type)
    """ if label == "Exercise" else ""

    def format_record(record: Any) -> Any:
        data = record.data()
        return RetrieverResultItem(
            content=data.get("node", {}),
            metadata={
                "score": float(data.get("score", 0.0) or 0.0),
                "locations": data.get("locations", []),
            },
        )

    return HybridCypherRetriever(
        driver=driver,
        vector_index_name=vector_index,
        fulltext_index_name=fulltext_index,
        embedder=Adapter(),
        result_formatter=format_record,
        neo4j_database=database,
        retrieval_query=f"""
        WITH node, score
        WHERE ($subject IS NULL OR node.subject = $subject)
          AND ($stage IS NULL OR node.stage = $stage)
          AND ($grade IS NULL OR node.grade = $grade)
          AND ($semester IS NULL OR node.semester = $semester)
          AND ($edition IS NULL OR node.edition = $edition)
          AND ($book_id IS NULL OR node.book_id = $book_id)
          AND (
            $section_id IS NULL
            OR node.section_id = $section_id
            OR $section_id IN coalesce(node.section_ids, [])
          )
          {exercise_filters}
        OPTIONAL MATCH (node)-[:appears_in]->(section:Section)
        OPTIONAL MATCH (section)-[:is_part_of]->(section_chapter:Chapter)
        OPTIONAL MATCH (node)-[:appears_in]->(direct_chapter:Chapter)
        WITH node, score, section, coalesce(section_chapter, direct_chapter) AS chapter
        OPTIONAL MATCH (chapter)-[:is_part_of]->(book:Book)
        RETURN {{
          id: node.id,
          label: labels(node)[0],
          name: node.name,
          properties: node {{
            .id, .name, .aliases, .definition, .formula, .examples, .importance,
            .description, .stem, .type, .difficulty, .answer, .analysis,
            .subject, .stage, .grade, .semester, .edition,
            .book_id, .section_id, .section_ids, .chapter_id
          }}
        }} AS node,
        score,
        collect(DISTINCT {{
          section_id: section.id, section_name: section.name,
          chapter_id: chapter.id, chapter_name: chapter.name,
          book_id: coalesce(book.id, node.book_id), book_name: book.name
        }}) AS locations
        """,
    )
