#!/usr/bin/env python3
"""Generate deterministic ``tests_concept`` / ``tests_skill`` QA JSONL without an LLM.

Derives short QA rows from KG exercise edges so ``merge_qa.py`` can include them in
the merged edge bundle.
"""

from __future__ import annotations

import argparse
import sys
from collections import OrderedDict
from pathlib import Path
from typing import Any, Dict, List

_SRC_DIR = Path(__file__).resolve().parents[1]
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from sft_qa.common import load_openai_env, resolve_input_path, resolve_workspace_root  # noqa: E402
from utils.config import load_config  # noqa: E402
from utils.io import read_json, write_jsonl  # noqa: E402


TASK_CONFIG = {
    "tests_concept": {
        "output": "edge_tests_concept.jsonl",
        "question_template": '"{source_stem}"这道题考察了什么概念？',
        "relationship": "tests_concept",
    },
    "tests_skill": {
        "output": "edge_tests_skill.jsonl",
        "question_template": '"{source_stem}"这道题考察了什么方法？',
        "relationship": "tests_skill",
    },
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="生成 tests_concept/tests_skill 的规则 QA")
    parser.add_argument("--config", type=str, default=None)
    parser.add_argument("--subject-stage", required=True, help="学科学段 key，例如 math_primaryschool")
    parser.add_argument("--input-json", type=str, default=None, help="覆盖默认输入 JSON 路径")
    parser.add_argument("--workspace-dir", type=str, default=None, help="覆盖默认 workspace/sft_qa/<subject_stage>")
    return parser.parse_args()


def unique_names(targets: List[Dict[str, Any]]) -> List[str]:
    names: List[str] = []
    seen: set[str] = set()
    for item in targets:
        name = str(item.get("target_name") or "").strip()
        if name and name not in seen:
            seen.add(name)
            names.append(name)
    return names


def build_records(data: Dict[str, Any], edge_type: str) -> List[Dict[str, Any]]:
    cfg = TASK_CONFIG[edge_type]
    grouped: "OrderedDict[tuple[str, str], List[Dict[str, Any]]]" = OrderedDict()

    for edge in data.get("edges", []):
        if edge.get("type") != edge_type:
            continue
        source_id = str(edge.get("source") or "").strip()
        source_stem = str(edge.get("source_stem") or "").strip()
        if not source_id or not source_stem:
            continue
        key = (source_id, source_stem)
        grouped.setdefault(key, [])
        grouped[key].extend(edge.get("target_name_to_ids") or [])

    records: List[Dict[str, Any]] = []
    for (source_id, source_stem), targets in grouped.items():
        answer = "，".join(unique_names(targets))
        records.append(
            {
                "task": f"edge_{edge_type}",
                "source_id": source_id,
                "relationship": cfg["relationship"],
                "source_stem": source_stem,
                "question": cfg["question_template"].format(source_stem=source_stem),
                "answer": answer,
            }
        )
    return records


def main() -> None:
    args = parse_args()
    load_openai_env(args.config)
    config = load_config(args.config)
    input_path = resolve_input_path(config, args.subject_stage, args.input_json)
    workspace_root = resolve_workspace_root(config, args.subject_stage, args.workspace_dir)
    parts_dir = workspace_root / "parts"
    parts_dir.mkdir(parents=True, exist_ok=True)

    data = read_json(input_path)
    if not isinstance(data, dict):
        raise ValueError(f"输入 JSON 顶层不是对象: {input_path}")

    for edge_type, cfg in TASK_CONFIG.items():
        records = build_records(data, edge_type)
        output_path = parts_dir / cfg["output"]
        write_jsonl(output_path, records)
        print(f"[ok] {edge_type}: {len(records)} 条 -> {output_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
