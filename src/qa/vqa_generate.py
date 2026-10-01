#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Generate VQA training data from a per-book multimodal graph.

One question/answer pair per figure-grounded edge:
``illustrates`` / ``refers_to`` / ``requires_figure`` / ``supports_edge``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

# ``python src/qa/vqa_generate.py`` puts src/qa (not src) on sys.path.
_SRC_DIR = Path(__file__).resolve().parents[1]
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from utils.config import load_config  # noqa: E402
from utils.io import parse_json_block, read_json, read_jsonl, write_jsonl  # noqa: E402
from utils.llm_client import OpenAIClient  # noqa: E402
from utils.prompts import load_prompt, render_prompt  # noqa: E402


PROMPT_DIR = Path(__file__).resolve().parent / "prompts" / "vqa"


def shorten_text(text: str, limit: int = 300) -> str:
    """Compact a string for console output."""
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


SYSTEM_PROMPT = """你是一个拥有二十年经验的顶级 K12 教育专家，擅长将复杂的理科教材知识转化为类似 GPT-5 风格的高逻辑性、高启发性教材 QA。

你的任务是根据给定的多模态图谱边及相关结构化信息，生成高质量的 K12 VQA SFT 训练数据。

统一输出要求：
1. 只输出 JSON。
2. JSON 中只能包含 `question` 和 `answer` 两个字段，不能多也不能少。
3. 不要输出 Markdown 代码块，不要输出任何额外解释、标题或前后缀。
4. 输出格式严格如下：
```json
{
  "question": "在这里填写问题",
  "answer": "在这里填写答案"
}
```
"""


@dataclass
class Sample:
    sample_id: str
    task_type: str
    prompt_name: str
    prompt_text: str
    metadata: Dict[str, Any]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate VQA training data from a per-book multimodal graph")
    parser.add_argument("--config", default=None, help="Pipeline config path; defaults to config/default.yaml")
    parser.add_argument("--book-prefix", default=None, help="Book prefix, e.g. math_7a_rjb; used for the default paths")
    parser.add_argument("--graph", default=None, help="Input graph; defaults to <data>/mmkg/<book>.json")
    parser.add_argument("--output", default=None, help="Output path; defaults to <data>/mmkg/vqa/sft_vqa/<book>/sft_vqa.jsonl")
    parser.add_argument("--model", default=os.environ.get("OPENAI_MODEL", "gpt-4o-mini"))
    parser.add_argument("--api-base", default=os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"))
    parser.add_argument("--api-key-env", default="OPENAI_API_KEY")
    parser.add_argument("--edge-types", default="illustrates,refers_to,requires_figure,supports_edge")
    parser.add_argument("--preview-limit", type=int, default=0, help="Print the first N rendered prompts and exit")
    parser.add_argument("--limit-samples", type=int, default=None)
    return parser.parse_args()


def build_node_map(graph: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    return {node["id"]: node for node in graph.get("nodes", []) if "id" in node}


def safe_props(node: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not node:
        return {}
    return node.get("properties", {}) or {}


def edge_rationale(edge: Dict[str, Any]) -> str:
    props = edge.get("properties", {}) or {}
    rationale = str(props.get("rationale", "") or "").strip()
    if rationale:
        return rationale
    evidence = str(props.get("evidence", "") or "").strip()
    if evidence:
        return evidence
    return ""


def node_display_name(node: Optional[Dict[str, Any]], fallback: str) -> str:
    if not node:
        return fallback
    props = safe_props(node)
    return str(props.get("name", fallback) or fallback)


def resolve_node_name(node_id: str, nodes: Dict[str, Dict[str, Any]]) -> str:
    return node_display_name(nodes.get(node_id), node_id)


def make_edge_key(edge_type: str, edge: Dict[str, Any]) -> str:
    props = edge.get("properties", {}) or {}
    source_section = str(props.get("source_section", "") or "")
    source_id = edge_source_id(edge)
    target_id = edge_target_id(edge)
    return f"{edge_type}|{source_section}|{source_id}|{target_id}"


def make_sample_id(edge_type: str, edge: Dict[str, Any]) -> str:
    props = edge.get("properties", {}) or {}
    source_section = str(props.get("source_section", "") or "na")
    source_id = edge_source_id(edge).replace("/", "_")
    target_id = edge_target_id(edge).replace("/", "_")
    return f"{edge_type}__{source_section}__{source_id}__{target_id}"


EDGE_TYPE_DISPLAY = {
    "is_a": "包含(is_a)",
    "prerequisites_for": "前置(prerequisites_for)",
    "relates_to": "相关(relates_to)",
    "verifies": "验证(verifies)",
    "tests_concept": "考察概念(tests_concept)",
    "tests_skill": "考察方法(tests_skill)",
}


def edge_source_id(edge: Dict[str, Any]) -> str:
    props = edge.get("properties", {}) or {}
    return str(props.get("source_id", edge.get("source", "")) or edge.get("source", ""))


def edge_target_id(edge: Dict[str, Any]) -> str:
    props = edge.get("properties", {}) or {}
    return str(props.get("target_id", edge.get("target", "")) or edge.get("target", ""))


def node_kind(node: Optional[Dict[str, Any]]) -> str:
    if not node:
        return ""
    return str(node.get("label", "") or "")


def infer_target_type(edge: Dict[str, Any], node: Optional[Dict[str, Any]]) -> str:
    label = node_kind(node)
    if label:
        return label
    target_id = edge_target_id(edge)
    if "_cpt" in target_id:
        return "Concept"
    if "_skl" in target_id:
        return "Skill"
    if "_exp" in target_id:
        return "Experiment"
    if "_edge_" in target_id:
        return "Edge"
    return ""


def render_illustrates_prompt(edge: Dict[str, Any], nodes: Dict[str, Dict[str, Any]], idx: int) -> Sample:
    source = nodes.get(edge_source_id(edge))
    target = nodes.get(edge_target_id(edge))
    s_props = safe_props(source)
    figure_name = str(s_props.get("name", edge.get("source", "")) or edge.get("source", ""))
    prompt_text = render_prompt(
        load_prompt(PROMPT_DIR / "illustrates.txt"),
        n="1",
        figure_name=figure_name,
        figure_description=str(s_props.get("description", "")),
        target_name=node_display_name(target, str(edge.get("target", ""))),
        target_type=infer_target_type(edge, target),
        rationale=edge_rationale(edge),
    )
    return Sample(
        sample_id=make_sample_id("illustrates", edge),
        task_type="illustrates",
        prompt_name="illustrates.txt",
        prompt_text=prompt_text,
        metadata={
            "edge_key": make_edge_key("illustrates", edge),
            "source_id": edge_source_id(edge),
            "target_id": edge_target_id(edge),
            "edge": edge,
        },
    )


def render_refers_to_prompt(
    edge: Dict[str, Any], nodes: Dict[str, Dict[str, Any]], parent_figure: Optional[Dict[str, Any]], idx: int
) -> Sample:
    source = nodes.get(edge_source_id(edge))
    target = nodes.get(edge_target_id(edge))
    s_props = safe_props(source)
    f_props = safe_props(parent_figure)
    prompt_text = render_prompt(
        load_prompt(PROMPT_DIR / "refers_to.txt"),
        n="1",
        visual_element_name=str(s_props.get("name", edge.get("source", "")) or edge.get("source", "")),
        visual_element_description=str(s_props.get("description", "")),
        target_name=node_display_name(target, str(edge.get("target", ""))),
        target_type=infer_target_type(edge, target),
        rationale=edge_rationale(edge) or str(f_props.get("description", "")),
    )
    return Sample(
        sample_id=make_sample_id("refers_to", edge),
        task_type="refers_to",
        prompt_name="refers_to.txt",
        prompt_text=prompt_text,
        metadata={
            "edge_key": make_edge_key("refers_to", edge),
            "source_id": edge_source_id(edge),
            "target_id": edge_target_id(edge),
            "parent_figure_id": parent_figure.get("id") if parent_figure else None,
            "edge": edge,
        },
    )


def render_requires_figure_prompt(edge: Dict[str, Any], nodes: Dict[str, Dict[str, Any]], idx: int) -> Sample:
    source = nodes.get(edge_source_id(edge))
    target = nodes.get(edge_target_id(edge))
    s_props = safe_props(source)
    t_props = safe_props(target)
    exercise_stem = str(s_props.get("description", "") or s_props.get("name", edge.get("source", "")) or edge.get("source", ""))
    prompt_text = render_prompt(
        load_prompt(PROMPT_DIR / "requires_figure.txt"),
        n="1",
        exercise_stem=exercise_stem,
        figure_name=str(t_props.get("name", edge.get("target", "")) or edge.get("target", "")),
        figure_description=str(t_props.get("description", "")),
        rationale=edge_rationale(edge),
    )
    return Sample(
        sample_id=make_sample_id("requires_figure", edge),
        task_type="requires_figure",
        prompt_name="requires_figure.txt",
        prompt_text=prompt_text,
        metadata={
            "edge_key": make_edge_key("requires_figure", edge),
            "source_id": edge_source_id(edge),
            "target_id": edge_target_id(edge),
            "edge": edge,
        },
    )


def render_supports_edge_prompt(edge: Dict[str, Any], nodes: Dict[str, Dict[str, Any]], idx: int) -> Sample:
    source = nodes.get(edge_source_id(edge))
    target = nodes.get(edge_target_id(edge))
    edge_props = edge.get("properties", {}) or {}
    s_props = safe_props(source)
    t_props = safe_props(target)
    supported_source_id = str(t_props.get("source", ""))
    supported_target_id = str(t_props.get("target", ""))
    raw_supported_edge_type = (
        str(edge_props.get("supported_edge_type", "")).strip()
        or str(t_props.get("edge_type", "")).strip()
        or str((edge.get("target", "") or "")).strip()
    )
    supported_edge_type = EDGE_TYPE_DISPLAY.get(raw_supported_edge_type, raw_supported_edge_type)
    supported_source_name = (
        str(edge_props.get("supported_edge_source_name", "")).strip()
        or resolve_node_name(supported_source_id, nodes)
    )
    supported_target_name = (
        str(edge_props.get("supported_edge_target_name", "")).strip()
        or resolve_node_name(supported_target_id, nodes)
    )
    prompt_text = render_prompt(
        load_prompt(PROMPT_DIR / "supports_edge.txt"),
        n="1",
        figure_name=str(s_props.get("name", edge.get("source", "")) or edge.get("source", "")),
        figure_description=str(s_props.get("description", "")),
        supported_edge_type=supported_edge_type,
        source_name=supported_source_name,
        target_name=supported_target_name,
        edge_type=supported_edge_type,
        edge_source=supported_source_name,
        edge_target=supported_target_name,
        rationale=edge_rationale(edge) or str(t_props.get("evidence", "")),
    )
    return Sample(
        sample_id=make_sample_id("supports_edge", edge),
        task_type="supports_edge",
        prompt_name="supports_edge.txt",
        prompt_text=prompt_text,
        metadata={
            "edge_key": make_edge_key("supports_edge", edge),
            "source_id": edge_source_id(edge),
            "target_id": edge_target_id(edge),
            "edge": edge,
        },
    )


def build_ve_to_figure_map(graph: Dict[str, Any]) -> Dict[str, str]:
    mapping: Dict[str, str] = {}
    for edge in graph.get("edges", []):
        if edge.get("type") == "contains_visual_element":
            mapping[edge_target_id(edge)] = edge_source_id(edge)
    return mapping


def interleave_edges_by_section(
    items: Iterable[Tuple[str, Dict[str, Any]]],
    edge_types: List[str],
) -> List[Tuple[str, Dict[str, Any]]]:
    grouped: Dict[str, Dict[str, List[Dict[str, Any]]]] = {}
    section_order: List[str] = []
    for edge_type, edge in items:
        section = str((edge.get("properties") or {}).get("source_section", "") or "")
        if section not in grouped:
            grouped[section] = {et: [] for et in edge_types}
            section_order.append(section)
        grouped[section].setdefault(edge_type, []).append(edge)

    ordered: List[Tuple[str, Dict[str, Any]]] = []
    for section in section_order:
        buckets = grouped[section]
        while True:
            progressed = False
            for edge_type in edge_types:
                bucket = buckets.get(edge_type, [])
                if bucket:
                    ordered.append((edge_type, bucket.pop(0)))
                    progressed = True
            if not progressed:
                break
    return ordered


def collect_samples_from_graph(graph: Dict[str, Any], edge_types: List[str]) -> List[Sample]:
    nodes = build_node_map(graph)
    ve_to_figure = build_ve_to_figure_map(graph)
    samples: List[Sample] = []
    counters = {edge_type: 0 for edge_type in edge_types}
    selected: List[Tuple[str, Dict[str, Any]]] = []

    for edge in graph.get("edges", []):
        edge_type = edge.get("type")
        if edge_type not in edge_types:
            continue
        selected.append((edge_type, edge))

    for edge_type, edge in interleave_edges_by_section(selected, edge_types):
        counters[edge_type] += 1
        idx = counters[edge_type]
        if edge_type == "illustrates":
            samples.append(render_illustrates_prompt(edge, nodes, idx))
        elif edge_type == "refers_to":
            parent_figure = nodes.get(ve_to_figure.get(edge_source_id(edge), ""))
            samples.append(render_refers_to_prompt(edge, nodes, parent_figure, idx))
        elif edge_type == "requires_figure":
            samples.append(render_requires_figure_prompt(edge, nodes, idx))
        elif edge_type == "supports_edge":
            samples.append(render_supports_edge_prompt(edge, nodes, idx))
    return samples


def collect_samples(payload: Dict[str, Any], edge_types: List[str]) -> List[Sample]:
    if not isinstance(payload.get("nodes"), list) or not isinstance(payload.get("edges"), list):
        raise ValueError("Unsupported input: expected a graph with 'nodes' and 'edges'")
    return collect_samples_from_graph(payload, edge_types=edge_types)


def preview_samples(samples: List[Sample], limit: int) -> None:
    for sample in samples[:limit]:
        print("=" * 80)
        print(f"sample_id: {sample.sample_id}")
        print(f"task_type: {sample.task_type}")
        print(f"prompt_name: {sample.prompt_name}")
        print("-" * 80)
        print(sample.prompt_text)
        print()


def read_jsonl_rows(path: Path) -> List[Dict[str, Any]]:
    return read_jsonl(path) if path.exists() else []


def append_jsonl_row(path: Path, row: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def row_edge_key(row: Dict[str, Any]) -> str:
    metadata = row.get("metadata", {}) or {}
    if metadata.get("edge_key"):
        return str(metadata["edge_key"])
    edge = metadata.get("edge", {}) or {}
    task_type = str(row.get("task_type", "") or "")
    if task_type and edge:
        return make_edge_key(task_type, edge)
    source_id = str(metadata.get("source_id", "") or "")
    target_id = str(metadata.get("target_id", "") or "")
    source_section = str(((metadata.get("edge", {}) or {}).get("properties", {}) or {}).get("source_section", "") or "")
    return f"{task_type}|{source_section}|{source_id}|{target_id}"


def validate_model_output(raw: Dict[str, Any], sample_id: str) -> Dict[str, str]:
    keys = set(raw.keys())
    if keys != {"question", "answer"}:
        raise ValueError(f"{sample_id}: 输出字段错误，实际字段为 {sorted(keys)}")

    question = str(raw.get("question", "") or "").strip()
    answer = str(raw.get("answer", "") or "").strip()
    if not question:
        raise ValueError(f"{sample_id}: question 为空")
    if not answer:
        raise ValueError(f"{sample_id}: answer 为空")
    if "\n\n" in answer:
        raise ValueError(f"{sample_id}: answer 不是单段文本")
    return {"question": question, "answer": answer}


def build_text_dump(rows: List[Dict[str, Any]]) -> str:
    blocks: List[str] = []
    for row in rows:
        blocks.extend(
            [
                f"sample_id: {row['sample_id']}",
                f"task_type: {row['task_type']}",
                f"prompt_name: {row['prompt_name']}",
                "metadata:",
                str(row["metadata"]),
                "prompt:",
                row["prompt_text"],
                "raw_output:",
                row["raw_output"],
                "",
                "=" * 80,
                "",
            ]
        )
    return "\n".join(blocks).rstrip() + "\n"


def build_raw_paths(output_path: Path) -> Tuple[Path, Path]:
    raw_jsonl_path = output_path.with_suffix(".raw.jsonl")
    raw_text_dir = output_path.parent / f"{output_path.stem}_raw"
    return raw_jsonl_path, raw_text_dir


def run_generation(
    samples: List[Sample],
    client: OpenAIClient,
    output_path: Path,
    raw_jsonl_path: Path,
    raw_text_dir: Path,
) -> Tuple[int, int]:
    success_count = 0
    failed_count = 0
    for sample in samples:
        raw_output = client.generate_chat(SYSTEM_PROMPT, sample.prompt_text, temperature=0.2, json_mode=True)
        raw_record = {
            "sample_id": sample.sample_id,
            "task_type": sample.task_type,
            "prompt_name": sample.prompt_name,
            "prompt_text": sample.prompt_text,
            "metadata": sample.metadata,
            "raw_output": raw_output,
        }
        append_jsonl_row(raw_jsonl_path, raw_record)
        write_text(raw_text_dir / f"{sample.sample_id}.txt", raw_output)

        try:
            raw = parse_json_block(raw_output)
            validated = validate_model_output(raw, sample.sample_id)
        except Exception as exc:
            failed_count += 1
            print(
                f"[raw_saved_parse_failed] {sample.sample_id} err={shorten_text(str(exc), 120)}",
                flush=True,
            )
            continue

        row = {
            "sample_id": sample.sample_id,
            "task_type": sample.task_type,
            "question": validated["question"],
            "answer": validated["answer"],
            "metadata": sample.metadata,
        }
        append_jsonl_row(output_path, row)
        success_count += 1
        print(f"[done] {sample.sample_id} q={shorten_text(validated['question'], 80)}", flush=True)
    return success_count, failed_count


def run_generation_to_text(samples: List[Sample], client: OpenAIClient) -> str:
    rows: List[Dict[str, Any]] = []
    for sample in samples:
        raw_output = client.generate_chat(SYSTEM_PROMPT, sample.prompt_text, temperature=0.2, json_mode=True)
        rows.append(
            {
                "sample_id": sample.sample_id,
                "task_type": sample.task_type,
                "prompt_name": sample.prompt_name,
                "prompt_text": sample.prompt_text,
                "metadata": sample.metadata,
                "raw_output": raw_output,
            }
        )
        print(f"[done] {sample.sample_id} raw={shorten_text(raw_output, 80)}", flush=True)
    return build_text_dump(rows)


def main() -> None:
    args = parse_args()
    config = load_config(args.config)
    if not args.book_prefix and not args.graph:
        raise SystemExit("Provide --book-prefix (for the default paths) or --graph")

    graph_path = Path(args.graph) if args.graph else config.mmkg_book_graph_for(str(args.book_prefix))
    if not graph_path.exists():
        raise SystemExit(f"Input graph not found: {graph_path}\nRun src/mm/extract_figures.py first.")
    graph = read_json(graph_path)

    edge_types = [item.strip() for item in args.edge_types.split(",") if item.strip()]
    samples = collect_samples(graph, edge_types=edge_types)
    if args.limit_samples is not None:
        samples = samples[: args.limit_samples]

    if args.preview_limit > 0:
        preview_samples(samples, args.preview_limit)
        return

    api_key = os.environ.get(args.api_key_env)
    if not api_key:
        raise SystemExit(f"Missing API key in environment variable: {args.api_key_env}")

    output_path = Path(args.output) if args.output else config.sft_vqa_dir(str(args.book_prefix)) / "sft_vqa.jsonl"
    if output_path.suffix.lower() == ".txt":
        client = OpenAIClient(model=args.model, api_key=api_key, base_url=args.api_base)
        text_dump = run_generation_to_text(samples, client)
        write_text(output_path, text_dump)
        print(f"Wrote {len(samples)} raw samples to {args.output}", flush=True)
        return

    client = OpenAIClient(model=args.model, api_key=api_key, base_url=args.api_base)
    raw_jsonl_path, raw_text_dir = build_raw_paths(output_path)
    existing_rows = read_jsonl_rows(output_path) if output_path.exists() else []
    existing_raw_rows = read_jsonl_rows(raw_jsonl_path) if raw_jsonl_path.exists() else []
    existing_edge_keys = {row_edge_key(row) for row in existing_rows}
    existing_raw_edge_keys = {row_edge_key(row) for row in existing_raw_rows}
    completed_edge_keys = existing_edge_keys | existing_raw_edge_keys
    pending_samples = [sample for sample in samples if str(sample.metadata.get("edge_key", "")) not in completed_edge_keys]
    if not pending_samples:
        print(
            f"No pending samples. Structured rows: {len(existing_rows)}, raw rows: {len(existing_raw_rows)}",
            flush=True,
        )
        return

    success_count, failed_count = run_generation(
        pending_samples,
        client,
        output_path=output_path,
        raw_jsonl_path=raw_jsonl_path,
        raw_text_dir=raw_text_dir,
    )
    total_rows = len(read_jsonl_rows(output_path)) if output_path.exists() else 0
    total_raw_rows = len(read_jsonl_rows(raw_jsonl_path)) if raw_jsonl_path.exists() else 0
    print(
        f"Wrote {success_count} structured rows to {args.output}; raw saved {total_raw_rows}, "
        f"parse_failed_this_run {failed_count}, total structured {total_rows}",
        flush=True,
    )


if __name__ == "__main__":
    main()
