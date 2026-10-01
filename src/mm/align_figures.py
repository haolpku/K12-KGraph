#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Step 1 of the multimodal pipeline: match each section with its figures.

Splits the MinerU source markdown by section (using the section markdown produced by
``src/kg/segment_textbooks.py`` as the anchor), collects the image references found in
each section, and writes ``aligned_sections.json`` for the figure-extraction stage.

Output is purely structural: it records which images belong to which section. The
semantic work (is this figure relevant, what does it show, what does it point at) is
done later by ``src/mm/extract_figures.py``.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

# ``python src/mm/align_figures.py`` puts src/mm (not src) on sys.path, so make the
# repository ``src`` root importable before touching ``utils``.
_SRC_DIR = Path(__file__).resolve().parents[1]
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from utils.config import PipelineConfig, load_config  # noqa: E402
from utils.io import write_json  # noqa: E402
from utils.k12_ids import normalize_book_prefix  # noqa: E402


SECTION_FILE_RE = re.compile(r"^ch(?P<chapter>\d+)(?:_s(?P<section>\d+))?\.md$")
IMAGE_RE = re.compile(r"!\[[^\]]*\]\((?P<path>images/[^)]+)\)")


@dataclass
class SectionFile:
    section_id: str
    chapter_number: int
    section_number: int
    path: Path
    title_line: str
    title_text: str
    text: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Align section markdown with textbook figures")
    parser.add_argument("--config", default=None, help="Pipeline config path; defaults to config/default.yaml")
    parser.add_argument("--book-prefix", default=None, help="Book prefix, e.g. math_7a_rjb")
    parser.add_argument("--sections-dir", default=None, help="Override: directory holding the section markdown files")
    parser.add_argument("--hybrid-dir", default=None, help="Override: MinerU directory (markdown + images/)")
    parser.add_argument("--book-root", default=None, help="Override: one book root holding out_sections/ and hybrid_auto/")
    parser.add_argument("--output-dir", default=None, help="Output directory; defaults to <workspace>/mm/<book>")
    return parser.parse_args()


def resolve_input_dirs(args: argparse.Namespace, config: PipelineConfig) -> Tuple[Path, Path, Optional[Path]]:
    """Resolve ``(sections_dir, hybrid_dir, book_root)``.

    ``--book-root`` keeps the plain ``out_sections/ + hybrid_auto/`` layout working;
    otherwise paths come from ``config/default.yaml``.
    """
    if args.book_root:
        book_root = Path(args.book_root).resolve()
        return book_root / "out_sections", book_root / "hybrid_auto", book_root
    if args.sections_dir and args.hybrid_dir:
        return Path(args.sections_dir).resolve(), Path(args.hybrid_dir).resolve(), None
    if not args.book_prefix:
        raise SystemExit("Provide --book-prefix, or --book-root, or both --sections-dir and --hybrid-dir")

    book_prefix = normalize_book_prefix(args.book_prefix)
    sections_dir = config.sections_dir_for(book_prefix)
    if not sections_dir.is_dir():
        raise SystemExit(
            f"Section markdown not found: {sections_dir}\n"
            f"Run `python src/kg/segment_textbooks.py --filter-prefix {book_prefix}` first, "
            f"or pass --sections-dir."
        )
    hybrid_dir = config.hybrid_dir_for(book_prefix)
    if hybrid_dir is None:
        raise SystemExit(
            f"Cannot locate the MinerU source (markdown + images/) for {book_prefix}.\n"
            f"Expected it under {config.pdf_to_md_book_dir(book_prefix)}/mineru_output, "
            f"or pass --hybrid-dir."
        )
    return sections_dir, hybrid_dir, None


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig")


def natural_key(path: Path) -> Tuple[int, int, str]:
    match = SECTION_FILE_RE.match(path.name)
    if not match:
        return (10**9, 10**9, path.name)
    section = match.group("section")
    return (int(match.group("chapter")), int(section) if section is not None else 0, path.name)


def first_nonempty_line(text: str) -> str:
    for line in text.splitlines():
        if line.strip():
            return line.rstrip()
    raise ValueError("Markdown file is empty")


def first_section_heading_line(text: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#") and not stripped.startswith("## 来源:"):
            return line.rstrip()
    return first_nonempty_line(text)


def normalize_heading_line(line: str) -> str:
    return re.sub(r"\s+", " ", line.strip())


def load_sections(sections_dir: Path) -> List[SectionFile]:
    sections: List[SectionFile] = []
    for path in sorted(sections_dir.glob("*.md"), key=natural_key):
        match = SECTION_FILE_RE.match(path.name)
        if not match:
            continue
        text = read_text(path)
        title_line = first_section_heading_line(text)
        title_text = re.sub(r"^#+\s*", "", title_line).strip()
        sections.append(
            SectionFile(
                section_id=path.stem,
                chapter_number=int(match.group("chapter")),
                section_number=int(match.group("section")) if match.group("section") is not None else 0,
                path=path,
                title_line=normalize_heading_line(title_line),
                title_text=title_text,
                text=text,
            )
        )
    if not sections:
        raise FileNotFoundError(f"No section markdown files found in {sections_dir}")
    return sections


def discover_source_markdown(hybrid_dir: Path) -> Path:
    candidates = []
    for path in hybrid_dir.glob("*.md"):
        stem = path.stem
        if stem.endswith("_content_list") or stem.endswith("_content_list_converted") or stem.endswith("_content_list_v2"):
            continue
        candidates.append(path)
    if not candidates:
        raise FileNotFoundError(f"No source markdown found under {hybrid_dir}")
    candidates.sort(key=lambda p: (len(p.name), p.name))
    return candidates[0]


def build_heading_index(lines: Sequence[str]) -> Dict[str, int]:
    index: Dict[str, int] = {}
    for lineno, line in enumerate(lines):
        if line.lstrip().startswith("#"):
            normalized = normalize_heading_line(line)
            index.setdefault(normalized, lineno)
    return index


def split_combined_heading(line: str) -> Optional[Tuple[str, str]]:
    normalized = normalize_heading_line(line)
    if not normalized.startswith("# "):
        return None
    body = normalized[2:].strip()
    parts = body.split(" ", 1)
    if len(parts) != 2:
        return None
    first, second = parts
    if not (first.startswith("\u7b2c") and first.endswith("\u8282")):
        return None
    return (f"# {first}", f"# {second}")


def normalize_content_line(line: str) -> str:
    return re.sub(r"\s+", " ", line.strip())


def first_content_anchor(section_text: str) -> Optional[str]:
    for raw in section_text.splitlines():
        stripped = raw.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            continue
        if IMAGE_RE.search(stripped):
            continue
        return normalize_content_line(stripped)
    return None


def nearest_heading_before(source_lines: Sequence[str], idx: int, lower_bound: int) -> int:
    for pos in range(idx, lower_bound - 1, -1):
        if source_lines[pos].lstrip().startswith("#"):
            return pos
    return idx


def resolve_section_start(section: SectionFile, heading_index: Dict[str, int], source_lines: Sequence[str], search_from: int) -> int:
    direct = heading_index.get(section.title_line)
    if direct is not None and direct >= search_from:
        return direct

    split = split_combined_heading(section.title_line)
    if split:
        first = heading_index.get(split[0])
        second = heading_index.get(split[1])
        if first is not None and second is not None and second - first <= 2 and first >= search_from:
            return first

    anchor = first_content_anchor(section.text)
    if anchor:
        for idx in range(search_from, len(source_lines)):
            if normalize_content_line(source_lines[idx]) == anchor:
                return nearest_heading_before(source_lines, idx, search_from)

    raise KeyError(f"Section heading not found in source markdown: {section.title_line}")


def strip_image_markup(text: str) -> str:
    cleaned_lines: List[str] = []
    blank_run = 0
    for line in text.splitlines():
        if IMAGE_RE.search(line):
            continue
        stripped = line.rstrip()
        if stripped.strip():
            cleaned_lines.append(stripped)
            blank_run = 0
        else:
            blank_run += 1
            if blank_run <= 1:
                cleaned_lines.append("")
    return "\n".join(cleaned_lines).strip() + "\n"


def extract_figure_mentions(slice_lines: Sequence[str], images_dir: Path) -> Tuple[Dict[str, str], List[Dict[str, object]], List[str]]:
    img_dict: Dict[str, str] = {}
    mentions: List[Dict[str, object]] = []
    missing_images: List[str] = []
    img_counter = 1

    for idx, line in enumerate(slice_lines):
        match = IMAGE_RE.search(line)
        if not match:
            continue

        rel_path = match.group("path")
        abs_path = (images_dir.parent / rel_path).resolve()
        img_id = f"img_{img_counter:03d}"
        img_counter += 1
        img_dict[img_id] = str(abs_path)
        if not abs_path.exists():
            missing_images.append(str(abs_path))

        mentions.append(
            {
                "img_id": img_id,
                "image_rel_path": rel_path,
                "image_abs_path": str(abs_path),
            }
        )

    return img_dict, mentions, missing_images


def build_figures(figure_mentions: Sequence[Dict[str, object]]) -> List[Dict[str, object]]:
    figures: List[Dict[str, object]] = []
    for figure_counter, mention in enumerate(figure_mentions, start=1):
        figures.append(
            {
                "figure_id": f"local_fig_{figure_counter:03d}",
                "image_ids": [mention["img_id"]],
                "image_paths": [mention["image_abs_path"]],
                "image_rel_paths": [mention["image_rel_path"]],
            }
        )
    return figures


def section_slice_boundaries(sections: Sequence[SectionFile], heading_index: Dict[str, int], source_lines: Sequence[str]) -> List[Tuple[int, int]]:
    starts: List[int] = []
    cursor = 0
    for section in sections:
        start = resolve_section_start(section, heading_index, source_lines, cursor)
        starts.append(start)
        cursor = max(cursor, start + 1)

    boundaries: List[Tuple[int, int]] = []
    for idx, start in enumerate(starts):
        end = starts[idx + 1] if idx + 1 < len(starts) else len(source_lines)
        boundaries.append((start, end))
    return boundaries


def build_records(sections_dir: Path, hybrid_dir: Path, book_prefix: str, output_dir: Path, book_root: Optional[Path] = None) -> Tuple[List[Dict[str, object]], List[Dict[str, object]], Dict[str, object]]:
    images_dir = hybrid_dir / "images"

    sections = load_sections(sections_dir)
    source_markdown = discover_source_markdown(hybrid_dir)
    source_text = read_text(source_markdown)
    source_lines = source_text.splitlines()
    heading_index = build_heading_index(source_lines)
    boundaries = section_slice_boundaries(sections, heading_index, source_lines)

    aligned_sections: List[Dict[str, object]] = []
    dataflow_input: List[Dict[str, object]] = []
    missing_images: List[str] = []
    image_total = 0
    figure_total = 0

    global_figure_counter = 1

    for section, (start, end) in zip(sections, boundaries):
        source_slice = "\n".join(source_lines[start:end]).strip() + "\n"
        img_dict, figure_mentions, missing = extract_figure_mentions(source_lines[start:end], images_dir)
        figures = build_figures(figure_mentions)
        for figure in figures:
            figure["figure_local_id"] = figure["figure_id"]
            figure["figure_index"] = global_figure_counter
            figure["figure_id"] = f"{book_prefix}_fig{global_figure_counter}"
            global_figure_counter += 1
        missing_images.extend(missing)
        image_total += len(img_dict)
        figure_total += len(figures)

        raw_chunk = strip_image_markup(section.text)
        record = {
            "book_prefix": book_prefix,
            "book_root": str(book_root.resolve()) if book_root else "",
            "sections_dir": str(sections_dir.resolve()),
            "hybrid_dir": str(hybrid_dir.resolve()),
            "section_id": section.section_id,
            "chapter_number": section.chapter_number,
            "section_number": section.section_number,
            "section_title": section.title_text,
            "section_markdown_path": str(section.path.resolve()),
            "source_markdown_path": str(source_markdown.resolve()),
            "raw_chunk": raw_chunk,
            "section_markdown": section.text,
            "source_slice_markdown": source_slice,
            "img_dict": img_dict,
            "figure_mentions": figure_mentions,
            "figures": figures,
            "image_count": len(img_dict),
            "figure_count": len(figures),
        }
        aligned_sections.append(record)
        dataflow_input.append(
            {
                "book_prefix": book_prefix,
                "section_id": section.section_id,
                "section_title": section.title_text,
                "raw_chunk": raw_chunk,
                "img_dict": img_dict,
                "figures": figures,
            }
        )

    report = {
        "book_prefix": book_prefix,
        "book_root": str(book_root.resolve()) if book_root else "",
        "sections_dir": str(sections_dir.resolve()),
        "hybrid_dir": str(hybrid_dir.resolve()),
        "section_count": len(sections),
        "image_count": image_total,
        "figure_count": figure_total,
        "source_markdown": str(source_markdown.resolve()),
        "missing_image_count": len(missing_images),
        "missing_images": missing_images,
        "sections_without_images": [r["section_id"] for r in aligned_sections if not r["img_dict"]],
        "sections_without_figures": [r["section_id"] for r in aligned_sections if not r["figures"]],
        "output_dir": str(output_dir.resolve()),
    }
    return aligned_sections, dataflow_input, report


def main() -> None:
    args = parse_args()
    config = load_config(args.config)
    sections_dir, hybrid_dir, book_root = resolve_input_dirs(args, config)

    default_book_name = book_root.name if book_root else sections_dir.parent.name
    book_prefix = normalize_book_prefix(args.book_prefix or default_book_name)
    output_dir = Path(args.output_dir).resolve() if args.output_dir else config.mm_book_dir(book_prefix)

    aligned_sections, dataflow_input, report = build_records(sections_dir, hybrid_dir, book_prefix, output_dir, book_root=book_root)
    write_json(output_dir / "aligned_sections.json", aligned_sections)
    write_json(output_dir / "alignment_report.json", report)

    print(f"Wrote {len(aligned_sections)} aligned sections to {output_dir}")
    print(f"Recovered {report['image_count']} image references")
    print(f"Grouped them into {report['figure_count']} figure objects")
    if report["missing_image_count"]:
        print(f"Warning: {report['missing_image_count']} referenced images were missing on disk")


if __name__ == "__main__":
    main()
