#!/usr/bin/env python3
"""Generate LLM-based SFT QA shards (node/edge tasks) from subject-stage KG JSON.

Consumes merged ``subject_stage_kg`` JSON and writes intermediate JSONL parts under
``workspace/sft_qa/<subject_stage>/parts`` for later merging.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List

_SRC_DIR = Path(__file__).resolve().parents[1]
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from sft_qa.common import load_openai_env, resolve_input_path, resolve_workspace_root  # noqa: E402
from utils.config import load_config  # noqa: E402
from utils.io import read_json  # noqa: E402
from utils.llm_client import create_llm_client  # noqa: E402


TASK_SPECS: Dict[str, Dict[str, str]] = {
    "node_concept": {
        "kind": "node",
        "label": "Concept",
        "prompt": "concept.txt",
        "output": "node_concept.jsonl",
        "name_prefix": "concept",
    },
    "node_skill": {
        "kind": "node",
        "label": "Skill",
        "prompt": "skill.txt",
        "output": "node_skill.jsonl",
        "name_prefix": "skill",
    },
    "edge_is_a": {
        "kind": "edge",
        "type": "is_a",
        "prompt": "is_a.txt",
        "output": "edge_is_a.jsonl",
    },
    "edge_prerequisites_for": {
        "kind": "edge",
        "type": "prerequisites_for",
        "prompt": "prerequisites_for.txt",
        "output": "edge_prerequisites_for.jsonl",
    },
    "edge_relates_to": {
        "kind": "edge",
        "type": "relates_to",
        "prompt": "relates_to.txt",
        "output": "edge_relates_to.jsonl",
    },
    "edge_verifies": {
        "kind": "edge",
        "type": "verifies",
        "prompt": "verifies.txt",
        "output": "edge_verifies.jsonl",
    },
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="生成 sft_qa 的 LLM 类中间 JSONL")
    parser.add_argument("--config", type=str, default=None, help="配置文件路径，默认使用 config/default.yaml")
    parser.add_argument("--subject-stage", required=True, help="学科学段 key，例如 math_primaryschool")
    parser.add_argument("--input-json", type=str, default=None, help="覆盖默认输入 JSON 路径")
    parser.add_argument("--workspace-dir", type=str, default=None, help="覆盖默认 workspace/sft_qa/<subject_stage>")
    parser.add_argument(
        "--tasks",
        type=str,
        default=",".join(TASK_SPECS.keys()),
        help="逗号分隔的 task 列表",
    )
    parser.add_argument("--limit", type=int, default=None, help="每个 task 最多处理多少条")
    parser.add_argument("--resume", action="store_true", help="若 parts 文件已存在，则跳过已处理 source_id")
    parser.add_argument("--model", type=str, default=None, help="覆盖 config 中的模型")
    parser.add_argument("--temperature", type=float, default=None, help="覆盖 config 中的 temperature")
    parser.add_argument("--max-tokens", type=int, default=1200, help="单次生成 max_tokens")
    parser.add_argument("-n", "--num-samples", type=int, default=1, help="每个输入样本生成多少条 QA")
    parser.add_argument(
        "--disable-json-schema",
        action="store_true",
        help="禁用 OpenAI Structured Outputs，改为只靠 prompt 约束输出格式",
    )
    return parser.parse_args()


def load_prompt(prompt_name: str) -> str:
    prompt_path = Path(__file__).resolve().parent / "prompts" / prompt_name
    return prompt_path.read_text(encoding="utf-8")


def render_prompt(template: str, fields: Dict[str, Any], num_samples: int) -> str:
    prompt = template.replace("{n}", str(num_samples))
    for key, value in fields.items():
        placeholder = "{{" + key + "}}"
        if isinstance(value, (dict, list)):
            value_text = json.dumps(value, ensure_ascii=False, indent=2)
        else:
            value_text = str(value)
        prompt = prompt.replace(placeholder, value_text)
    return prompt


def trim_node_properties(node: Dict[str, Any]) -> Dict[str, Any]:
    props = dict(node.get("properties") or {})
    label = str(node.get("label", "")).strip()
    if label == "Concept":
        for key in ("importance", "examples", "pages"):
            props.pop(key, None)
    elif label == "Skill":
        for key in ("examples", "pages"):
            props.pop(key, None)
    return props


def trim_edge_properties(edge: Dict[str, Any]) -> Dict[str, Any]:
    props = dict(edge.get("properties") or {})
    props.pop("page", None)
    props.pop("evidence_page", None)
    return props


def get_node_name(node: Dict[str, Any]) -> str:
    return str(node.get("name") or node.get("id") or "").strip()


def get_edge_names(edge: Dict[str, Any], node_index: Dict[str, Dict[str, Any]]) -> tuple[str, str]:
    source_name = str(edge.get("source_name") or "").strip()
    target_name = str(edge.get("target_name") or "").strip()
    if not source_name:
        source_node = node_index.get(str(edge.get("source", "")).strip())
        source_name = get_node_name(source_node or {})
    if not target_name:
        target_node = node_index.get(str(edge.get("target", "")).strip())
        target_name = get_node_name(target_node or {})
    return source_name, target_name


def build_relationship(edge_type: str, source_name: str, target_name: str) -> str:
    if edge_type == "relates_to":
        return f"{edge_type} | {source_name} <-> {target_name}"
    return f"{edge_type} | {source_name} -> {target_name}"


def build_node_fields(node: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "label": str(node.get("label", "")).strip(),
        "name": get_node_name(node),
        "properties_json": trim_node_properties(node),
    }


def build_edge_fields(edge: Dict[str, Any], node_index: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    source_name, target_name = get_edge_names(edge, node_index)
    return {
        "type": str(edge.get("type", "")).strip(),
        "source_name": source_name,
        "target_name": target_name,
        "properties_json": trim_edge_properties(edge),
    }


def parse_response_to_qas(text: str) -> List[Dict[str, str]]:
    candidates: List[str] = [text.strip()]

    fenced_matches = re.findall(r"```(?:json)?\s*(.*?)\s*```", text, flags=re.DOTALL)
    candidates.extend(match.strip() for match in fenced_matches if match.strip())

    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        candidates.append(text[start : end + 1].strip())

    start_arr = text.find("[")
    end_arr = text.rfind("]")
    if start_arr != -1 and end_arr != -1 and end_arr > start_arr:
        candidates.append(text[start_arr : end_arr + 1].strip())

    for candidate in candidates:
        if not candidate:
            continue
        try:
            obj = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and "question" in obj and "answer" in obj:
            return [
                {
                    "question": str(obj["question"]).strip(),
                    "answer": str(obj["answer"]).strip(),
                }
            ]
        if isinstance(obj, dict):
            for key in ("items", "qas", "qa_items"):
                value = obj.get(key)
                if not isinstance(value, list):
                    continue
                qas: List[Dict[str, str]] = []
                for item in value:
                    if not isinstance(item, dict):
                        continue
                    if "question" not in item or "answer" not in item:
                        continue
                    qas.append(
                        {
                            "question": str(item["question"]).strip(),
                            "answer": str(item["answer"]).strip(),
                        }
                    )
                if qas:
                    return qas
        if isinstance(obj, list) and obj and isinstance(obj[0], dict):
            qas: List[Dict[str, str]] = []
            for item in obj:
                if not isinstance(item, dict):
                    continue
                if "question" not in item or "answer" not in item:
                    continue
                qas.append(
                    {
                        "question": str(item["question"]).strip(),
                        "answer": str(item["answer"]).strip(),
                    }
                )
            if qas:
                return qas
    raise ValueError("模型输出无法解析为 question/answer JSON")


def build_response_format(num_samples: int) -> Dict[str, Any]:
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "qa_items",
            "strict": True,
            "schema": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "items": {
                        "type": "array",
                        "minItems": num_samples,
                        "maxItems": num_samples,
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": {
                                "question": {"type": "string"},
                                "answer": {"type": "string"},
                            },
                            "required": ["question", "answer"],
                        },
                    },
                },
                "required": ["items"],
            },
        },
    }


def load_processed_source_ids(path: Path) -> set[str]:
    processed: set[str] = set()
    if not path.exists():
        return processed
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            source_id = record.get("source_id")
            if isinstance(source_id, str) and source_id:
                processed.add(source_id)
    return processed


def select_items(task_name: str, data: Dict[str, Any]) -> List[Dict[str, Any]]:
    spec = TASK_SPECS[task_name]
    if spec["kind"] == "node":
        label = spec["label"]
        return [node for node in data.get("nodes", []) if node.get("label") == label]
    edge_type = spec["type"]
    return [edge for edge in data.get("edges", []) if edge.get("type") == edge_type]


def build_source_id(task_name: str, item: Dict[str, Any]) -> str:
    spec = TASK_SPECS[task_name]
    if spec["kind"] == "node":
        return str(item.get("id") or "").strip()
    return "::".join(
        [
            str(item.get("type") or "").strip(),
            str(item.get("source") or "").strip(),
            str(item.get("target") or "").strip(),
        ]
    )


def build_record(
    task_name: str,
    item: Dict[str, Any],
    qa: Dict[str, str],
    node_index: Dict[str, Dict[str, Any]],
) -> Dict[str, Any]:
    spec = TASK_SPECS[task_name]
    record: Dict[str, Any] = {
        "task": task_name,
        "source_id": build_source_id(task_name, item),
        "question": qa["question"],
        "answer": qa["answer"],
    }
    if spec["kind"] == "node":
        raw_name = get_node_name(item)
        record["name"] = f"{spec['name_prefix']} | {raw_name}"
    else:
        source_name, target_name = get_edge_names(item, node_index)
        record["relationship"] = build_relationship(spec["type"], source_name, target_name)
    return record


def append_raw_output(raw_path: Path, source_id: str, content: str) -> None:
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    with raw_path.open("a", encoding="utf-8") as f:
        f.write(f"===== {source_id} =====\n")
        f.write(content.rstrip())
        f.write("\n\n")


def write_record(out_f: Any, record: Dict[str, Any]) -> None:
    out_f.write(json.dumps(record, ensure_ascii=False) + "\n")
    out_f.flush()


def iter_task_names(raw_tasks: str) -> Iterable[str]:
    for task_name in (part.strip() for part in raw_tasks.split(",")):
        if not task_name:
            continue
        if task_name not in TASK_SPECS:
            raise ValueError(f"未知 task: {task_name}")
        yield task_name


def main() -> None:
    args = parse_args()
    load_openai_env(args.config)
    config = load_config(args.config)
    input_path = resolve_input_path(config, args.subject_stage, args.input_json)
    workspace_root = resolve_workspace_root(config, args.subject_stage, args.workspace_dir)
    parts_dir = workspace_root / "parts"
    raw_dir = workspace_root / "raw"
    parts_dir.mkdir(parents=True, exist_ok=True)
    raw_dir.mkdir(parents=True, exist_ok=True)

    data = read_json(input_path)
    if not isinstance(data, dict):
        raise ValueError(f"输入 JSON 顶层不是对象: {input_path}")

    node_index = {
        str(node.get("id") or "").strip(): node
        for node in data.get("nodes", [])
        if isinstance(node, dict) and str(node.get("id") or "").strip()
    }

    llm_cfg = dict(config.llm)
    provider = str(llm_cfg.get("provider", "openai"))
    model = args.model or str(llm_cfg.get("model", "gpt-4o")) # gpt-4.1-mini
    api_key = str(llm_cfg.get("api_key", "") or "").strip()
    base_url = str(llm_cfg.get("base_url", "") or "").strip() or None
    temperature = args.temperature if args.temperature is not None else float(llm_cfg.get("temperature", 0.0))
    client = create_llm_client(provider=provider, model=model, api_key=api_key, base_url=base_url)

    for task_name in iter_task_names(args.tasks):
        spec = TASK_SPECS[task_name]
        prompt_template = load_prompt(spec["prompt"])
        output_path = parts_dir / spec["output"]
        raw_path = raw_dir / output_path.name.replace(".jsonl", "_raw.txt")
        processed = load_processed_source_ids(output_path) if args.resume else set()
        mode = "a" if args.resume and output_path.exists() else "w"
        if mode == "w":
            raw_path.parent.mkdir(parents=True, exist_ok=True)
            raw_path.write_text("", encoding="utf-8")
        items = select_items(task_name, data)
        if args.limit is not None:
            items = items[: args.limit]

        print(f"[task] {task_name}: 共 {len(items)} 条", file=sys.stderr)

        with output_path.open(mode, encoding="utf-8") as out_f:
            for index, item in enumerate(items, start=1):
                source_id = build_source_id(task_name, item)
                if args.resume and source_id in processed:
                    continue

                try:
                    if spec["kind"] == "node":
                        fields = build_node_fields(item)
                    else:
                        fields = build_edge_fields(item, node_index)
                    prompt = render_prompt(prompt_template, fields, args.num_samples)
                    extra_kwargs: Dict[str, Any] = {}
                    if not args.disable_json_schema:
                        extra_kwargs["response_format"] = build_response_format(args.num_samples)
                    response = client.generate(
                        prompt,
                        temperature=temperature,
                        max_tokens=args.max_tokens,
                        **extra_kwargs,
                    )
                    append_raw_output(raw_path, source_id, response)
                    qas = parse_response_to_qas(response)
                    for qa in qas:
                        record = build_record(task_name, item, qa, node_index)
                        write_record(out_f, record)
                    print(
                        f"  [{index}/{len(items)}] 完成 {source_id}，写入 {len(qas)} 条",
                        file=sys.stderr,
                    )
                except Exception as exc:  # noqa: BLE001
                    append_raw_output(raw_path, source_id, f"[error] {type(exc).__name__}: {exc}")
                    print(f"  [{index}/{len(items)}] 失败 {source_id}: {exc}", file=sys.stderr)


if __name__ == "__main__":
    main()
