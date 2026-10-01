#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Step 4: detect simple cycles in merged graphs for selected edge types.

Loads merged graph JSON from configured output directories and reports cycles on
``is_a`` / ``prerequisites_for`` relations (optionally persisting a workspace report).
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Set, Tuple

_SRC_DIR = Path(__file__).resolve().parents[1]
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from utils.config import load_config
from utils.io import read_json, write_json

TARGET_TYPES = ("is_a", "prerequisites_for")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check cycles in merged graph outputs")
    parser.add_argument("--config", default=None, help="Pipeline config path")
    parser.add_argument("--level", choices=["book", "subject_stage", "subject", "global"], default="book")
    parser.add_argument("--save-report", action="store_true", help="Save report into workspace/check_graph")
    return parser.parse_args()


def iter_graph_inputs(level: str, config_path: str | None) -> Iterable[Path]:
    config = load_config(config_path)
    if level == "book":
        yield from sorted(config.book_kg_dir.glob("*.json"))
    elif level == "subject_stage":
        yield from sorted(config.subject_stage_kg_dir.glob("*.json"))
    elif level == "subject":
        yield from sorted(config.subject_kg_dir.glob("*.json"))
    else:
        yield config.global_kg_dir / "edges.json"


def build_graph(edges: List[Any], edge_type: str) -> Dict[str, Set[str]]:
    graph: Dict[str, Set[str]] = defaultdict(set)
    for edge in edges:
        if not isinstance(edge, dict) or edge.get("type") != edge_type:
            continue
        source = edge.get("source")
        target = edge.get("target")
        if not isinstance(source, str) or not isinstance(target, str):
            continue
        source = source.strip()
        target = target.strip()
        if not source or not target:
            continue
        graph[source].add(target)
        if target not in graph:
            graph[target] = graph[target]
    return graph


def find_cycles(graph: Dict[str, Set[str]], max_cycles: int = 200) -> List[List[str]]:
    cycles: List[List[str]] = []
    seen_keys: Set[Tuple[str, ...]] = set()

    def canonicalize(cycle: List[str]) -> Tuple[str, ...]:
        core = cycle[:-1]
        if not core:
            return tuple()
        rotations = [tuple(core[i:] + core[:i]) for i in range(len(core))]
        return min(rotations)

    def dfs(start: str, current: str, stack: List[str], in_stack: Set[str]) -> None:
        if len(cycles) >= max_cycles:
            return
        for nxt in graph.get(current, set()):
            if nxt == start and len(stack) >= 2:
                cycle = stack + [start]
                key = canonicalize(cycle)
                if key and key not in seen_keys:
                    seen_keys.add(key)
                    cycles.append(cycle)
                continue
            if nxt in in_stack or nxt < start:
                continue
            in_stack.add(nxt)
            stack.append(nxt)
            dfs(start, nxt, stack, in_stack)
            stack.pop()
            in_stack.remove(nxt)

    for start in sorted(graph):
        dfs(start, start, [start], {start})
        if len(cycles) >= max_cycles:
            break
    return cycles


def load_edges(path: Path, level: str) -> List[Dict[str, Any]]:
    data = read_json(path)
    if level == "global":
        return data if isinstance(data, list) else []
    if isinstance(data, dict):
        edges = data.get("edges", [])
        return edges if isinstance(edges, list) else []
    return []


def check_file(path: Path, level: str) -> Dict[str, Any]:
    edges = load_edges(path, level)
    result: Dict[str, Any] = {"file": str(path)}
    for edge_type in TARGET_TYPES:
        graph = build_graph(edges, edge_type)
        cycles = find_cycles(graph)
        result[edge_type] = {
            "edge_count": sum(len(values) for values in graph.values()),
            "has_cycle": bool(cycles),
            "cycle_count": len(cycles),
            "cycles": cycles,
        }
    return result


def run_checks(config_path: Optional[str], level: str, save_report: bool) -> List[Dict[str, Any]]:
    paths = list(iter_graph_inputs(level, config_path))
    if not paths:
        raise FileNotFoundError(f"no inputs found for level={level}")

    reports = [check_file(path, level) for path in paths]
    print(json.dumps(reports, ensure_ascii=False, indent=2))

    if save_report:
        config = load_config(config_path)
        output_path = config.check_graph_workspace_dir / f"{level}_cycle_report.json"
        write_json(output_path, reports)
        print(f"saved report: {output_path}")

    return reports


def main() -> None:
    args = parse_args()
    run_checks(args.config, args.level, args.save_report)


if __name__ == "__main__":
    main()
