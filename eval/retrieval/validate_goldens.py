"""Validate retrieval golden JSONL files."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

REQUIRED_FIELDS = {
    "query_id",
    "query",
    "intent",
    "user_type",
    "filters",
    "expected_ids",
    "expected_evidence_types",
    "notes",
}


def load_jsonl(path: Path) -> list[dict]:
    rows = []
    with path.open(encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, start=1):
            if line.strip():
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError as exc:
                    raise ValueError(f"line {line_no}: invalid JSON: {exc}") from exc
    return rows


def validate_rows(rows: list[dict], min_rows: int = 200) -> None:
    if len(rows) < min_rows:
        raise ValueError(f"expected at least {min_rows} rows, got {len(rows)}")

    seen_ids: set[str] = set()
    for index, row in enumerate(rows, start=1):
        missing = REQUIRED_FIELDS - row.keys()
        if missing:
            raise ValueError(f"row {index}: missing fields {sorted(missing)}")
        if row["query_id"] in seen_ids:
            raise ValueError(f"row {index}: duplicate query_id {row['query_id']}")
        seen_ids.add(row["query_id"])
        if not isinstance(row["query"], str) or not row["query"].strip():
            raise ValueError(f"row {index}: query must be a non-empty string")
        if row["user_type"] not in {"student", "teacher"}:
            raise ValueError(f"row {index}: invalid user_type {row['user_type']}")
        if not isinstance(row["filters"], dict):
            raise ValueError(f"row {index}: filters must be an object")
        if not isinstance(row["expected_ids"], list):
            raise ValueError(f"row {index}: expected_ids must be a list")
        if row["notes"] != "synthetic_seed_not_sme_reviewed":
            raise ValueError(f"row {index}: notes must mark synthetic review status")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("goldens", type=Path)
    parser.add_argument("--min-rows", type=int, default=200)
    args = parser.parse_args()
    rows = load_jsonl(args.goldens)
    validate_rows(rows, min_rows=args.min_rows)
    print(f"validated {len(rows)} rows from {args.goldens}")


if __name__ == "__main__":
    main()
