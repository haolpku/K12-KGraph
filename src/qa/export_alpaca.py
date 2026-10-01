#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Package the generated VQA data as LLaMA-Factory alpaca+images."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from collections import Counter
from pathlib import Path
from typing import Any

# ``python src/qa/export_alpaca.py`` puts src/qa (not src) on sys.path.
_SRC_DIR = Path(__file__).resolve().parents[1]
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from utils.config import load_config  # noqa: E402
from utils.io import read_json, write_json  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Convert sft_vqa outputs to LlamaFactory alpaca+images format."
    )
    parser.add_argument("--config", default=None, help="Pipeline config path; defaults to config/default.yaml")
    parser.add_argument("--vqa-root", default=None, help="Defaults to <data>/mmkg/vqa/sft_vqa")
    parser.add_argument("--asset-root", default=None, help="Defaults to <data>/mmkg/vqa/assets")
    parser.add_argument("--output-root", default=None, help="Defaults to <data>/mmkg/vqa/alpaca")
    parser.add_argument("--dataset-name", default="k12_mmkg_sft_vqa_alpaca")
    return parser.parse_args()


def append_jsonl(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def choose_image(
    task_type: str,
    metadata: dict[str, Any],
    figure_manifest: dict[str, str],
    boxed_manifest: dict[str, str],
    asset_root: Path,
) -> tuple[list[str] | None, str | None]:
    source_id = str(metadata.get("source_id", "") or "")
    target_id = str(metadata.get("target_id", "") or "")

    if task_type == "refers_to":
        rel = boxed_manifest.get(source_id)
        if not rel:
            return None, "missing_boxed_image"
        image_path = asset_root / "ve_boxed" / rel
        if not image_path.exists():
            return None, "boxed_file_not_found"
        return [str(image_path)], None

    figure_id = target_id if task_type == "requires_figure" else source_id
    rel = figure_manifest.get(figure_id)
    if not rel:
        return None, "missing_figure_image"
    image_path = asset_root / "figures" / rel
    if not image_path.exists():
        return None, "figure_file_not_found"
    return [str(image_path)], None


def build_alpaca_row(
    book: str,
    sample: dict[str, Any],
    images: list[str],
) -> dict[str, Any]:
    metadata = sample.get("metadata", {}) or {}
    edge = metadata.get("edge", {}) or {}
    props = edge.get("properties", {}) or {}
    return {
        "instruction": str(sample.get("question", "") or "").strip(),
        "input": "",
        "output": str(sample.get("answer", "") or "").strip(),
        "images": images,
        "task_type": sample.get("task_type"),
        "sample_id": sample.get("sample_id"),
        "book": book,
        "section": props.get("source_section"),
        "edge_key": metadata.get("edge_key"),
        "source_id": metadata.get("source_id"),
        "target_id": metadata.get("target_id"),
    }


def main() -> None:
    args = parse_args()
    config = load_config(args.config)
    vqa_root = Path(args.vqa_root) if args.vqa_root else config.sft_vqa_root
    asset_root = Path(args.asset_root) if args.asset_root else config.vqa_assets_dir
    output_root = Path(args.output_root) if args.output_root else config.vqa_alpaca_dir
    output_root.mkdir(parents=True, exist_ok=True)

    dataset_path = output_root / f"{args.dataset_name}.jsonl"
    if dataset_path.exists():
        dataset_path.unlink()
    skipped_root = output_root / "skipped_backup"
    if skipped_root.exists():
        shutil.rmtree(skipped_root)
    skipped_root.mkdir(parents=True, exist_ok=True)

    summary_books: list[dict[str, Any]] = []
    task_counter: Counter[str] = Counter()
    skip_counter: Counter[str] = Counter()
    skipped_examples: list[dict[str, Any]] = []

    for book_dir in sorted([p for p in vqa_root.iterdir() if p.is_dir()]):
        book = book_dir.name
        sft_path = book_dir / "sft_vqa.jsonl"
        asset_manifest_path = asset_root / book / "asset_manifest.json"
        if not sft_path.exists() or not asset_manifest_path.exists():
            continue

        asset_manifest = read_json(asset_manifest_path)
        figure_manifest = asset_manifest.get("figure_manifest", {}) or {}
        boxed_manifest = asset_manifest.get("boxed_manifest", {}) or {}

        kept = 0
        skipped = 0
        kept_by_type: Counter[str] = Counter()
        skipped_by_reason: Counter[str] = Counter()

        with sft_path.open("r", encoding="utf-8") as f:
            for line in f:
                sample = json.loads(line)
                task_type = str(sample.get("task_type", "") or "")
                images, error = choose_image(
                    task_type=task_type,
                    metadata=sample.get("metadata", {}) or {},
                    figure_manifest=figure_manifest,
                    boxed_manifest=boxed_manifest,
                    asset_root=asset_root,
                )
                if images is None:
                    skipped += 1
                    skipped_by_reason[error or "unknown"] += 1
                    skip_counter[error or "unknown"] += 1
                    append_jsonl(
                        skipped_root / f"{error or 'unknown'}.jsonl",
                        {
                            "book": book,
                            "sample": sample,
                            "reason": error or "unknown",
                        },
                    )
                    if len(skipped_examples) < 20:
                        skipped_examples.append(
                            {
                                "book": book,
                                "sample_id": sample.get("sample_id"),
                                "task_type": task_type,
                                "reason": error or "unknown",
                            }
                        )
                    continue

                row = build_alpaca_row(book=book, sample=sample, images=images)
                append_jsonl(dataset_path, row)
                kept += 1
                kept_by_type[task_type] += 1
                task_counter[task_type] += 1

        summary_books.append(
            {
                "book": book,
                "kept": kept,
                "skipped": skipped,
                "kept_by_type": dict(sorted(kept_by_type.items())),
                "skipped_by_reason": dict(sorted(skipped_by_reason.items())),
            }
        )

    dataset_info_snippet = {
        args.dataset_name: {
            "file_name": dataset_path.name,
            "formatting": "alpaca",
            "columns": {
                "prompt": "instruction",
                "query": "input",
                "response": "output",
                "images": "images",
            },
        }
    }

    write_json(output_root / "dataset_info.snippet.json", dataset_info_snippet)
    write_json(
        output_root / "summary.json",
        {
            "vqa_root": str(vqa_root),
            "asset_root": str(asset_root),
            "output_root": str(output_root),
            "dataset_name": args.dataset_name,
            "dataset_file": str(dataset_path),
            "books": summary_books,
            "totals": {
                "book_count": len(summary_books),
                "kept_total": sum(item["kept"] for item in summary_books),
                "skipped_total": sum(item["skipped"] for item in summary_books),
                "kept_by_type": dict(sorted(task_counter.items())),
                "skipped_by_reason": dict(sorted(skip_counter.items())),
            },
            "skipped_examples": skipped_examples,
        },
    )


if __name__ == "__main__":
    main()
