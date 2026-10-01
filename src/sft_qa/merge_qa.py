#!/usr/bin/env python3
"""Merge per-task SFT QA JSONL parts into final ``node`` / ``edge`` / ``exercise`` files.

Reads ``workspace/sft_qa/<subject_stage>/parts`` and writes consolidated JSONL under
``final/`` for downstream packaging (e.g. ``build_train_jsonl.py``).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Dict, List

_SRC_DIR = Path(__file__).resolve().parents[1]
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from sft_qa.common import load_openai_env, resolve_workspace_root  # noqa: E402
from utils.config import load_config  # noqa: E402
from utils.io import read_jsonl, write_jsonl  # noqa: E402


NODE_PARTS = ["node_concept.jsonl", "node_skill.jsonl"]
EDGE_PARTS = [
    "edge_is_a.jsonl",
    "edge_prerequisites_for.jsonl",
    "edge_relates_to.jsonl",
    "edge_verifies.jsonl",
    "edge_tests_concept.jsonl",
    "edge_tests_skill.jsonl",
]
EXERCISE_PARTS = ["exercise.jsonl"]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="合并 sft_qa parts 为最终 JSONL")
    parser.add_argument("--config", type=str, default=None)
    parser.add_argument("--subject-stage", required=True, help="学科学段 key，例如 math_primaryschool")
    parser.add_argument("--workspace-dir", type=str, default=None, help="覆盖默认 workspace/sft_qa/<subject_stage>")
    return parser.parse_args()


def load_optional_jsonl(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    return read_jsonl(path)


def merge_node_records(parts_dir: Path) -> List[Dict[str, Any]]:
    merged: List[Dict[str, Any]] = []
    index = 1
    for filename in NODE_PARTS:
        for item in load_optional_jsonl(parts_dir / filename):
            merged.append(
                {
                    "id": f"node_item_{index}",
                    "name": str(item.get("name", "")),
                    "question": str(item.get("question", "")),
                    "answer": str(item.get("answer", "")),
                }
            )
            index += 1
    return merged


def merge_edge_records(parts_dir: Path) -> List[Dict[str, Any]]:
    merged: List[Dict[str, Any]] = []
    index = 1
    for filename in EDGE_PARTS:
        for item in load_optional_jsonl(parts_dir / filename):
            merged.append(
                {
                    "id": f"edge_item_{index}",
                    "relationship": str(item.get("relationship", "")),
                    "question": str(item.get("question", "")),
                    "answer": str(item.get("answer", "")),
                }
            )
            index += 1
    return merged


def merge_exercise_records(parts_dir: Path) -> List[Dict[str, Any]]:
    merged: List[Dict[str, Any]] = []
    index = 1
    for filename in EXERCISE_PARTS:
        for item in load_optional_jsonl(parts_dir / filename):
            merged.append(
                {
                    "id": f"exercise_item_{index}",
                    "stem": str(item.get("stem", "")),
                    "question": str(item.get("question", "")),
                    "answer": str(item.get("answer", "")),
                }
            )
            index += 1
    return merged


def main() -> None:
    args = parse_args()
    load_openai_env(args.config)
    config = load_config(args.config)
    workspace_root = resolve_workspace_root(config, args.subject_stage, args.workspace_dir)
    parts_dir = workspace_root / "parts"
    final_dir = workspace_root / "final"
    final_dir.mkdir(parents=True, exist_ok=True)

    node_records = merge_node_records(parts_dir)
    edge_records = merge_edge_records(parts_dir)
    exercise_records = merge_exercise_records(parts_dir)

    write_jsonl(final_dir / "node.jsonl", node_records)
    write_jsonl(final_dir / "edge.jsonl", edge_records)
    write_jsonl(final_dir / "exercise.jsonl", exercise_records)

    print(f"[ok] node: {len(node_records)} 条 -> {final_dir / 'node.jsonl'}")
    print(f"[ok] edge: {len(edge_records)} 条 -> {final_dir / 'edge.jsonl'}")
    print(f"[ok] exercise: {len(exercise_records)} 条 -> {final_dir / 'exercise.jsonl'}")


if __name__ == "__main__":
    main()
