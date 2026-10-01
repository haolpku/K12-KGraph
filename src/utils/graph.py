"""Small helpers for working with ``{"nodes": [...], "edges": [...]}`` graphs."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Iterable, List, Sequence, Set, Union

from utils.io import read_json


def load_graph(path: Union[str, Path]) -> Dict[str, Any]:
    """Read a graph file, always returning ``nodes`` / ``edges`` lists."""
    data = read_json(path)
    if not isinstance(data, dict):
        raise ValueError(f"graph file must contain a JSON object: {path}")
    nodes = data.get("nodes")
    edges = data.get("edges")
    data["nodes"] = nodes if isinstance(nodes, list) else []
    data["edges"] = edges if isinstance(edges, list) else []
    return data


def index_nodes(graph: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    return {str(node["id"]): node for node in graph.get("nodes", []) if isinstance(node, dict) and node.get("id")}


def node_ids(graph: Dict[str, Any]) -> Set[str]:
    return set(index_nodes(graph))


def dangling_edges(graph: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Return edges whose source or target is not a node of the same graph.

    Self-consistency check: a graph is only usable downstream when every edge
    connects two nodes that exist in it.
    """
    known = node_ids(graph)
    dangling: List[Dict[str, Any]] = []
    for edge in graph.get("edges", []):
        if not isinstance(edge, dict):
            continue
        source = str(edge.get("source", ""))
        target = str(edge.get("target", ""))
        missing = [end for end in (source, target) if end not in known]
        if missing:
            dangling.append({"edge": edge, "missing": missing})
    return dangling


def filter_nodes(nodes: Iterable[Dict[str, Any]], labels: Sequence[str]) -> List[Dict[str, Any]]:
    wanted = set(labels)
    return [node for node in nodes if isinstance(node, dict) and str(node.get("label", "")) in wanted]
