#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Multimodal pipeline for one or more books: align figures -> extract figures.

``align_figures.py`` matches every section with its figures; ``extract_figures.py``
asks the vision model what those figures show, and merges the result onto the
text-side book graph.

Examples
--------
::

    python src/mm/run_pipeline.py --filter-prefix math_7a_rjb
    python src/mm/run_pipeline.py --filter-prefix math_7a_rjb --dry-run
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path
from typing import List, Sequence

# ``python src/mm/run_pipeline.py`` puts src/mm (not src) on sys.path.
_SRC_DIR = Path(__file__).resolve().parents[1]
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from utils.config import load_config  # noqa: E402
from utils.k12_ids import normalize_book_prefix  # noqa: E402


SCRIPT_DIR = Path(__file__).resolve().parent


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="一键运行多模态：align_figures -> extract_figures")
    parser.add_argument("--config", default=None, help="Pipeline config path; defaults to config/default.yaml")
    parser.add_argument("--filter-prefix", action="append", default=None, help="Only process these book prefixes")
    parser.add_argument("--limit", type=int, default=None, help="Process at most N books")
    parser.add_argument("--model", default=None, help="Override the multimodal model name")
    parser.add_argument("--api-base", default=None, help="Override the OpenAI-compatible API base URL")
    parser.add_argument("--sections-dir", default=None, help="Override the section markdown directory")
    parser.add_argument("--hybrid-dir", default=None, help="Override the MinerU directory (markdown + images/)")
    parser.add_argument("--dry-run", action="store_true", help="Skip the model calls")
    parser.add_argument("--no-overwrite", action="store_true", help="Reuse an existing aligned_sections.json")
    parser.add_argument("--python", default=None, help="Override the Python interpreter")
    return parser.parse_args()


def run(cmd: List[str]) -> None:
    print("[cmd]", " ".join(cmd), flush=True)
    subprocess.run(cmd, check=True)


def align_cmd(args: argparse.Namespace, py: str, book_prefix: str) -> List[str]:
    cmd = [py, str(SCRIPT_DIR / "align_figures.py"), "--book-prefix", book_prefix]
    if args.config:
        cmd += ["--config", str(args.config)]
    if args.sections_dir:
        cmd += ["--sections-dir", str(args.sections_dir)]
    if args.hybrid_dir:
        cmd += ["--hybrid-dir", str(args.hybrid_dir)]
    return cmd


def extract_cmd(args: argparse.Namespace, py: str, book_prefix: str) -> List[str]:
    cmd = [py, str(SCRIPT_DIR / "extract_figures.py"), "--book-prefix", book_prefix, "--resume"]
    if args.config:
        cmd += ["--config", str(args.config)]
    if args.model:
        cmd += ["--model", str(args.model)]
    if args.api_base:
        cmd += ["--api-base", str(args.api_base)]
    if args.dry_run:
        cmd += ["--dry-run"]
    return cmd


def main() -> None:
    args = parse_args()
    config = load_config(args.config)
    py = args.python or sys.executable

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
        aligned = config.aligned_figures_for(book_prefix)
        if args.no_overwrite and aligned.exists():
            print(f"[skip] aligned sections exist: {aligned}", flush=True)
        else:
            try:
                run(align_cmd(args, py, book_prefix))
            except subprocess.CalledProcessError as exc:
                print(f"[FAIL] align {book_prefix}: {exc}", flush=True)
                failures.append(book_prefix)
                continue
        try:
            run(extract_cmd(args, py, book_prefix))
        except subprocess.CalledProcessError as exc:
            print(f"[FAIL] extract {book_prefix}: {exc}", flush=True)
            failures.append(book_prefix)

    print("\n===== SUMMARY =====")
    print(f"books: {len(books)} | failed: {len(failures)}")
    if failures:
        print("failed books: " + ", ".join(failures))
        raise SystemExit(1)


if __name__ == "__main__":
    main()
