#!/usr/bin/env python3
"""Build a LlamaFactory-style ``train.jsonl`` from collected SFT QA shards.

Walks ``data/sft_qa`` (or a custom root), down-samples edge-heavy tasks using fixed
ratios, deduplicates, shuffles, and emits a single ``question``/``answer`` JSONL.
"""

from __future__ import annotations

import argparse
import hashlib
import random
import sys
from collections import Counter
from pathlib import Path
from typing import Dict, Iterable, List, Tuple

_SRC_DIR = Path(__file__).resolve().parents[1]
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from utils.io import read_jsonl, write_jsonl  # noqa: E402


DEFAULT_EDGE_RATIOS: Dict[str, float] = {
    "edge_is_a": 0.60,
    "edge_verifies": 0.60,
    "edge_prerequisites_for": 0.35,
    "edge_relates_to": 0.35,
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="按固定配比生成 LlamaFactory 训练集 JSONL")
    parser.add_argument(
        "--input-root",
        type=str,
        default=None,
        help="默认读取 repo_root/data/sft_qa",
    )
    parser.add_argument(
        "--output-jsonl",
        type=str,
        default=None,
        help="默认写入 repo_root/data/sft_qa/train.jsonl",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=20260412,
        help="随机种子，用于下采样与最终打乱",
    )
    parser.add_argument(
        "--dedupe-by",
        choices=["question_answer", "question", "none"],
        default="question_answer",
        help="去重策略",
    )
    return parser.parse_args()


def resolve_paths(args: argparse.Namespace) -> Tuple[Path, Path]:
    repo_root = Path(__file__).resolve().parents[2]
    input_root = Path(args.input_root).resolve() if args.input_root else repo_root / "data" / "sft_qa"
    output_jsonl = Path(args.output_jsonl).resolve() if args.output_jsonl else input_root / "train.jsonl"
    return input_root, output_jsonl


def stable_seed(seed: int, name: str) -> int:
    digest = hashlib.md5(name.encode("utf-8")).hexdigest()
    return seed + int(digest[:8], 16)


def normalize_text(text: object) -> str:
    return str(text or "").replace("\r\n", "\n").replace("\r", "\n").strip()


def file_task(path: Path, input_root: Path) -> str | None:
    rel = path.relative_to(input_root)
    if rel == Path("exercise.jsonl"):
        return "exercise"
    if rel.parts[-1] in {"k12_pri_math_edge.jsonl", "k12_pri_math_node.jsonl"}:
        return None
    if "parts" not in rel.parts:
        return None
    return path.stem


def desired_count(task: str, total: int) -> int:
    ratio = DEFAULT_EDGE_RATIOS.get(task)
    if ratio is None:
        return total
    return max(1, round(total * ratio))


def sample_rows(rows: List[dict], *, count: int, seed: int, key: str) -> List[dict]:
    if count >= len(rows):
        return list(rows)
    picked = list(rows)
    rng = random.Random(stable_seed(seed, key))
    rng.shuffle(picked)
    return picked[:count]


def iter_input_files(input_root: Path) -> Iterable[Path]:
    for path in sorted(input_root.rglob("*.jsonl")):
        if path.name == "train.jsonl":
            continue
        yield path


def make_record(row: dict) -> dict | None:
    question = normalize_text(row.get("question", ""))
    answer = normalize_text(row.get("answer", ""))
    if not question or not answer:
        return None
    return {"question": question, "answer": answer}


def dedupe_key(record: dict, mode: str) -> Tuple[str, ...]:
    if mode == "none":
        return ()
    if mode == "question":
        return (record["question"],)
    return (record["question"], record["answer"])


def main() -> None:
    args = parse_args()
    input_root, output_jsonl = resolve_paths(args)

    bucket_counts = Counter()
    selected_counts = Counter()
    file_counts: List[Tuple[str, int, int]] = []
    selected_records: List[dict] = []

    for path in iter_input_files(input_root):
        task = file_task(path, input_root)
        if task is None:
            continue

        rows = read_jsonl(path)
        bucket_counts[task] += len(rows)

        rel = str(path.relative_to(input_root))
        picked = sample_rows(rows, count=desired_count(task, len(rows)), seed=args.seed, key=rel)
        selected_counts[task] += len(picked)
        file_counts.append((rel, len(rows), len(picked)))

        for row in picked:
            record = make_record(row)
            if record is not None:
                selected_records.append(record)

    before_dedupe = len(selected_records)
    if args.dedupe_by == "none":
        deduped_records = selected_records
        removed = 0
    else:
        deduped_records: List[dict] = []
        seen = set()
        for record in selected_records:
            key = dedupe_key(record, args.dedupe_by)
            if key in seen:
                continue
            seen.add(key)
            deduped_records.append(record)
        removed = before_dedupe - len(deduped_records)

    rng = random.Random(args.seed)
    rng.shuffle(deduped_records)
    write_jsonl(output_jsonl, deduped_records)

    print(f"[ok] input_root: {input_root}")
    print(f"[ok] output_jsonl: {output_jsonl}")
    print(f"[ok] selected before dedupe: {before_dedupe}")
    print(f"[ok] duplicates removed ({args.dedupe_by}): {removed}")
    print(f"[ok] final records: {len(deduped_records)}")

    print("[stats] by task (raw -> selected)")
    for task in sorted(bucket_counts):
        print(f"  - {task}: {bucket_counts[task]} -> {selected_counts[task]}")

    print("[stats] by file (raw -> selected)")
    for rel, raw, picked in file_counts:
        print(f"  - {rel}: {raw} -> {picked}")


if __name__ == "__main__":
    main()
