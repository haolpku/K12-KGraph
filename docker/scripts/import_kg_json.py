#!/usr/bin/env python3
"""Import K12-KGraph JSON into Neo4j with retrieval-friendly text properties."""

from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
from typing import Any

NODE_LABELS = {"Book", "Chapter", "Section", "Concept", "Skill", "Experiment", "Exercise"}
EDGE_TYPES = {
    "is_a",
    "prerequisites_for",
    "relates_to",
    "verifies",
    "tests_concept",
    "tests_skill",
    "appears_in",
    "leads_to",
    "is_part_of",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kg-json", required=True, help="Graph JSON file or directory containing nodes.json and edges.json")
    parser.add_argument("--neo4j-uri", default=os.getenv("NEO4J_URI", "bolt://localhost:7687"))
    parser.add_argument("--neo4j-user", default=os.getenv("NEO4J_USER", "neo4j"))
    parser.add_argument("--neo4j-password", default=os.getenv("NEO4J_PASSWORD"))
    parser.add_argument("--neo4j-database", default=os.getenv("NEO4J_DATABASE", "neo4j"))
    parser.add_argument("--subject", default=os.getenv("KG_SUBJECT", "数学"))
    parser.add_argument("--stage", default=os.getenv("KG_STAGE", "小学"))
    parser.add_argument("--edition", default=os.getenv("KG_EDITION", "人教版"))
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument(
        "--drop-embeddings",
        action=argparse.BooleanOptionalAction,
        default=os.getenv("KG_DROP_EMBEDDINGS", "true").lower()
        in {"1", "true", "yes", "on"},
        help="Remove source embedding values so the target provider can regenerate them",
    )
    return parser.parse_args()


def load_graph(path: Path) -> dict[str, list[dict[str, Any]]]:
    if path.is_dir():
        nodes = json.loads((path / "nodes.json").read_text(encoding="utf-8"))
        edges = json.loads((path / "edges.json").read_text(encoding="utf-8"))
        return {"nodes": nodes, "edges": edges}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("nodes"), list) or not isinstance(data.get("edges"), list):
        raise ValueError(f"{path} must contain top-level nodes and edges lists")
    return {"nodes": data["nodes"], "edges": data["edges"]}


def flatten_properties(
    node: dict[str, Any],
    subject: str,
    stage: str,
    edition: str,
    *,
    drop_embeddings: bool = False,
) -> dict[str, Any]:
    props = dict(node.get("properties") or {})
    if drop_embeddings:
        props.pop("embedding", None)
    props["id"] = str(node["id"])
    props["name"] = str(node.get("name") or props.get("name") or node["id"])
    props.setdefault("subject", subject)
    props.setdefault("stage", stage)
    props.setdefault("edition", edition)
    props.setdefault("book_id", infer_book_id(props["id"]))
    grade, semester = infer_grade_semester(props["id"])
    if grade:
        props.setdefault("grade", grade)
    if semester:
        props.setdefault("semester", semester)
    props["search_text"] = build_search_text(str(node.get("label")), props)
    return {k: normalize_value(v) for k, v in props.items() if v not in (None, "", [], {})}


def apply_location_metadata(nodes: list[dict[str, Any]], edges: list[dict[str, Any]]) -> None:
    section_ids: dict[str, list[str]] = {}
    for edge in edges:
        if edge.get("type") != "appears_in" or not edge.get("source") or not edge.get("target"):
            continue
        values = section_ids.setdefault(str(edge["source"]), [])
        target = str(edge["target"])
        if target not in values:
            values.append(target)
    for node in nodes:
        values = section_ids.get(str(node.get("id")), [])
        if not values:
            continue
        props = node["props"]
        props["section_ids"] = values
        props.setdefault("section_id", values[0])
        props.setdefault("chapter_id", values[0].rsplit("_s", 1)[0])


def normalize_value(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, list):
        return [normalize_value(item) for item in value if item not in (None, "", [], {})]
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def build_search_text(label: str, props: dict[str, Any]) -> str:
    keys_by_label = {
        "Concept": ["name", "definition", "formula", "aliases", "examples", "importance", "unit"],
        "Skill": ["name", "description", "examples", "importance"],
        "Exercise": ["name", "stem", "type", "difficulty"],
        "Experiment": ["name", "process", "phenomena", "conclusion", "instruments"],
        "Book": ["name"],
        "Chapter": ["name"],
        "Section": ["name"],
    }
    parts: list[str] = []
    for key in keys_by_label.get(label, ["name"]):
        value = props.get(key)
        if isinstance(value, list):
            parts.extend(str(item) for item in value)
        elif value not in (None, ""):
            parts.append(str(value))
    return " ".join(parts)


def infer_book_id(node_id: str) -> str:
    parts = str(node_id).split("_")
    return "_".join(parts[:3]) if len(parts) >= 3 else str(node_id)


def infer_grade_semester(node_id: str) -> tuple[str, str]:
    match = re.search(r"_(\d+)([ab])_", f"_{node_id}_")
    if not match:
        return "", ""
    semester = "上册" if match.group(2) == "a" else "下册"
    return match.group(1), semester


def chunks(items: list[dict[str, Any]], size: int) -> list[list[dict[str, Any]]]:
    return [items[idx : idx + size] for idx in range(0, len(items), size)]


def merge_nodes(session: Any, nodes: list[dict[str, Any]], batch_size: int) -> None:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for node in nodes:
        label = str(node.get("label"))
        if label in NODE_LABELS and node.get("id"):
            grouped.setdefault(label, []).append(node)
    for label, rows in grouped.items():
        query = f"UNWIND $rows AS row MERGE (n:{label} {{id: row.id}}) SET n += row.props"
        for batch in chunks(rows, batch_size):
            session.run(query, rows=[{"id": row["id"], "props": row["props"]} for row in batch]).consume()


def merge_edges(session: Any, edges: list[dict[str, Any]], batch_size: int) -> None:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for edge in expand_edges(edges):
        edge_type = str(edge.get("type"))
        if edge_type in EDGE_TYPES and edge.get("source") and edge.get("target"):
            props = dict(edge.get("properties") or {})
            props.setdefault("source_name", edge.get("source_name"))
            props.setdefault("target_name", edge.get("target_name"))
            grouped.setdefault(edge_type, []).append(
                {
                    "source": str(edge["source"]),
                    "target": str(edge["target"]),
                    "props": {k: normalize_value(v) for k, v in props.items() if v not in (None, "", [], {})},
                }
            )
    for edge_type, rows in grouped.items():
        query = f"""
        UNWIND $rows AS row
        MATCH (source {{id: row.source}})
        MATCH (target {{id: row.target}})
        MERGE (source)-[r:{edge_type}]->(target)
        SET r += row.props
        """
        for batch in chunks(rows, batch_size):
            session.run(query, rows=batch).consume()


def expand_edges(edges: list[dict[str, Any]]) -> list[dict[str, Any]]:
    expanded: list[dict[str, Any]] = []
    for edge in edges:
        targets = edge.get("target_name_to_ids")
        if isinstance(targets, list):
            for target in targets:
                if not isinstance(target, dict) or not target.get("target"):
                    continue
                copied = dict(edge)
                copied.pop("target_name_to_ids", None)
                copied["target"] = target["target"]
                copied["target_name"] = target.get("target_name")
                copied.setdefault("source_name", edge.get("source_name") or edge.get("source_stem"))
                expanded.append(copied)
            continue
        expanded.append(edge)
    return expanded


def main() -> int:
    args = parse_args()
    if not args.neo4j_password:
        raise SystemExit("Set --neo4j-password or NEO4J_PASSWORD")

    from neo4j import GraphDatabase

    graph = load_graph(Path(args.kg_json))
    nodes = [
        {
            **node,
            "props": flatten_properties(
                node,
                args.subject,
                args.stage,
                args.edition,
                drop_embeddings=args.drop_embeddings,
            ),
        }
        for node in graph["nodes"]
    ]

    expanded_edges = expand_edges(graph["edges"])
    apply_location_metadata(nodes, expanded_edges)

    with GraphDatabase.driver(args.neo4j_uri, auth=(args.neo4j_user, args.neo4j_password)) as driver:
        driver.verify_connectivity()
        with driver.session(database=args.neo4j_database) as session:
            merge_nodes(session, nodes, args.batch_size)
            merge_edges(session, expanded_edges, args.batch_size)

    print(f"Imported {len(nodes)} nodes and {len(expanded_edges)} edges")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
