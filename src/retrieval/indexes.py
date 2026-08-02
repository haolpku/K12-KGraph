#!/usr/bin/env python3
"""Create Neo4j constraints, range indexes, full-text indexes, and vector indexes."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Iterable, List

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from utils.bootstrap import ensure_src_on_path

ensure_src_on_path(__file__)

from retrieval.settings import RetrievalSettings  # noqa: E402
from retrieval.store import Neo4jStore  # noqa: E402


def constraint_statements() -> List[str]:
    return [
        "CREATE CONSTRAINT concept_id_unique IF NOT EXISTS FOR (n:Concept) REQUIRE n.id IS UNIQUE",
        "CREATE CONSTRAINT skill_id_unique IF NOT EXISTS FOR (n:Skill) REQUIRE n.id IS UNIQUE",
        "CREATE CONSTRAINT exercise_id_unique IF NOT EXISTS FOR (n:Exercise) REQUIRE n.id IS UNIQUE",
        "CREATE CONSTRAINT book_id_unique IF NOT EXISTS FOR (n:Book) REQUIRE n.id IS UNIQUE",
        "CREATE CONSTRAINT chapter_id_unique IF NOT EXISTS FOR (n:Chapter) REQUIRE n.id IS UNIQUE",
        "CREATE CONSTRAINT section_id_unique IF NOT EXISTS FOR (n:Section) REQUIRE n.id IS UNIQUE",
    ]


def range_index_statements() -> List[str]:
    stmts: List[str] = []
    for label in ("Concept", "Skill", "Exercise"):
        for prop in ("name", "subject", "stage", "grade", "semester", "edition", "book_id", "section_id"):
            stmts.append(f"CREATE INDEX {label.lower()}_{prop}_idx IF NOT EXISTS FOR (n:{label}) ON (n.{prop})")
    return stmts


def fulltext_statements() -> List[str]:
    return [
        _fulltext_index("concept_fulltext", "Concept"),
        _fulltext_index("skill_fulltext", "Skill"),
        _fulltext_index("exercise_fulltext", "Exercise"),
    ]


def _fulltext_index(name: str, label: str) -> str:
    return (
        f"CREATE FULLTEXT INDEX {name} IF NOT EXISTS FOR (n:{label}) "
        "ON EACH [n.name, n.search_text] "
        "OPTIONS {indexConfig: {`fulltext.analyzer`: 'cjk'}}"
    )


def vector_statements(dimension: int) -> List[str]:
    return [
        _vector_index("concept_vector", "Concept", "embedding", dimension),
        _vector_index("skill_vector", "Skill", "embedding", dimension),
        _vector_index("exercise_vector", "Exercise", "embedding", dimension),
    ]


def _vector_index(name: str, label: str, prop: str, dimension: int) -> str:
    return (
        f"CREATE VECTOR INDEX {name} IF NOT EXISTS FOR (n:{label}) ON (n.{prop}) "
        f"OPTIONS {{indexConfig: {{`vector.dimensions`: {dimension}, `vector.similarity_function`: 'cosine'}}}}"
    )


def all_statements(settings: RetrievalSettings) -> List[str]:
    return constraint_statements() + range_index_statements() + fulltext_statements() + vector_statements(settings.embedding_dimension)


def apply_indexes(store: Neo4jStore, statements: Iterable[str]) -> None:
    for statement in statements:
        store.run_write(statement)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--print-only", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    settings = RetrievalSettings.from_env()
    statements = all_statements(settings)
    if args.print_only:
        print("\n".join(statements))
        return
    with Neo4jStore.connect(settings) as store:
        apply_indexes(store, statements)
        for row in store.run_read("SHOW INDEXES YIELD name, state RETURN name, state ORDER BY name"):
            print(row)


if __name__ == "__main__":
    main()
