#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""VQA data pipeline: generate -> image assets -> LLaMA-Factory format.

Examples
--------
::

    python src/qa/run_pipeline.py --filter-prefix math_7a_rjb
    python src/qa/run_pipeline.py --filter-prefix math_7a_rjb --skip-assets
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path
from typing import List, Sequence

# ``python src/qa/run_pipeline.py`` puts src/qa (not src) on sys.path.
_SRC_DIR = Path(__file__).resolve().parents[1]
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from utils.config import load_config  # noqa: E402
from utils.k12_ids import normalize_book_prefix  # noqa: E402


SCRIPT_DIR = Path(__file__).resolve().parent


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="一键运行 VQA：generate -> assets -> alpaca")
    parser.add_argument("--config", default=None, help="Pipeline config path; defaults to config/default.yaml")
    parser.add_argument("--filter-prefix", action="append", default=None, help="Only process these book prefixes")
    parser.add_argument("--limit", type=int, default=None, help="Process at most N books")
    parser.add_argument("--model", default=None, help="Override the model name")
    parser.add_argument("--api-base", default=None, help="Override the OpenAI-compatible API base URL")
    parser.add_argument("--dataset-name", default="k12_mmkg_sft_vqa_alpaca", help="Name of the exported dataset")
    parser.add_argument("--skip-assets", action="store_true", help="Skip the image-asset step")
    parser.add_argument("--skip-export", action="store_true", help="Skip the LLaMA-Factory export")
    parser.add_argument("--python", default=None, help="Override the Python interpreter")
    return parser.parse_args()


def run(cmd: List[str]) -> None:
    print("[cmd]", " ".join(cmd), flush=True)
    subprocess.run(cmd, check=True)


def main() -> None:
    args = parse_args()
    config = load_config(args.config)
    py = args.python or sys.executable
    common = ["--config", str(args.config)] if args.config else []

    books: Sequence[str] = [str(book["book_prefix"]) for book in config.load_books(require_source=False)]
    wanted = {normalize_book_prefix(item) for item in (args.filter_prefix or [])}
    if wanted:
        books = [book for book in books if normalize_book_prefix(book) in wanted]
    if args.limit is not None:
        books = list(books)[: args.limit]
    if not books:
        raise SystemExit("No books selected; check --filter-prefix / books.yaml")

    failures: List[str] = []
    for idx, book_prefix in enumerate(books, start=1):
        print(f"\n===== [{idx}/{len(books)}] {book_prefix} =====", flush=True)
        if not config.mmkg_book_graph_for(book_prefix).exists():
            print(f"[skip] no multimodal graph yet: {config.mmkg_book_graph_for(book_prefix)}", flush=True)
            failures.append(book_prefix)
            continue
        cmd = [py, str(SCRIPT_DIR / "vqa_generate.py"), "--book-prefix", book_prefix, *common]
        if args.model:
            cmd += ["--model", str(args.model)]
        if args.api_base:
            cmd += ["--api-base", str(args.api_base)]
        try:
            run(cmd)
        except subprocess.CalledProcessError as exc:
            print(f"[FAIL] generate {book_prefix}: {exc}", flush=True)
            failures.append(book_prefix)

    if not args.skip_assets:
        run([py, str(SCRIPT_DIR / "vqa_assets.py"), *common])

    if not args.skip_export:
        run([py, str(SCRIPT_DIR / "export_alpaca.py"), *common, "--dataset-name", args.dataset_name])

    print("\n===== SUMMARY =====")
    print(f"books: {len(books)} | failed: {len(failures)}")
    if failures:
        print("failed books: " + ", ".join(failures))
        raise SystemExit(1)


if __name__ == "__main__":
    main()
