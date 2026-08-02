#!/usr/bin/env python3
"""Load an enriched K12 graph JSON into Neo4j."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from utils.bootstrap import ensure_src_on_path

ensure_src_on_path(__file__)

from retrieval.data_prep import expand_edges  # noqa: E402
from retrieval.indexes import all_statements, apply_indexes  # noqa: E402
from retrieval.settings import RetrievalSettings  # noqa: E402
from retrieval.store import Neo4jStore  # noqa: E402
from utils.io import read_json  # noqa: E402

REL_TYPES = {
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
NODE_LABELS = {"Book", "Chapter", "Section", "Concept", "Skill", "Experiment", "Exercise"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", default="data/retrieval/graph_enriched.json")
    parser.add_argument("--create-indexes", action="store_true")
    parser.add_argument("--batch-size", type=int, default=500)
    return parser.parse_args()


def load_graph(path: str) -> Dict[str, Any]:
    payload = read_json(path)
    if not isinstance(payload, dict) or "nodes" not in payload or "edges" not in payload:
        raise ValueError("input must be a graph object with nodes and edges")
    return payload


def import_nodes(store: Neo4jStore, nodes: List[Dict[str, Any]], *, batch_size: int) -> None:
    labels = {str(node.get("label", "")) for node in nodes if node.get("label")}
    unsupported = sorted(labels - NODE_LABELS)
    if unsupported:
        raise ValueError(f"unsupported node labels: {', '.join(unsupported)}")
    for label in sorted(labels):
        selected = [_node_payload(node) for node in nodes if node.get("label") == label]
        for batch in _batches(selected, batch_size):
            store.run_write(
                f"""
                UNWIND $rows AS row
                MERGE (n:{label} {{id: row.id}})
                SET n += row.properties
                SET n.name = row.name
                """,
                {"rows": batch},
            )


def import_edges(store: Neo4jStore, edges: List[Dict[str, Any]], *, batch_size: int) -> None:
    edges = expand_edges(edges)
    for rel_type in sorted({str(edge.get("type", "")) for edge in edges if edge.get("type") in REL_TYPES}):
        selected = [_edge_payload(edge) for edge in edges if edge.get("type") == rel_type]
        for batch in _batches(selected, batch_size):
            store.run_write(
                f"""
                UNWIND $rows AS row
                MATCH (s {{id: row.source}})
                MATCH (t {{id: row.target}})
                MERGE (s)-[r:{rel_type}]->(t)
                SET r += row.properties
                """,
                {"rows": batch},
            )


def _node_payload(node: Dict[str, Any]) -> Dict[str, Any]:
    props = _neo4j_properties(
        node.get("properties", {}) if isinstance(node.get("properties"), dict) else {}
    )
    props["id"] = str(node.get("id", ""))
    props["name"] = str(node.get("name", ""))
    return {"id": props["id"], "name": props["name"], "properties": props}


def _edge_payload(edge: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "source": str(edge.get("source", "")),
        "target": str(edge.get("target", "")),
        "properties": _neo4j_properties(
            edge.get("properties", {}) if isinstance(edge.get("properties"), dict) else {}
        ),
    }


def _neo4j_properties(properties: Dict[str, Any]) -> Dict[str, Any]:
    """Preserve nested source evidence as JSON because Neo4j properties are flat."""

    out: Dict[str, Any] = {}
    for key, value in properties.items():
        if value is None:
            continue
        if isinstance(value, dict) or (
            isinstance(value, list)
            and any(isinstance(item, (dict, list)) for item in value)
        ):
            out[key] = json.dumps(value, ensure_ascii=False, sort_keys=True)
        else:
            out[key] = value
    return out


def _batches(items: List[Dict[str, Any]], size: int) -> Iterable[List[Dict[str, Any]]]:
    for idx in range(0, len(items), size):
        yield items[idx : idx + size]


def main() -> None:
    args = parse_args()
    settings = RetrievalSettings.from_env()
    graph = load_graph(args.input)
    with Neo4jStore.connect(settings) as store:
        if args.create_indexes:
            apply_indexes(store, all_statements(settings))
        import_nodes(store, graph["nodes"], batch_size=args.batch_size)
        import_edges(store, graph["edges"], batch_size=args.batch_size)


if __name__ == "__main__":
    main()
