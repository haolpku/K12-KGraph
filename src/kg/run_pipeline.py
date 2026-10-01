#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Single entrypoint for the textbook KG pipeline (prepare → segment → extract → merge → afterclass → check).

Orchestrates the numbered KG stages in order, reusing the same ``--config`` and
filter/limit flags across steps for reproducible batch runs.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional, Sequence

_SRC_DIR = Path(__file__).resolve().parents[1]
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from kg.build_afterclass_exercises import process_all as afterclass_process_all
from kg.check_cycles import run_checks
from kg.extract_kg_from_textbook import process_all as extract_process_all
from kg.merge_kg import run_all as merge_run_all
from kg.prepare_textbooks import process_all as prepare_process_all
from kg.segment_textbooks import process_all as segment_process_all

AFTERCLASS_ANSWER_MAX_TOKENS = 2400
AFTERCLASS_LINK_MAX_TOKENS = 1800


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="一键运行 kg 主线：source_pdf/source_md -> 章节 Markdown -> chapter KG -> merged KG + afterclass_exercises"
    )
    parser.add_argument("--config", default=None, help="Pipeline config path")
    parser.add_argument("--filter-prefix", action="append", default=None, help="Only process specific book_prefix values")
    parser.add_argument("--limit", type=int, default=None, help="Process at most N books")
    parser.add_argument("--prompt", default=None, help="Override KG extraction prompt path")
    parser.add_argument("--max-tokens", type=int, default=4000, help="Max completion tokens for KG extraction")
    parser.add_argument(
        "--no-overwrite",
        action="store_true",
        help="Reuse existing outputs when possible",
    )
    parser.add_argument(
        "--skip-check",
        action="store_true",
        help="Skip the final cycle-check step",
    )
    parser.add_argument(
        "--check-level",
        choices=["book", "subject_stage", "subject", "global"],
        default="global",
        help="Graph level for the final cycle check",
    )
    return parser.parse_args()


def _ensure_step_ok(step_name: str, summary: dict) -> None:
    failed = int(summary.get("failed", 0))
    if failed > 0:
        raise RuntimeError(f"{step_name} failed for {failed} book(s); inspect the summary above")


def _print_step(title: str) -> None:
    print(f"\n===== {title} =====")


def _run_prepare(config_path: Optional[str], filter_prefixes: Optional[Sequence[str]], limit: Optional[int], overwrite: bool) -> None:
    _print_step("STEP 0 prepare_textbooks")
    summary = prepare_process_all(
        config_path=config_path,
        filter_prefixes=filter_prefixes,
        limit=limit,
        overwrite=overwrite,
    )
    _ensure_step_ok("prepare_textbooks", summary)


def _run_segment(config_path: Optional[str], filter_prefixes: Optional[Sequence[str]], limit: Optional[int], overwrite: bool) -> None:
    _print_step("STEP 1 segment_textbooks")
    summary = segment_process_all(
        config_path=config_path,
        filter_prefixes=filter_prefixes,
        limit=limit,
        overwrite=overwrite,
    )
    _ensure_step_ok("segment_textbooks", summary)


def _run_extract(
    config_path: Optional[str],
    prompt_path: Optional[str],
    filter_prefixes: Optional[Sequence[str]],
    limit: Optional[int],
    max_tokens: int,
    overwrite: bool,
) -> None:
    _print_step("STEP 2 extract_kg_from_textbook")
    summary = extract_process_all(
        config_path=config_path,
        prompt_path=prompt_path,
        filter_prefixes=filter_prefixes,
        chapters_arg=None,
        limit=limit,
        max_tokens=max_tokens,
        overwrite=overwrite,
    )
    _ensure_step_ok("extract_kg_from_textbook", summary)


def _run_merge(config_path: Optional[str], filter_prefixes: Optional[Sequence[str]]) -> None:
    _print_step("STEP 3 merge_kg")
    merge_run_all(
        config_path=config_path,
        stage=None,
        filter_prefixes=filter_prefixes,
        dry_run=False,
    )


def _run_check(config_path: Optional[str], level: str) -> None:
    _print_step(f"STEP 5 check_cycles ({level})")
    run_checks(config_path, level, save_report=True)


def _run_afterclass(config_path: Optional[str], filter_prefixes: Optional[Sequence[str]], limit: Optional[int], overwrite: bool) -> None:
    _print_step("STEP 4 build_afterclass_exercises")
    summary = afterclass_process_all(
        config_path=config_path,
        filter_prefixes=filter_prefixes,
        limit=limit,
        overwrite=overwrite,
        extract_only=False,
        enrich_only=False,
        answer_max_tokens=AFTERCLASS_ANSWER_MAX_TOKENS,
        link_max_tokens=AFTERCLASS_LINK_MAX_TOKENS,
    )
    _ensure_step_ok("build_afterclass_exercises", summary)


def main() -> None:
    args = parse_args()
    overwrite = not args.no_overwrite

    _run_prepare(args.config, args.filter_prefix, args.limit, overwrite)
    _run_segment(args.config, args.filter_prefix, args.limit, overwrite)
    _run_extract(args.config, args.prompt, args.filter_prefix, args.limit, args.max_tokens, overwrite)
    _run_merge(args.config, args.filter_prefix)
    _run_afterclass(args.config, args.filter_prefix, args.limit, overwrite)
    if not args.skip_check:
        _run_check(args.config, args.check_level)


if __name__ == "__main__":
    main()
