#!/usr/bin/env python3
"""Recover structured sft_vqa rows from raw outputs by stripping think blocks."""

from __future__ import annotations

import argparse
import json
import re
import shutil
from pathlib import Path
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
DEFAULT_SFT_ROOT = PROJECT_ROOT / "outputs" / "sft_vqa"

THINK_BLOCK_RE = re.compile(r"<think>.*?</think>\s*", re.S)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Recover structured sft_vqa rows from raw outputs.")
    parser.add_argument("--sft-root", default=str(DEFAULT_SFT_ROOT))
    parser.add_argument("--backup-suffix", default="_backup_before_raw_recovery_20260610")
    return parser.parse_args()


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.exists():
        return rows
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rows.append(json.loads(line))
    return rows


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def try_parse_raw(raw_text: str) -> tuple[dict[str, Any] | None, str | None]:
    text = (raw_text or "").strip()
    if not text:
        return None, "empty_raw"

    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict) and "question" in parsed and "answer" in parsed:
            return parsed, "direct"
    except Exception:
        pass

    cleaned = THINK_BLOCK_RE.sub("", text).strip()
    try:
        parsed = json.loads(cleaned)
        if isinstance(parsed, dict) and "question" in parsed and "answer" in parsed:
            return parsed, "strip_think"
    except Exception:
        pass

    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start != -1 and end != -1 and end > start:
        candidate = cleaned[start : end + 1]
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, dict) and "question" in parsed and "answer" in parsed:
                return parsed, "extract_object"
        except Exception:
            pass
    return None, "parse_failed"


def build_row(raw_row: dict[str, Any], parsed: dict[str, Any]) -> dict[str, Any]:
    return {
        "sample_id": raw_row.get("sample_id"),
        "task_type": raw_row.get("task_type"),
        "question": str(parsed.get("question", "") or "").strip(),
        "answer": str(parsed.get("answer", "") or "").strip(),
        "metadata": raw_row.get("metadata", {}) or {},
    }


def backup_file(path: Path, suffix: str) -> Path | None:
    if not path.exists():
        return None
    backup = path.with_name(path.name + suffix)
    if not backup.exists():
        shutil.copy2(path, backup)
    return backup


def main() -> None:
    args = parse_args()
    sft_root = Path(args.sft_root)
    summary_books: list[dict[str, Any]] = []
    recover_mode_counts: dict[str, int] = {}
    failed_examples: list[dict[str, Any]] = []

    for book_dir in sorted([p for p in sft_root.iterdir() if p.is_dir()]):
        raw_path = book_dir / "sft_vqa.raw.jsonl"
        struct_path = book_dir / "sft_vqa.jsonl"
        if not raw_path.exists():
            continue

        raw_rows = read_jsonl(raw_path)
        struct_rows = read_jsonl(struct_path)
        existing_by_id = {row.get("sample_id"): row for row in struct_rows}
        recovered = 0
        failed = 0

        for raw_row in raw_rows:
            sample_id = raw_row.get("sample_id")
            if sample_id in existing_by_id:
                continue
            parsed, mode = try_parse_raw(str(raw_row.get("raw_output", "") or ""))
            if parsed is None:
                failed += 1
                if len(failed_examples) < 20:
                    failed_examples.append(
                        {
                            "book": book_dir.name,
                            "sample_id": sample_id,
                            "reason": mode,
                            "raw_prefix": str(raw_row.get("raw_output", "") or "")[:500],
                        }
                    )
                continue
            row = build_row(raw_row, parsed)
            struct_rows.append(row)
            existing_by_id[sample_id] = row
            recovered += 1
            recover_mode_counts[mode or "unknown"] = recover_mode_counts.get(mode or "unknown", 0) + 1

        if recovered:
            backup_file(struct_path, args.backup_suffix)
            struct_rows.sort(key=lambda r: str(r.get("sample_id", "")))
            write_jsonl(struct_path, struct_rows)

        if recovered or failed:
            summary_books.append(
                {
                    "book": book_dir.name,
                    "raw_count": len(raw_rows),
                    "structured_count_after": len(struct_rows),
                    "recovered": recovered,
                    "failed": failed,
                }
            )

    summary = {
        "sft_root": str(sft_root),
        "books": summary_books,
        "totals": {
            "book_count": len(summary_books),
            "recovered_total": sum(item["recovered"] for item in summary_books),
            "failed_total": sum(item["failed"] for item in summary_books),
            "recover_mode_counts": recover_mode_counts,
        },
        "failed_examples": failed_examples,
    }
    summary_path = sft_root / "raw_recovery_summary_20260610.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
