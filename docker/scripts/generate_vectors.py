#!/usr/bin/env python3
"""Generate Neo4j vector properties for Concept, Skill, and Exercise nodes."""

from __future__ import annotations

import argparse
import os
from typing import Any

LABELS = ("Concept", "Skill", "Exercise")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--neo4j-uri", default=os.getenv("NEO4J_URI", "bolt://localhost:7687"))
    parser.add_argument("--neo4j-user", default=os.getenv("NEO4J_USER", "neo4j"))
    parser.add_argument("--neo4j-password", default=os.getenv("NEO4J_PASSWORD"))
    parser.add_argument("--neo4j-database", default=os.getenv("NEO4J_DATABASE", "neo4j"))
    parser.add_argument("--model", default=os.getenv("EMBEDDING_MODEL", "BAAI/bge-small-zh-v1.5"))
    parser.add_argument(
        "--provider",
        choices=("fastembed", "openai", "hash"),
        default=os.getenv("K12_RETRIEVAL_EMBEDDING_PROVIDER", "fastembed"),
    )
    parser.add_argument(
        "--dimension",
        type=int,
        default=int(
            os.getenv(
                "EMBEDDING_DIMENSIONS",
                os.getenv("EMBEDDING_DIMENSION", "512"),
            )
        ),
    )
    parser.add_argument(
        "--api-key",
        default=os.getenv("EMBEDDING_API_KEY")
        or os.getenv("K12_RETRIEVAL_EMBEDDING_API_KEY"),
    )
    parser.add_argument(
        "--base-url",
        default=os.getenv("EMBEDDING_BASE_URL")
        or os.getenv("K12_RETRIEVAL_EMBEDDING_BASE_URL")
        or "https://dashscope.aliyuncs.com/compatible-mode/v1",
    )
    parser.add_argument("--batch-size", type=int, default=int(os.getenv("VECTOR_BATCH_SIZE", "10")))
    parser.add_argument("--limit", type=int, default=0, help="Optional maximum nodes per label; 0 means no limit")
    parser.add_argument(
        "--replace",
        action="store_true",
        help="Regenerate existing embedding properties in the isolated database",
    )
    return parser.parse_args()


def fetch_rows(
    session: Any, label: str, limit: int, *, replace: bool
) -> list[dict[str, str]]:
    suffix = "LIMIT $limit" if limit > 0 else ""
    embedding_condition = "" if replace else "AND n.embedding IS NULL"
    query = f"""
    MATCH (n:{label})
    WHERE n.search_text IS NOT NULL {embedding_condition}
    RETURN n.id AS id, n.search_text AS text
    ORDER BY n.id
    {suffix}
    """
    return [dict(record) for record in session.run(query, limit=limit)]


def write_batch(session: Any, label: str, rows: list[dict[str, Any]]) -> None:
    query = f"""
    UNWIND $rows AS row
    MATCH (n:{label} {{id: row.id}})
    SET n.embedding = row.embedding
    """
    session.run(query, rows=rows).consume()


def main() -> int:
    args = parse_args()
    if not args.neo4j_password:
        raise SystemExit("Set --neo4j-password or NEO4J_PASSWORD")

    from neo4j import GraphDatabase

    from retrieval.embeddings import (
        DashScopeEmbedding,
        FastEmbedder,
        HashEmbedder,
    )

    if args.provider == "openai":
        if not args.api_key:
            raise SystemExit(
                "Set EMBEDDING_API_KEY for the OpenAI-compatible provider"
            )
        if args.batch_size > 10:
            raise SystemExit("OpenAI-compatible DashScope batch size must be <= 10")
        embedder = DashScopeEmbedding(
            args.model,
            args.dimension,
            api_key=args.api_key,
            base_url=args.base_url,
            timeout=2.0,
        )
    elif args.provider == "fastembed":
        embedder = FastEmbedder(args.model, args.dimension)
    else:
        embedder = HashEmbedder(args.dimension)
    total = 0
    with GraphDatabase.driver(args.neo4j_uri, auth=(args.neo4j_user, args.neo4j_password)) as driver:
        driver.verify_connectivity()
        with driver.session(database=args.neo4j_database) as session:
            for label in LABELS:
                rows = fetch_rows(
                    session, label, args.limit, replace=args.replace
                )
                for start in range(0, len(rows), args.batch_size):
                    batch = rows[start : start + args.batch_size]
                    vectors = embedder.embed([row["text"] for row in batch])
                    write_batch(
                        session,
                        label,
                        [
                            {"id": row["id"], "embedding": list(map(float, vector))}
                            for row, vector in zip(batch, vectors, strict=True)
                        ],
                    )
                    total += len(batch)
                print(f"{label}: generated {len(rows)} vectors")
    print(f"Generated {total} vectors")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
