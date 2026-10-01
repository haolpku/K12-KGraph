#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Step 0: ensure each book has Markdown by running MinerU on PDFs when needed.

Reads ``books.yaml`` via pipeline config, generates missing ``source_md`` from
``source_pdf``, and rewrites YAML paths back to the books manifest when successful.
"""

from __future__ import annotations

import argparse
import json
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

_SRC_DIR = Path(__file__).resolve().parents[1]
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from utils.config import PipelineConfig, load_config
from utils.io import write_json

IGNORED_MARKDOWN_SUFFIXES = ("_content_list", "_model", "sections_index")
BOOK_PREFIX_RE = re.compile(r"^\s*-\s*book_prefix:\s*(?P<prefix>\S+)\s*$")
SOURCE_MD_RE = re.compile(r"^(?P<indent>\s*)source_md:\s*(?P<value>.*)$")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Step 0: 如果 books.yaml 提供了 source_pdf，则调用 MinerU 生成 Markdown 并回写 source_md。"
    )
    parser.add_argument("--config", default=None, help="Pipeline config path")
    parser.add_argument("--filter-prefix", action="append", default=None, help="Only process specific book_prefix values")
    parser.add_argument("--limit", type=int, default=None, help="Process at most N books")
    parser.add_argument(
        "--no-overwrite",
        action="store_true",
        help="Do not overwrite existing prepared Markdown outputs",
    )
    parser.add_argument(
        "--failure-report",
        default=None,
        help="Optional path to write failure report jsonl",
    )
    return parser.parse_args()


def _is_candidate_markdown(path: Path) -> bool:
    if path.suffix.lower() != ".md":
        return False
    return not any(token in path.stem for token in IGNORED_MARKDOWN_SUFFIXES)


def _markdown_sort_key(path: Path, root: Path, preferred_stem: str) -> Tuple[int, int, int, int, str]:
    try:
        rel = path.relative_to(root)
    except ValueError:
        rel = path
    rel_text = rel.as_posix().lower()
    exact_stem = 0 if path.stem == preferred_stem else 1
    hybrid_hint = 0 if "hybrid" in rel_text else 1
    depth = len(rel.parts)
    try:
        size_score = -path.stat().st_size
    except OSError:
        size_score = 0
    return (exact_stem, hybrid_hint, depth, size_score, rel.as_posix())


def discover_markdown_output(raw_dir: Path, pdf_stem: str) -> Optional[Path]:
    candidates = [path for path in raw_dir.rglob("*.md") if _is_candidate_markdown(path)]
    if not candidates:
        return None
    candidates.sort(key=lambda path: _markdown_sort_key(path, raw_dir, pdf_stem))
    return candidates[0]


def _write_command_logs(book_dir: Path, stdout: str, stderr: str) -> None:
    (book_dir / "prepare_stdout.txt").write_text(stdout, encoding="utf-8")
    (book_dir / "prepare_stderr.txt").write_text(stderr, encoding="utf-8")


def _render_books_yaml_path(config: PipelineConfig, path: Path) -> str:
    path = path.resolve()
    try:
        return path.relative_to(config.books_yaml.parent.resolve()).as_posix()
    except ValueError:
        return str(path)


def _update_book_source_md(config: PipelineConfig, book_prefix: str, source_md: Path) -> None:
    lines = config.books_yaml.read_text(encoding="utf-8").splitlines()
    rendered_value = json.dumps(_render_books_yaml_path(config, source_md), ensure_ascii=False)
    in_target_book = False

    for idx, line in enumerate(lines):
        match = BOOK_PREFIX_RE.match(line)
        if match:
            in_target_book = match.group("prefix").strip() == book_prefix
            continue
        if not in_target_book:
            continue
        md_match = SOURCE_MD_RE.match(line)
        if md_match:
            lines[idx] = f"{md_match.group('indent')}source_md: {rendered_value}"
            config.books_yaml.write_text("\n".join(lines) + "\n", encoding="utf-8")
            return

    raise ValueError(f"Could not find source_md field for book_prefix={book_prefix} in books.yaml")


def prepare_from_pdf(
    config: PipelineConfig,
    book: Dict[str, Any],
    source_pdf: Path,
    output_md: Path,
) -> Tuple[bool, str, Dict[str, Any]]:
    book_prefix = str(book["book_prefix"])
    book_dir = config.pdf_to_md_book_dir(book_prefix)
    raw_dir = book_dir / "mineru_output"
    raw_dir.mkdir(parents=True, exist_ok=True)

    mineru_cfg = config.mineru
    mineru_command = str(mineru_cfg.get("command", "mineru")).strip() or "mineru"
    cmd = shlex.split(mineru_command) + ["-p", str(source_pdf), "-o", str(raw_dir)]

    try:
        proc = subprocess.run(
            cmd,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except FileNotFoundError:
        write_json(
            config.generated_book_manifest_for(book_prefix),
            {
                "book_prefix": book_prefix,
                "status": "mineru_not_found",
                "source_pdf": str(source_pdf),
                "command": cmd,
            },
        )
        return False, "mineru_not_found", {"command": cmd}

    _write_command_logs(book_dir, proc.stdout, proc.stderr)
    if proc.returncode != 0:
        write_json(
            config.generated_book_manifest_for(book_prefix),
            {
                "book_prefix": book_prefix,
                "status": "mineru_failed",
                "source_pdf": str(source_pdf),
                "command": cmd,
                "returncode": proc.returncode,
            },
        )
        return False, "mineru_failed", {"command": cmd, "returncode": proc.returncode}

    chosen_md = discover_markdown_output(raw_dir, pdf_stem=source_pdf.stem)
    if chosen_md is None:
        write_json(
            config.generated_book_manifest_for(book_prefix),
            {
                "book_prefix": book_prefix,
                "status": "markdown_not_found",
                "source_pdf": str(source_pdf),
                "command": cmd,
                "raw_dir": str(raw_dir),
            },
        )
        return False, "markdown_not_found", {"raw_dir": str(raw_dir)}

    output_md.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(chosen_md, output_md)
    _update_book_source_md(config, book_prefix, output_md)
    manifest = {
        "book_prefix": book_prefix,
        "status": "ok",
        "source_pdf": str(source_pdf),
        "source_md": _render_books_yaml_path(config, output_md),
        "source_kind": "pdf",
        "command": cmd,
        "raw_dir": str(raw_dir),
        "selected_markdown": str(chosen_md),
        "prepared_markdown": str(output_md),
    }
    write_json(config.generated_book_manifest_for(book_prefix), manifest)
    return True, "ok", {"prepared_markdown": str(output_md), "selected_markdown": str(chosen_md)}


def prepare_book(config: PipelineConfig, book: Dict[str, Any], overwrite: bool) -> Tuple[bool, str, Dict[str, Any]]:
    book_prefix = str(book["book_prefix"])
    source_pdf = config.resolve_book_pdf(book)
    source_md = config.resolve_book_markdown(book)

    if source_pdf is not None:
        output_md = source_md or config.generated_book_md_for(book_prefix)
        if output_md.exists() and not overwrite:
            _update_book_source_md(config, book_prefix, output_md)
            return True, "skipped_existing", {"prepared_markdown": str(output_md)}

        if not source_pdf.exists():
            return False, "missing_source_pdf", {"source_pdf": str(source_pdf)}

        book_dir = config.pdf_to_md_book_dir(book_prefix)
        if book_dir.exists() and overwrite:
            shutil.rmtree(book_dir)
        book_dir.mkdir(parents=True, exist_ok=True)
        return prepare_from_pdf(config, book, source_pdf, output_md)

    if source_md is None:
        return False, "missing_source", {}
    if not source_md.exists():
        return False, "missing_source_md", {"source_md": str(source_md)}

    write_json(
        config.generated_book_manifest_for(book_prefix),
        {
            "book_prefix": book_prefix,
            "status": "ok",
            "source_md": str(source_md),
            "source_kind": "markdown",
            "prepared_markdown": str(source_md),
        },
    )
    return True, "ready_source_md", {"prepared_markdown": str(source_md)}


def selected_books(config: PipelineConfig, filter_prefixes: Optional[Sequence[str]]) -> List[Dict[str, Any]]:
    wanted = {item.strip() for item in (filter_prefixes or []) if item.strip()}
    books: List[Dict[str, Any]] = []
    for book in config.load_books(require_source=False):
        if wanted and str(book["book_prefix"]) not in wanted:
            continue
        books.append(book)
    return books


def process_all(
    config_path: Optional[str],
    filter_prefixes: Optional[Sequence[str]],
    limit: Optional[int],
    overwrite: bool,
) -> Dict[str, Any]:
    config = load_config(config_path)
    summary: Dict[str, Any] = {
        "processed": 0,
        "success": 0,
        "skipped": 0,
        "failed": 0,
        "failures": [],
    }

    count = 0
    for book in selected_books(config, filter_prefixes):
        book_prefix = str(book["book_prefix"])
        summary["processed"] += 1
        ok, status, info = prepare_book(config, book, overwrite=overwrite)
        if ok and status == "skipped_existing":
            summary["skipped"] += 1
            print(f"[SKIP] {book_prefix} -> existing source_md")
        elif ok:
            summary["success"] += 1
            print(f"[OK] {book_prefix} -> {info.get('prepared_markdown', '')}")
        else:
            summary["failed"] += 1
            failure = {
                "book_prefix": book_prefix,
                "source_pdf": str(book.get("source_pdf", "") or ""),
                "source_md": str(book.get("source_md", "") or ""),
                "status": status,
                "info": info,
            }
            summary["failures"].append(failure)
            print(f"[FAIL] {book_prefix} ({status})")

        count += 1
        if limit and count >= limit:
            break

    return summary


def main() -> None:
    args = parse_args()
    summary = process_all(
        config_path=args.config,
        filter_prefixes=args.filter_prefix,
        limit=args.limit,
        overwrite=not args.no_overwrite,
    )

    if args.failure_report:
        report_path = Path(args.failure_report)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        with report_path.open("w", encoding="utf-8") as f:
            for item in summary["failures"]:
                f.write(json.dumps(item, ensure_ascii=False) + "\n")

    print("\n===== SUMMARY =====")
    print(json.dumps({k: v for k, v in summary.items() if k != "failures"}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
