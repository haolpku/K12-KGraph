#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Step 1: parse textbook Markdown TOC, emit section index, and split section files.

Detects a table-of-contents block (with fallbacks), normalizes the outline to a
two-level chapter/section model, writes ``sections_index.json`` (and a legacy
``section_index.json`` alias), and writes per-section Markdown under the
configured segmentation workspace.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

_SRC_DIR = Path(__file__).resolve().parents[1]
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from utils.config import load_config


STAGE_NAMES = {
    "小学",
    "初中",
    "高中",
    "小学（五•四学制）",
    "初中（五•四学制）",
}

SECTION_KEYWORDS = [
    # "阅读与思考",
    # "观察与猜想",
    # "实验与探究",
    # "探究与发现",
    # "信息技术应用",
    # "数学活动",
    # "综合与实践",
    # "课题学习",
    # "项目",
    # "小结",
    "复习参考题",
    "复习题",
    "习题",
    # "语文园地",
    # "口语交际",
    # "快乐读书吧",
    # "习作",
    # "做一做",
    "练习",
]

CHINESE_DIGITS = {
    "零": 0,
    "〇": 0,
    "一": 1,
    "二": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}

INT_TO_CHINESE = {
    1: "一",
    2: "二",
    3: "三",
    4: "四",
    5: "五",
    6: "六",
    7: "七",
    8: "八",
    9: "九",
    10: "十",
}


def _to_chinese_num(raw: str) -> Optional[str]:
    raw = normalize_text(raw).strip()
    if not raw:
        return None
    if raw.isdigit():
        return INT_TO_CHINESE.get(int(raw))
    v = chinese_to_int(raw)
    if v is None:
        return None
    return INT_TO_CHINESE.get(v)


def normalize_hs_grade_name(book_name: str) -> Optional[str]:
    name = normalize_text(book_name)

    # 选择性必修1 / 选择性必修 第一册 / 选择性必修一
    m = re.search(r"选择性必修\s*第?([一二三四五六七八九十\d]+)\s*册?", name)
    if m:
        n = _to_chinese_num(m.group(1))
        if n:
            return f"选择性必修{n}"

    # 必修 第一册 / 必修1 / 必修一
    m = re.search(r"必修\s*第?([一二三四五六七八九十\d]+)\s*册?", name)
    if m:
        n = _to_chinese_num(m.group(1))
        if n:
            return f"必修{n}"

    return None


def infer_grade_from_book_name(book_name: str) -> str:
    name = normalize_text(book_name)
    hs = normalize_hs_grade_name(name)
    if hs:
        return hs

    # Common K12 grade markers
    m = re.search(r"([一二三四五六七八九]|[1-9])年级[上下]册", name)
    if m:
        # Return full matched token to preserve detail
        return m.group(0)
    m = re.search(r"([一二三四五六七八九]|[1-9])年级", name)
    if m:
        return m.group(0)
    m = re.search(r"(七|八|九|十|十一|十二|[7-9]|1[0-2])年级[上下]册", name)
    if m:
        return m.group(0)
    m = re.search(r"(必修|选择性必修|选修)\s*[第一二三四五六七八九十\d]+册", name)
    if m:
        return m.group(0).replace(" ", "")
    return "未知年级"


def normalize_output_grade(stage: str, raw_grade: str, book_name: str) -> str:
    """Normalize output grade folder name to required granularity.

    - 初中: prefer 七/八/九年级上册|下册
    - 高中: prefer 必修一/必修二/选择性必修一... from book title
    """
    inferred = infer_grade_from_book_name(book_name)

    if stage == "初中":
        # If raw is only 年级 (e.g. 九年级), enrich with 上/下册 when possible.
        if re.fullmatch(r"[一二三四五六七八九十\d]+年级", normalize_text(raw_grade)):
            if re.search(r"年级[上下]册", inferred):
                return inferred
        if re.search(r"年级[上下]册", normalize_text(raw_grade)):
            return raw_grade
        if re.search(r"年级[上下]册", inferred):
            return inferred

    if stage == "高中":
        # Use canonical high-school naming: 必修一/选择性必修一
        hs_from_book = normalize_hs_grade_name(book_name)
        if hs_from_book:
            return hs_from_book
        hs_from_raw = normalize_hs_grade_name(raw_grade)
        if hs_from_raw:
            return hs_from_raw

    # Fallback to inferred label, then raw grade.
    if inferred != "未知年级":
        return inferred
    return raw_grade


@dataclass
class BookRecord:
    book_prefix: str
    source_md: Optional[Path]
    stage: str
    subject: str
    publisher: str
    grade: str
    book_name: str
    source_input: Optional[Path] = None


@dataclass
class TocEntry:
    level: int  # 1 unit, 2 chapter, 3 section
    title: str
    unit_id: Optional[str] = None
    chapter_id: Optional[str] = None
    section_id: Optional[str] = None

    @property
    def normalized_title(self) -> str:
        return normalize_for_match(self.title)


@dataclass
class Segment:
    unit_id: str
    unit_title: str
    chapter_id: str
    chapter_title: str
    section_id: Optional[str]
    section_title: Optional[str]
    start: int
    end: int

    @property
    def filename(self) -> str:
        unit = sanitize_token(self.unit_id) or "unit"
        chap = sanitize_token(self.chapter_id) or "chapter"
        if self.section_id:
            sec = sanitize_token(self.section_id)
            if not sec:
                sec = sanitize_token(self.section_title or "section") or "section"
            return f"u{unit}_ch{chap}_s{sec}.md"
        return f"u{unit}_ch{chap}.md"


def normalize_text(text: str) -> str:
    return unicodedata.normalize("NFKC", text)


def normalize_for_match(text: str) -> str:
    text = normalize_text(text)
    text = re.sub(r"^#+\s*", "", text)
    text = re.sub(r"\$[^$]*\$", "", text)
    text = re.sub(r"\([^)]*\)", "", text)
    # also strip common OCR artifacts like backslashes
    text = re.sub(r"[\\\s\-—_·•\.。,:：;；，、'\"`~!@#$%^&*+=|<>?\[\]{}()]+", "", text)
    return text.lower()


def sanitize_token(token: str) -> str:
    token = normalize_text(token)
    token = re.sub(r"\s+", "", token)
    token = re.sub(r"[\\/:*?\"<>|]+", "_", token)
    token = token.strip("._")
    return token


def cleanup_markdown(text: str) -> str:
    # remove markdown image links and html img tags
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)
    text = re.sub(r"<img\b[^>]*>", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip() + "\n"


def chinese_to_int(raw: str) -> Optional[int]:
    raw = normalize_text(raw).strip()
    if not raw:
        return None
    if raw.isdigit():
        return int(raw)
    if raw == "十":
        return 10
    if "十" in raw:
        left, _, right = raw.partition("十")
        left_num = CHINESE_DIGITS.get(left, 1 if left == "" else None)
        right_num = CHINESE_DIGITS.get(right, 0 if right == "" else None)
        if left_num is None or right_num is None:
            return None
        return left_num * 10 + right_num
    if raw in CHINESE_DIGITS:
        return CHINESE_DIGITS[raw]
    return None


def strip_toc_line(line: str) -> str:
    line = normalize_text(line).strip()
    line = re.sub(r"^#+\s*", "", line)
    # TOC often contains latex-like markers such as $11^{*}$
    line = line.replace("$", "")
    line = line.replace("{", "")
    line = line.replace("}", "")
    line = line.replace("^", "")
    line = line.replace("*", "")
    line = line.replace("\u3000", " ")
    line = re.sub(r"\s*[…\.。·•]+\s*\d+\s*$", "", line)
    line = re.sub(r"\s+\d+\s*$", "", line)
    line = re.sub(r"\s+", " ", line).strip()
    return line


def detect_toc_start(lines: Sequence[str]) -> int:
    for idx, line in enumerate(lines):
        stripped = strip_toc_line(line)
        compact = stripped.replace(" ", "")
        if compact in {"目录", "目錄", "目录页"}:
            return idx
    return -1


def detect_pseudo_toc_start(lines: Sequence[str]) -> int:
    """Detect TOC-like block when there is no explicit '目录' heading.

    Typical pattern: early '# 第一单元' ... '# 第八单元' listing, then正文 repeats '# 第一单元'.
    """
    heading_candidates: List[Tuple[int, str]] = []
    for idx, line in enumerate(lines[:260]):
        s = line.strip()
        if not s.startswith("#"):
            continue
        plain = re.sub(r"^#+\s*", "", normalize_text(s)).strip()
        if re.match(r"^第\s*[一二三四五六七八九十百零〇\d]+\s*(章|单元|课|编)\b", plain):
            heading_candidates.append((idx, plain))

    if len(heading_candidates) < 3:
        return -1

    # Use first heading as pseudo toc start when same heading appears again much later.
    first_idx, first_title = heading_candidates[0]
    first_norm = normalize_for_match(first_title)
    if not first_norm:
        return -1

    for idx in range(first_idx + 30, min(len(lines), 2500)):
        s = lines[idx].strip()
        if not s.startswith("#"):
            continue
        plain = re.sub(r"^#+\s*", "", normalize_text(s)).strip()
        if normalize_for_match(plain) == first_norm:
            return first_idx

    return -1


def parse_chapter_candidate(line: str, fallback_idx: int) -> Optional[Tuple[str, str]]:
    s = strip_toc_line(line)
    if not s:
        return None

    m = re.match(r"^第\s*([一二三四五六七八九十百零〇\d]*)\s*(章|课|编)\s*(.*)$", s)
    if m:
        num_raw = (m.group(1) or "").strip()
        title = (m.group(3) or "").strip() or s
        if num_raw:
            num = chinese_to_int(num_raw)
            chap_id = str(num) if num is not None else sanitize_token(num_raw)
        else:
            chap_id = str(fallback_idx)
        return chap_id, title

    m = re.match(r"^Unit\s+([A-Za-z0-9]+)\s*(.*)$", s, flags=re.IGNORECASE)
    if m:
        chap_id = sanitize_token(m.group(1))
        title = s
        return chap_id, title

    # Numeric-only chapter style: "1 小数乘法"
    m = re.match(r"^(\d+)\s+(.+)$", s)
    if m:
        chap_id = m.group(1)
        title = m.group(2).strip()
        if title:
            return chap_id, title

    return None


def parse_unit_candidate(line: str, fallback_idx: int) -> Optional[Tuple[str, str]]:
    s = strip_toc_line(line)
    if not s:
        return None

    m = re.match(r"^第\s*([一二三四五六七八九十百零〇\d]*)\s*单元\s*(.*)$", s)
    if not m:
        return None

    num_raw = (m.group(1) or "").strip()
    title = (m.group(2) or "").strip() or s
    if num_raw:
        num = chinese_to_int(num_raw)
        unit_id = str(num) if num is not None else sanitize_token(num_raw)
    else:
        unit_id = str(fallback_idx)
    return unit_id, title


def parse_section_candidate(line: str, current_chapter: Optional[str]) -> Optional[Tuple[str, str, str]]:
    s = strip_toc_line(line)
    if not s:
        return None

    # Chinese style section heading in TOC: "第一节 硫及其化合物" / "第一节硫及其化合物"
    m = re.match(r"^第\s*([一二三四五六七八九十百零〇\d]+)\s*节\s*(.*)$", s)
    if m and current_chapter:
        sec_num_raw = m.group(1)
        sec_title = (m.group(2) or "").strip()
        sec_num = sec_num_raw
        conv = chinese_to_int(sec_num_raw)
        if conv is not None:
            sec_num = str(conv)
        title = sec_title or s
        return current_chapter, sec_num, title

    # Chemistry/physics style: "课题1 xxx", "实验活动2 xxx"
    m = re.match(r"^(课题|实验活动)\s*(\d+)\s*(.*)$", s)
    if m and current_chapter:
        prefix = m.group(1)
        sec = f"{prefix}{m.group(2)}"
        rest = (m.group(3) or "").strip()
        title = f"{sec} {rest}".strip()
        return current_chapter, sec, title

    # 21.1 / 21．1 / 211
    m = re.match(r"^(\d+)\s*[\.．。·•]\s*(\d+)\s*(.*)$", s)
    if m:
        ch = m.group(1)
        sec = m.group(2)
        title = (m.group(3) or "").strip() or s
        return ch, sec, title

    # Numeric-dot style: "1. 质点 参考系"
    m = re.match(r"^(\d+)\s*[\.．。·•]\s*(.+)$", s)
    if m and current_chapter:
        sec = m.group(1)
        title = m.group(2).strip()
        if title:
            return current_chapter, sec, title

    # Primary language style: "1 白鹭" / "26忆读书" (guard against long prose lines)
    m = re.match(r"^(\d+)\s+(.+)$", s)
    if m and current_chapter:
        sec = m.group(1)
        title = m.group(2).strip()
        # Avoid promoting prose lines like "20 世纪初, ..." to TOC section entries.
        if title and len(title) <= 32 and not re.search(r"[，,。；;！!？?]", title):
            return current_chapter, sec, title

    for kw in SECTION_KEYWORDS:
        if s.startswith(kw):
            sec = kw
            title = s
            return current_chapter or "1", sec, title

    return None


def extract_toc_block(lines: Sequence[str], toc_start: int) -> Tuple[int, List[str]]:
    block: List[str] = []
    first_chapter_key = ""
    first_key_from_heading = False
    first_key_heading_seen_once = False
    end_idx = min(len(lines), toc_start + 260)

    start_line = toc_start + 1
    toc_marker = strip_toc_line(lines[toc_start]).replace(" ", "") if 0 <= toc_start < len(lines) else ""
    # Pseudo-TOC begins directly with chapter heading (no explicit 目录 line)
    if toc_marker not in {"目录", "目錄", "目录页"}:
        start_line = toc_start

    for idx in range(start_line, min(len(lines), toc_start + 600)):
        line = lines[idx]
        stripped = line.strip()
        if not stripped:
            block.append(line)
            continue
        if stripped.startswith("!["):
            continue

        # If we've already collected a meaningful number of TOC chapter/unit headings and we now hit
        # a heading that is NOT a unit/chapter heading, it's very likely we've entered the body
        # (e.g. "致同学们", "编者的话", "前言", etc.). Stop TOC collection.
        if stripped.startswith("#") and len(block) > 0:
            # Count how many chapter/unit headings we've seen inside TOC so far.
            seen_structured = 0
            for b in block[-80:]:
                bs = b.strip()
                if not bs.startswith("#"):
                    continue
                plain = re.sub(r"^#+\s*", "", normalize_text(bs)).strip()
                if parse_unit_candidate(plain, fallback_idx=1) or parse_chapter_candidate(plain, fallback_idx=1):
                    seen_structured += 1
            if seen_structured >= 3:
                plain = re.sub(r"^#+\s*", "", normalize_text(stripped)).strip()
                if not (parse_unit_candidate(plain, fallback_idx=1) or parse_chapter_candidate(plain, fallback_idx=1)):
                    end_idx = idx
                    break

        # Detect the first TOC item key to know when the body begins.
        # Some books use "单元" as the topmost TOC level (no explicit 章),
        # so we try both unit/chapter candidates.
        if not first_chapter_key:
            unit_candidate = parse_unit_candidate(line, fallback_idx=1)
            if unit_candidate:
                # Use the full TOC line (incl. ordinal) as the key to avoid over-matching
                # short titles like "三角形" against "全等三角形".
                first_chapter_key = normalize_for_match(strip_toc_line(line))
                first_key_from_heading = stripped.startswith("#")
            else:
                candidate = parse_chapter_candidate(line, fallback_idx=1)
                if candidate:
                    first_chapter_key = normalize_for_match(strip_toc_line(line))
                    first_key_from_heading = stripped.startswith("#")

        # End TOC when the first TOC item heading re-appears in the body.
        # If the key was captured from a heading line inside TOC, then the *first* match is still TOC;
        # break only on the second match. If the key came from non-heading TOC lines, then the first
        # heading match is already body and we should break immediately.
        if stripped.startswith("#") and first_chapter_key:
            h = normalize_for_match(stripped)
            if first_chapter_key and (first_chapter_key in h or h in first_chapter_key):
                if first_key_from_heading:
                    if first_key_heading_seen_once:
                        end_idx = idx
                        break
                    first_key_heading_seen_once = True
                else:
                    end_idx = idx
                    break

        block.append(line)
        end_idx = idx + 1

    return end_idx, block


def parse_toc_entries(lines: Sequence[str]) -> List[TocEntry]:
    entries: List[TocEntry] = []
    current_unit: Optional[str] = None
    current_unit_title: Optional[str] = None
    current_chapter: Optional[str] = None
    current_chapter_title: Optional[str] = None
    unit_counter = 0
    chapter_counter = 0

    for raw in lines:
        s = strip_toc_line(raw)
        if not s:
            continue
        if s.replace(" ", "") in {"目录", "目錄"}:
            continue

        is_heading_line = normalize_text(raw).lstrip().startswith("#")

        # 1) Unit heading lines.
        if is_heading_line:
            unit = parse_unit_candidate(s, fallback_idx=unit_counter + 1)
            if unit:
                unit_counter += 1
                current_unit = unit[0]
                current_unit_title = unit[1]
                current_chapter = None
                current_chapter_title = None
                chapter_counter = 0
                entries.append(TocEntry(level=1, title=unit[1], unit_id=unit[0]))
                continue

        # 1.5) Some books split unit title across two heading lines:
        # "# 第七单元" + "# 生物圈中生命的延续和发展".
        if is_heading_line and current_unit and current_chapter is None:
            unit_only = re.match(r"^第\s*[一二三四五六七八九十百零〇\d]+\s*单元\s*$", s)
            if not unit_only and not re.match(r"^第\s*[一二三四五六七八九十百零〇\d]+\s*(章|节|课|编)\b", s):
                # Update the latest unit entry with richer title text.
                for i in range(len(entries) - 1, -1, -1):
                    e = entries[i]
                    if e.level == 1 and e.unit_id == current_unit:
                        base = e.title.strip()
                        if s not in base:
                            entries[i] = TocEntry(level=1, title=f"{base} {s}".strip(), unit_id=e.unit_id)
                        break
                continue

        # 2) TOC heading lines denote chapters.
        if is_heading_line:
            chap = parse_chapter_candidate(s, fallback_idx=chapter_counter + 1)
            if chap:
                chapter_counter += 1
                current_chapter = chap[0]
                current_chapter_title = chap[1]
                entries.append(
                    TocEntry(
                        level=2,
                        title=chap[1],
                        unit_id=current_unit or "1",
                        chapter_id=chap[0],
                    )
                )
                continue

        # 3) Non-heading lines under a chapter/unit are usually sections/lessons.
        # If the TOC has unit level but no explicit chapter level (e.g. chemistry: 单元 -> 课题),
        # treat unit as the "current chapter" anchor for section parsing.
        anchor_chapter = current_chapter or current_unit
        sec = parse_section_candidate(s, current_chapter=anchor_chapter)
        if sec:
            entries.append(
                TocEntry(
                    level=3,
                    title=sec[2],
                    unit_id=current_unit or "1",
                    chapter_id=sec[0],
                    section_id=sec[1],
                )
            )
            continue

        # 4) Fallback: numeric chapter style without heading marker (e.g. "1 小数乘法")
        chap = parse_chapter_candidate(s, fallback_idx=chapter_counter + 1)
        if chap:
            chapter_counter += 1
            current_chapter = chap[0]
            current_chapter_title = chap[1]
            entries.append(
                TocEntry(
                    level=2,
                    title=chap[1],
                    unit_id=current_unit or "1",
                    chapter_id=chap[0],
                )
            )

    # remove duplicate adjacent entries
    deduped: List[TocEntry] = []
    for e in entries:
        if deduped:
            p = deduped[-1]
            if (
                p.level == e.level
                and (p.unit_id or "") == (e.unit_id or "")
                and p.chapter_id == e.chapter_id
                and (p.section_id or "") == (e.section_id or "")
                and p.normalized_title == e.normalized_title
            ):
                continue
        deduped.append(e)
    return deduped


def heading_positions(lines: Sequence[str], start: int = 0, stage: Optional[str] = None) -> List[Tuple[int, str]]:
    out = []
    for i in range(start, len(lines)):
        s = lines[i].strip()
        # Treat some non-heading review blocks as headings to enable splitting.
        if not s.startswith("#"):
            plain = normalize_text(s).strip()
            if re.match(r"^(复习参考题|复习题)\s*[一二三四五六七八九十百零〇\d]*\b", plain):
                out.append((i, plain))
            continue

        if s.startswith("#"):
            # In many textbooks, "section" level may appear as ### (while ####+ are smaller units).
            # Keep up to ### for high-school books; ignore deeper headings.
            if stage == "高中" and s.startswith("####"):
                continue
            out.append((i, re.sub(r"^#+\s*", "", normalize_text(s)).strip()))
    return out


def text_matches(a: str, b: str) -> bool:
    na = normalize_for_match(a)
    nb = normalize_for_match(b)
    if not na or not nb:
        return False
    return na in nb or nb in na


def find_heading_for_entry(
    entry: TocEntry,
    headings: Sequence[Tuple[int, str]],
    start_idx: int,
    end_idx: Optional[int] = None,
) -> Optional[int]:
    for idx, title in headings:
        if idx < start_idx:
            continue
        if end_idx is not None and idx >= end_idx:
            break

        # Numeric fallback for unit/chapter/section ids
        if entry.level == 1 and entry.unit_id and entry.unit_id.isdigit():
            m = re.match(r"^第\s*([一二三四五六七八九十百零〇\d]*)\s*单元", title)
            if m:
                n = m.group(1)
                num = chinese_to_int(n) if n else None
                if (num is not None and str(num) == entry.unit_id) or (n.isdigit() and n == entry.unit_id):
                    return idx

        if entry.level == 2 and entry.chapter_id and entry.chapter_id.isdigit():
            m = re.match(r"^第\s*([一二三四五六七八九十百零〇\d]*)\s*(章|课|编)", title)
            if m:
                n = m.group(1)
                num = chinese_to_int(n) if n else None
                if (num is not None and str(num) == entry.chapter_id) or (n.isdigit() and n == entry.chapter_id):
                    return idx
            if re.match(rf"^\s*{re.escape(entry.chapter_id)}\s", title):
                return idx

        if entry.level == 3 and entry.section_id:
            sid = entry.section_id
            if entry.chapter_id and sid.isdigit() and re.match(rf"^\s*{re.escape(entry.chapter_id)}\s*[\.．。·•]\s*{re.escape(sid)}\b", title):
                return idx
            # High-school physics style section heading in body: "2 动量定理"
            if sid.isdigit() and re.match(rf"^\s*{re.escape(sid)}\s+", title):
                return idx
            # Chemistry style body headings: "课题1" / "实验活动1"
            if sid.isdigit() and re.match(rf"^\s*(课题|实验活动)\s*{re.escape(sid)}\b", title):
                return idx
            if not sid.isdigit() and sid in title:
                return idx

        # Fuzzy match only after structural matches; avoid over-matching very short chapter titles like "光".
        if text_matches(entry.title, title):
            if entry.level == 2 and len(normalize_for_match(entry.title)) <= 2:
                continue
            return idx

    return None


def build_segments_from_toc(lines: Sequence[str], toc_start: int, toc_entries: Sequence[TocEntry], body_start: int, stage: Optional[str] = None) -> List[Segment]:
    units = [e for e in toc_entries if e.level == 1]
    chapters = [e for e in toc_entries if e.level == 2]
    if not chapters:
        return []

    headings = heading_positions(lines, start=body_start, stage=stage)
    if not headings:
        return []

    unit_title_map: Dict[str, str] = {}
    for u in units:
        if u.unit_id:
            prev = unit_title_map.get(u.unit_id, "")
            cand = (u.title or "").strip()
            # Keep the richer (longer) unit title when duplicate unit ids appear.
            if not prev or len(normalize_text(cand)) > len(normalize_text(prev)):
                unit_title_map[u.unit_id] = cand

    chapter_starts: List[Tuple[TocEntry, int]] = []
    cursor = body_start
    for ch in chapters:
        pos = find_heading_for_entry(ch, headings, start_idx=cursor)
        if pos is None:
            continue
        chapter_starts.append((ch, pos))
        cursor = pos + 1

    if not chapter_starts:
        return []

    chapter_sections: Dict[Tuple[str, str], List[TocEntry]] = {}
    for e in toc_entries:
        if e.level == 3 and e.unit_id and e.chapter_id:
            chapter_sections.setdefault((e.unit_id, e.chapter_id), []).append(e)

    segments: List[Segment] = []

    for i, (ch_entry, ch_start) in enumerate(chapter_starts):
        ch_end = chapter_starts[i + 1][1] if i + 1 < len(chapter_starts) else len(lines)
        unit_id = ch_entry.unit_id or "1"
        chapter_id = ch_entry.chapter_id or "1"
        sec_entries = chapter_sections.get((unit_id, chapter_id), [])

        if not sec_entries:
            segments.append(
                Segment(
                    unit_id=unit_id,
                    unit_title=unit_title_map.get(unit_id, ""),
                    chapter_id=chapter_id,
                    chapter_title=ch_entry.title,
                    section_id=None,
                    section_title=None,
                    start=ch_start,
                    end=ch_end,
                )
            )
            continue

        sec_starts: List[Tuple[TocEntry, int]] = []
        sec_cursor = ch_start
        for sec in sec_entries:
            pos = find_heading_for_entry(sec, headings, start_idx=sec_cursor, end_idx=ch_end)
            if pos is None:
                continue
            sec_starts.append((sec, pos))
            sec_cursor = pos + 1

        if not sec_starts:
            segments.append(
                Segment(
                    unit_id=unit_id,
                    unit_title=unit_title_map.get(unit_id, ""),
                    chapter_id=chapter_id,
                    chapter_title=ch_entry.title,
                    section_id=None,
                    section_title=None,
                    start=ch_start,
                    end=ch_end,
                )
            )
            continue

        for j, (sec_entry, sec_start) in enumerate(sec_starts):
            sec_end = sec_starts[j + 1][1] if j + 1 < len(sec_starts) else ch_end
            segments.append(
                Segment(
                    unit_id=unit_id,
                    unit_title=unit_title_map.get(unit_id, ""),
                    chapter_id=chapter_id,
                    chapter_title=ch_entry.title,
                    section_id=sec_entry.section_id,
                    section_title=sec_entry.title,
                    start=sec_start,
                    end=sec_end,
                )
            )

    return segments


def build_segments_from_headings(lines: Sequence[str], stage: Optional[str] = None) -> Tuple[int, List[Segment]]:
    headings = heading_positions(lines, start=0, stage=stage)
    if not headings:
        return 0, []

    chapter_candidates: List[Tuple[int, str, str]] = []
    chapter_counter = 0
    for idx, title in headings:
        m = re.match(r"^第\s*([一二三四五六七八九十百零〇\d]+)\s*(章|单元|课|编)\s*(.*)$", title)
        if m:
            num = m.group(1)
            cnum = chinese_to_int(num)
            cid = str(cnum) if cnum is not None else sanitize_token(num)
            ctitle = m.group(3).strip() or title
            chapter_candidates.append((idx, cid, ctitle))
            continue
        m = re.match(r"^(\d+)\s+(.+)$", title)
        if m:
            chapter_candidates.append((idx, m.group(1), m.group(2).strip()))

    if not chapter_candidates:
        return headings[0][0], []

    segments: List[Segment] = []
    for i, (start, cid, ctitle) in enumerate(chapter_candidates):
        end = chapter_candidates[i + 1][0] if i + 1 < len(chapter_candidates) else len(lines)
        segments.append(
            Segment(
                unit_id="1",
                unit_title="",
                chapter_id=cid or str(chapter_counter + 1),
                chapter_title=ctitle,
                section_id=None,
                section_title=None,
                start=start,
                end=end,
            )
        )
        chapter_counter += 1

    return chapter_candidates[0][0], segments


def split_markdown(content: str, stage: Optional[str] = None) -> Tuple[str, List[Segment], Dict[str, object]]:
    lines = content.splitlines()
    toc_start = detect_toc_start(lines)
    if toc_start < 0:
        toc_start = detect_pseudo_toc_start(lines)
    info: Dict[str, object] = {"toc_found": toc_start >= 0}

    if toc_start >= 0:
        body_start, toc_block = extract_toc_block(lines, toc_start)
        toc_entries = parse_toc_entries(toc_block)
        info["toc_entries"] = len(toc_entries)
        info["has_unit_level"] = any(e.level == 1 for e in toc_entries)
        segments = build_segments_from_toc(lines, toc_start, toc_entries, body_start=body_start, stage=stage)
        if segments:
            metadata = "\n".join(lines[:toc_start])
            info["mode"] = "toc"
            return metadata, segments, info

    # fallback
    first_heading, segments = build_segments_from_headings(lines, stage=stage)
    info["mode"] = "fallback"
    info["has_unit_level"] = False
    metadata = "\n".join(lines[:first_heading]) if first_heading > 0 else ""
    return metadata, segments, info


def parse_toc_entries_from_content(content: str) -> Optional[List[TocEntry]]:
    lines = content.splitlines()
    toc_start = detect_toc_start(lines)
    if toc_start < 0:
        toc_start = detect_pseudo_toc_start(lines)
    if toc_start < 0:
        return None
    _, toc_block = extract_toc_block(lines, toc_start)
    toc_entries = parse_toc_entries(toc_block)
    return toc_entries or None


def build_segments_for_index_from_toc_entries(toc_entries: Sequence[TocEntry]) -> List[Segment]:
    unit_title_map: Dict[str, str] = {}
    chapter_title_map: Dict[Tuple[str, str], str] = {}
    out: List[Segment] = []

    for e in toc_entries:
        if e.level == 1 and e.unit_id:
            unit_title_map[e.unit_id] = e.title
            continue

        if e.level == 2 and e.unit_id and e.chapter_id:
            chapter_title_map[(e.unit_id, e.chapter_id)] = e.title
            continue

        if e.level == 3 and e.chapter_id and e.section_id:
            uid = e.unit_id or "1"
            cid = e.chapter_id
            ctitle = chapter_title_map.get((uid, cid), "")
            out.append(
                Segment(
                    unit_id=uid,
                    unit_title=unit_title_map.get(uid, ""),
                    chapter_id=cid,
                    chapter_title=ctitle,
                    section_id=e.section_id,
                    section_title=e.title,
                    start=0,
                    end=0,
                )
            )

    return out


def build_two_level_index_items_from_toc_entries(toc_entries: Sequence[TocEntry]) -> Tuple[List[Dict[str, str]], Dict[str, object]]:
    """Coerce any parsed TOC into at most two levels: chapter -> section.

    Rules (per user requirement):
    - Always output at most two levels: chapter (top) and section (second).
    - If TOC has more than two levels, ignore smaller levels by grouping them under the same section.
    - If TOC has only one level, output chapter only (no sections).
    - If TOC has Unit level (level=1), treat Unit as chapter, and treat Chapter (level=2) as section.
      Any Section entries (level=3) are ignored.
    - If there is no Unit level, treat Chapter (level=2) as chapter, and Section (level=3) as section.
    """
    has_unit_level = any(e.level == 1 for e in toc_entries)
    has_section_level = any(e.level == 3 for e in toc_entries)

    index_items: List[Dict[str, str]] = []

    if has_unit_level:
        # Two-level coercion when TOC contains unit level:
        # - If TOC also has chapter level (level=2): unit -> chapter, chapter -> section (ignore level=3).
        # - Otherwise (unit + lessons only): unit -> chapter, level=3 -> section.
        unit_titles: Dict[str, str] = {}
        unit_order: List[str] = []
        for e in toc_entries:
            if e.level == 1 and e.unit_id:
                if e.unit_id not in unit_titles:
                    unit_order.append(e.unit_id)
                prev = unit_titles.get(e.unit_id, "")
                cand = (e.title or "").strip()
                if not prev or len(normalize_text(cand)) > len(normalize_text(prev)):
                    unit_titles[e.unit_id] = cand

        chapters_by_unit: Dict[str, List[TocEntry]] = {}
        for e in toc_entries:
            if e.level == 2 and e.unit_id and e.chapter_id:
                chapters_by_unit.setdefault(e.unit_id, []).append(e)

        sections_by_unit: Dict[str, List[TocEntry]] = {}
        # Only use level=3 as sections when there is no chapter level in TOC.
        if not chapters_by_unit:
            for e in toc_entries:
                if e.level == 3 and e.unit_id and e.section_id:
                    # When TOC is unit -> (课题/实验活动...), parse_toc_entries anchors chapter_id to unit_id.
                    sections_by_unit.setdefault(e.unit_id, []).append(e)

        for uid in unit_order:
            utitle = unit_titles.get(uid, "")
            secs = sections_by_unit.get(uid, [])
            chs = chapters_by_unit.get(uid, [])

            if not secs and not chs:
                # Unit-only TOC: chapter only.
                chapter_token = sanitize_token(uid) or "chapter"
                index_items.append(
                    {
                        "chapter_num": uid,
                        "chapter_title": _with_unit_prefix(uid, utitle) if utitle else _with_chapter_prefix(uid, utitle),
                        "section_num": "",
                        "section_title": "",
                        "file": f"ch{chapter_token}.md",
                    }
                )
                continue

            # If chapter level exists, it is our section level (ignore smaller levels).
            if chs:
                for ch in chs:
                    sid = ch.chapter_id or ""
                    stitle = (ch.title or "").strip()
                    chapter_token = sanitize_token(uid) or "chapter"
                    section_token = sanitize_token(sid) or sanitize_token(stitle) or "section"
                    index_items.append(
                        {
                            "chapter_num": uid,
                            "chapter_title": _with_unit_prefix(uid, utitle) if utitle else _with_chapter_prefix(uid, utitle),
                            "section_num": sid,
                            "section_title": stitle,
                            "file": f"ch{chapter_token}_s{section_token}.md",
                        }
                    )
                continue

            # Otherwise, use level=3 lessons under unit as sections.
            for s in secs:
                sid = s.section_id or ""
                stitle = (s.title or "").strip()
                chapter_token = sanitize_token(uid) or "chapter"
                section_token = sanitize_token(sid) or sanitize_token(stitle) or "section"
                index_items.append(
                    {
                        "chapter_num": uid,
                        "chapter_title": _with_unit_prefix(uid, utitle) if utitle else _with_chapter_prefix(uid, utitle),
                        "section_num": sid,
                        "section_title": stitle,
                        "file": f"ch{chapter_token}_s{section_token}.md",
                    }
                )
            continue

        info = {
            "coerced_two_levels": True,
            "toc_has_unit_level": True,
            "toc_has_section_level": has_section_level,
            "chapter_kind": "unit",
            "section_kind": "chapter" if chapters_by_unit else "section",
        }
        return index_items, info

    # No unit level: chapter -> section (ignore deeper than section, but we only parse up to level 3).
    chapter_titles: Dict[str, str] = {}
    chapter_order: List[str] = []
    for e in toc_entries:
        if e.level == 2 and e.chapter_id:
            if e.chapter_id not in chapter_titles:
                chapter_order.append(e.chapter_id)
            prev = chapter_titles.get(e.chapter_id, "")
            cand = (e.title or "").strip()
            if not prev or len(normalize_text(cand)) > len(normalize_text(prev)):
                chapter_titles[e.chapter_id] = cand

    sections_by_chapter: Dict[str, List[TocEntry]] = {}
    for e in toc_entries:
        if e.level == 3 and e.chapter_id and e.section_id:
            sections_by_chapter.setdefault(e.chapter_id, []).append(e)

    for cid in chapter_order:
        ctitle = chapter_titles.get(cid, "")
        secs = sections_by_chapter.get(cid, [])
        chapter_token = sanitize_token(cid) or "chapter"
        if not secs:
            index_items.append(
                {
                    "chapter_num": cid,
                    "chapter_title": _with_chapter_prefix(cid, ctitle),
                    "section_num": "",
                    "section_title": "",
                    "file": f"ch{chapter_token}.md",
                }
            )
            continue
        for s in secs:
            sid = s.section_id or ""
            stitle = (s.title or "").strip()
            section_token = sanitize_token(sid) or sanitize_token(stitle) or "section"
            index_items.append(
                {
                    "chapter_num": cid,
                    "chapter_title": _with_chapter_prefix(cid, ctitle),
                    "section_num": sid,
                    "section_title": stitle,
                    "file": f"ch{chapter_token}_s{section_token}.md",
                }
            )

    info = {
        "coerced_two_levels": True,
        "toc_has_unit_level": False,
        "toc_has_section_level": has_section_level,
        "chapter_kind": "chapter",
        "section_kind": "section",
    }
    return index_items, info


def find_source_pdf(md_path: Path) -> Optional[Path]:
    parent = md_path.parent
    stem = md_path.stem

    prefer = list(parent.glob("*_origin.pdf"))
    if prefer:
        return prefer[0]

    exact = parent / f"{stem}.pdf"
    if exact.exists():
        return exact

    layout = list(parent.glob("*_layout.pdf"))
    if layout:
        return layout[0]

    any_pdf = list(parent.glob("*.pdf"))
    if any_pdf:
        return any_pdf[0]
    return None


def _fixed_search_roots(
    input_root: Path,
    stage: Optional[str],
    subject: Optional[str],
    publisher: Optional[str],
) -> List[Path]:
    """Build deterministic roots from known docs structure.

    This avoids expensive full-tree recursive scans in very large datasets.
    """
    root = input_root
    if stage:
        root = root / stage
    if subject:
        root = root / subject
    if publisher:
        root = root / publisher

    if root.exists():
        return [root]
    return []


def _iter_md_candidates(base: Path, grade: Optional[str]) -> Iterable[Path]:
    """Yield markdown candidates via fixed-depth glob patterns.

    Supported structures:
    1) <publisher>/<grade>/<book>/hybrid_auto/*.md
    2) <publisher>/<book>/hybrid_auto/*.md
    """
    patterns: List[str] = []
    if grade:
        patterns.extend([
            f"{grade}/*/hybrid_auto/*.md",
            f"{grade}/hybrid_auto/*.md",
        ])
    else:
        patterns.extend([
            "*/*/hybrid_auto/*.md",
            "*/hybrid_auto/*.md",
        ])

    seen: set[str] = set()
    for pattern in patterns:
        for p in base.glob(pattern):
            key = str(p)
            if key in seen:
                continue
            seen.add(key)
            yield p


def discover_books(
    input_root: Path,
    stage: Optional[str] = None,
    subject: Optional[str] = None,
    publisher: Optional[str] = None,
    grade: Optional[str] = None,
) -> Iterable[BookRecord]:
    roots = _fixed_search_roots(input_root, stage, subject, publisher)
    for base in roots:
        for md in _iter_md_candidates(base, grade=grade):
            name = md.name
            if any(x in name for x in ["_content_list", "_model", "sections_index"]):
                continue

            parts = md.parts
            stage_idx = -1
            for i, part in enumerate(parts):
                if part in STAGE_NAMES:
                    stage_idx = i
                    break
            if stage_idx < 0:
                continue

            try:
                hybrid_idx = parts.index("hybrid_auto")
            except ValueError:
                continue

            rel = list(parts[stage_idx + 1 : hybrid_idx])
            # Expect at least subject/publisher/book
            if len(rel) < 3:
                continue

            rec_stage = parts[stage_idx]
            rec_subject = rel[0]
            rec_publisher = rel[1]

            if len(rel) == 3:
                # Structure: <subject>/<publisher>/<book>/hybrid_auto
                book_dir = rel[2]
                rec_grade = infer_grade_from_book_name(book_dir)
            else:
                # Structure: <subject>/<publisher>/<grade>/<book>/hybrid_auto
                rec_grade = rel[2]
                book_dir = rel[-1]

            rec_grade = normalize_output_grade(rec_stage, rec_grade, book_dir)

            yield BookRecord(
                book_prefix="",
                source_md=md,
                stage=rec_stage,
                subject=rec_subject,
                publisher=rec_publisher,
                grade=normalize_grade_name(rec_stage, rec_grade, book_dir),
                book_name=book_dir,
            )


def normalize_grade_name(stage: str, grade_from_path: str, book_name: str) -> str:
    inferred = infer_grade_from_book_name(book_name)

    if stage == "高中":
        # User requests high-school folders use 必修一/选择性必修一 style.
        if any(k in inferred for k in ["必修", "选择性必修", "选修"]):
            return inferred
        return grade_from_path

    # For middle/primary, prefer 上/下册 when path only has 年级.
    if re.fullmatch(r"[一二三四五六七八九\d]+年级", grade_from_path):
        if re.fullmatch(r"[一二三四五六七八九\d]+年级[上下]册", inferred):
            return inferred

    return grade_from_path


def should_skip_protected(record: BookRecord) -> bool:
    return False


def _int_to_chinese_for_ordinal(num: int) -> Optional[str]:
    if num <= 0:
        return None
    if num <= 10:
        return INT_TO_CHINESE.get(num)
    if num < 20:
        ones = num % 10
        return f"十{INT_TO_CHINESE.get(ones, '')}" if ones else "十"
    if num < 100:
        tens = num // 10
        ones = num % 10
        tens_cn = INT_TO_CHINESE.get(tens)
        if not tens_cn:
            return None
        return f"{tens_cn}十{INT_TO_CHINESE.get(ones, '')}" if ones else f"{tens_cn}十"
    return str(num)


def _with_chapter_prefix(chapter_num: str, title: str) -> str:
    t = (title or "").strip()
    if re.match(r"^第\s*[一二三四五六七八九十百零〇\d]+\s*章\b", normalize_text(t)):
        return t
    if chapter_num.isdigit():
        cn = _int_to_chinese_for_ordinal(int(chapter_num))
        if cn:
            return f"第{cn}章 {t}".strip()
    return t


def _with_unit_prefix(unit_num: str, title: str) -> str:
    t = (title or "").strip()
    if re.match(r"^第\s*[一二三四五六七八九十百零〇\d]+\s*单元\b", normalize_text(t)):
        return t
    if unit_num.isdigit():
        cn = _int_to_chinese_for_ordinal(int(unit_num))
        if cn:
            return f"第{cn}单元 {t}".strip()
    return t


def _with_section_prefix(section_num: str, title: str) -> str:
    t = (title or "").strip()
    if re.match(r"^第\s*[一二三四五六七八九十百零〇\d]+\s*节\b", normalize_text(t)):
        return t
    if section_num.isdigit():
        cn = _int_to_chinese_for_ordinal(int(section_num))
        if cn:
            return f"第{cn}节 {t}".strip()
    return t


def _normalize_hs_physics_section_title(title: str) -> str:
    """Normalize OCR artifacts in high-school physics TOC section titles.

    Common bad patterns:
    - "第一节 .质点 参考系"
    - "第二节 . 匀变速..."
    """
    t = (title or "").strip()
    if not t:
        return t

    # If title starts with 第X节, strip punctuation/spaces immediately after it.
    m = re.match(r"^(第\s*[一二三四五六七八九十百零〇\d]+\s*节)\s*([\.．。·•]+)\s*(.*)$", normalize_text(t))
    if m:
        head = m.group(1)
        tail = (m.group(3) or "").strip()
        return f"{head} {tail}".strip()

    # Fallback: remove leading dot-like punctuation for non-prefixed titles.
    t = re.sub(r"^[\s\.．。·•]+", "", t)
    return t.strip()


def _special_section_file_token(section_id: str, section_title: str) -> str:
    sid = sanitize_token(section_id)
    if sid == "复习题":
        title = normalize_text(section_title or "").strip()
        m = re.match(r"^(复习题\s*[一二三四五六七八九十百零〇\d]+)", title)
        if m:
            return sanitize_token(m.group(1).replace(" ", "")) or sid
    if sid == "复习参考题":
        title = normalize_text(section_title or "").strip()
        m = re.match(r"^(复习参考题\s*[A-Za-z一二三四五六七八九十百零〇\d]+)", title)
        if m:
            return sanitize_token(m.group(1).replace(" ", "")) or sid
    return sid


def _keep_math_section_for_index(section_id: str, section_title: str) -> bool:
    """Keep only '第x节' (numeric ids) and '复习题x' style entries for math."""
    sid = normalize_text(section_id or "").strip()
    if sid.isdigit():
        return True

    title = normalize_text(section_title or "")
    compact = re.sub(r"\s+", "", title)
    return re.match(r"^复习题[一二三四五六七八九十百零〇\d]+$", compact) is not None


def build_index_items(
    segments: Sequence[Segment],
    has_unit_level: bool,
    hs_physics_mode: bool = False,
    force_numeric_sections: bool = False,
) -> List[Dict[str, str]]:
    index_items: List[Dict[str, str]] = []
    chapter_section_order: Dict[Tuple[str, str], int] = {}

    for seg in segments:
        flatten_unit_to_chapter = has_unit_level and not seg.section_id

        source_chapter_id = seg.unit_id if flatten_unit_to_chapter else seg.chapter_id
        source_chapter_title = seg.unit_title if flatten_unit_to_chapter else seg.chapter_title
        source_section_id = seg.chapter_id if flatten_unit_to_chapter else seg.section_id
        source_section_title = seg.chapter_title if flatten_unit_to_chapter else seg.section_title

        if force_numeric_sections:
            # For junior/high math index, keep only '第x节' and '复习题x'.
            if not source_section_id:
                continue
            if not _keep_math_section_for_index(str(source_section_id), source_section_title or ""):
                continue

        output_section_num = ""
        chapter_token = sanitize_token(source_chapter_id) or "chapter"
        output_filename = f"ch{chapter_token}.md"

        if source_section_id:
            original_is_numeric = str(source_section_id).isdigit()
            # Numeric section ids are normalized to chapter-local sequence index.
            if force_numeric_sections or original_is_numeric:
                key = (source_chapter_id, source_chapter_id)
                chapter_section_order.setdefault(key, 0)
                chapter_section_order[key] += 1
                output_section_num = str(chapter_section_order[key])
            else:
                output_section_num = str(source_section_id)

            if force_numeric_sections:
                if original_is_numeric:
                    section_token = sanitize_token(str(source_section_id)) or "section"
                else:
                    section_token = _special_section_file_token(str(source_section_id), source_section_title or "") or "section"
            else:
                section_token = sanitize_token(output_section_num) or "section"
            output_filename = f"ch{chapter_token}_s{section_token}.md"

        # For forced-numeric sections in junior math:
        # - numeric sections keep original ordinal (第x节)
        # - special sections keep original TOC title text
        if source_section_id and force_numeric_sections and str(source_section_id).isdigit():
            section_title = _with_section_prefix(str(source_section_id), source_section_title or "")
        elif source_section_id and force_numeric_sections:
            section_title = (source_section_title or "").strip()
        else:
            section_title = _with_section_prefix(output_section_num, source_section_title or "")
        if hs_physics_mode:
            section_title = _normalize_hs_physics_section_title(section_title)

        item: Dict[str, str] = {
            "chapter_num": source_chapter_id,
            "chapter_title": _with_chapter_prefix(source_chapter_id, source_chapter_title),
            "section_num": output_section_num,
            "section_title": section_title,
            "file": output_filename,
        }
        index_items.append(item)

    return index_items


def _find_entry_heading(
    headings: Sequence[Tuple[int, str]],
    item: Dict[str, str],
    start_idx: int,
) -> Optional[int]:
    title = item.get("section_title") or item.get("chapter_title") or item.get("unit_title") or ""
    unit_num = str(item.get("unit_num", "")).strip()
    chapter_num = str(item.get("chapter_num", "")).strip()
    section_num = str(item.get("section_num", "")).strip()
    is_review_item = bool(re.match(r"^复习(参考)?题", normalize_text(section_num))) or bool(
        re.match(r"^复习(参考)?题", normalize_text(title))
    )

    # If the body uses explicit "第x节" headings, prefer them and avoid
    # matching loose numeric subheadings like "2. ..." inside a section.
    has_explicit_section_headings = False
    has_chapter_dot_section_headings = False
    if section_num.isdigit():
        for idx, heading_title in headings:
            if idx < start_idx:
                continue
            if re.match(r"^第\s*[一二三四五六七八九十百零〇\d]+\s*节\b", heading_title):
                has_explicit_section_headings = True
                break
            if chapter_num.isdigit() and re.match(
                rf"^\s*{re.escape(chapter_num)}\s*[\.．。·•]\s*\d+\b", heading_title
            ):
                has_chapter_dot_section_headings = True
                # don't break; still might find 第x节 which is even stronger

    for idx, heading_title in headings:
        if idx < start_idx:
            continue

        if is_review_item and normalize_text(heading_title).strip().startswith("复习"):
            # Avoid cross-chapter mis-match like using "复习参考题10" for chapter 9.
            if chapter_num.isdigit():
                m = re.search(r"(\d+)", normalize_text(heading_title))
                if m and m.group(1) != chapter_num:
                    continue

        # For math subjects with Arabic section_num, prioritize exact title match first
        if title and section_num.isdigit():
            # Exact match for math sections
            if normalize_for_match(title) == normalize_for_match(heading_title):
                return idx
            # Remove "第x节" prefix from title for matching actual headings (for math)
            clean_title = re.sub(r'^第\s*[一二三四五六七八九十百零〇\d]+\s*节\s*', '', title).strip()
            if clean_title and normalize_for_match(clean_title) == normalize_for_match(heading_title):
                return idx
            # Or if heading starts with chapter.section format like "4.1 数列的概念"
            chapter_num_int = chapter_num.isdigit() and int(chapter_num) or 0
            if chapter_num_int and re.match(rf"^\s*{chapter_num_int}\s*\.\s*{re.escape(section_num)}\s*", heading_title):
                return idx
            # Or if heading starts with section number followed by title
            if re.match(rf"^\s*{re.escape(section_num)}\s*[\.．。·•]?\s*{re.escape(title)}", heading_title, re.IGNORECASE):
                return idx
            # Or if heading contains the section number and title (for math)
            if section_num in heading_title and title in heading_title:
                return idx

        # General title match (but less priority for math)
        if title and text_matches(title, heading_title):
            return idx

        if not section_num and not chapter_num and unit_num.isdigit():
            m = re.match(r"^第\s*([一二三四五六七八九十百零〇\d]+)\s*单元", heading_title)
            if m:
                num = chinese_to_int(m.group(1))
                if num is not None and str(num) == unit_num:
                    return idx

        # Section numeric fallback: 第X节
        if section_num.isdigit():
            m = re.match(r"^第\s*([一二三四五六七八九十百零〇\d]+)\s*节", heading_title)
            if m:
                num = chinese_to_int(m.group(1))
                if num is not None and str(num) == section_num:
                    return idx
            # Only allow loose numeric headings when there are no explicit section headings:
            # - "第x节 ..." (common in junior physics/chem)
            # - "<chapter>.<section> ..." (common in math)
            if not has_explicit_section_headings and not has_chapter_dot_section_headings:
                # Physics-style heading: "1. 质点 参考系"
                if re.match(rf"^\s*{re.escape(section_num)}\s*[\.．。·•]\s*", heading_title):
                    return idx
                # Physics-style heading: "1 质点 参考系"
                if re.match(rf"^\s*{re.escape(section_num)}\s+", heading_title):
                    return idx

        # Chapter numeric fallback: 第X章/单元/课/编
        if not section_num and chapter_num.isdigit():
            m = re.match(r"^第\s*([一二三四五六七八九十百零〇\d]*)\s*(章|单元|课|编)", heading_title)
            if m:
                n = m.group(1)
                num = chinese_to_int(n) if n else None
                if (num is not None and str(num) == chapter_num) or (n.isdigit() and n == chapter_num):
                    return idx

    return None


def build_file_blocks_from_index(content: str, index_items: Sequence[Dict[str, str]], stage: Optional[str] = None) -> Tuple[str, List[Tuple[str, str]], Dict[str, object]]:
    """Build output markdown blocks by current sections_index.json order and titles.

    Returns: metadata_text, [(filename, cleaned_markdown)], info
    """
    lines = content.splitlines()
    toc_start = detect_toc_start(lines)
    if toc_start < 0:
        toc_start = detect_pseudo_toc_start(lines)

    if toc_start >= 0:
        body_start, _ = extract_toc_block(lines, toc_start)
        metadata_text = "\n".join(lines[:toc_start])
    else:
        body_start = 0
        metadata_text = ""

    headings = heading_positions(lines, start=body_start, stage=stage)
    if not headings:
        return metadata_text, [], {"mode": "index", "matched": 0, "total": len(index_items), "missing": len(index_items)}

    starts: List[Tuple[Dict[str, str], int]] = []
    cursor = body_start
    missing = 0
    for item in index_items:
        pos = _find_entry_heading(headings, item, start_idx=cursor)
        if pos is None:
            missing += 1
            continue
        starts.append((item, pos))
        cursor = pos + 1

    blocks: List[Tuple[str, str]] = []
    tail_heading_re = re.compile(r"^(后记|附录|索引|参考文献|答案|致谢|编后记|后记与说明)\b")
    for i, (item, start) in enumerate(starts):
        end = starts[i + 1][1] if i + 1 < len(starts) else len(lines)

        # If chapter/unit changes at next item, stop this section at the next chapter heading,
        # not at the next section heading.
        if i + 1 < len(starts):
            next_item = starts[i + 1][0]
            cur_unit = str(item.get("unit_num", "")).strip()
            cur_ch = str(item.get("chapter_num", "")).strip()
            nxt_unit = str(next_item.get("unit_num", "")).strip()
            nxt_ch = str(next_item.get("chapter_num", "")).strip()
            if (cur_unit != nxt_unit) or (cur_ch != nxt_ch):
                unit_boundary = None
                if cur_unit != nxt_unit and nxt_unit:
                    unit_boundary = _find_entry_heading(
                        headings,
                        {
                            "unit_num": nxt_unit,
                            "unit_title": next_item.get("unit_title", ""),
                            "chapter_num": "",
                            "chapter_title": "",
                            "section_num": "",
                            "section_title": "",
                        },
                        start_idx=start + 1,
                    )

                chapter_boundary = _find_entry_heading(
                    headings,
                    {
                        "unit_num": nxt_unit,
                        "unit_title": next_item.get("unit_title", ""),
                        "chapter_num": nxt_ch,
                        "chapter_title": next_item.get("chapter_title", ""),
                        "section_num": "",
                        "section_title": "",
                    },
                    start_idx=start + 1,
                )
                if unit_boundary is not None and unit_boundary < end:
                    end = unit_boundary
                if chapter_boundary is not None and chapter_boundary < end:
                    end = chapter_boundary

        # For the last section, drop trailing tail matter like 附录/后记/索引.
        if i + 1 == len(starts):
            for h_idx, h_title in headings:
                if h_idx <= start:
                    continue
                if tail_heading_re.match(normalize_text(h_title).strip()):
                    end = min(end, h_idx)
                    break

        filename = item.get("file", "").strip()
        if not filename:
            unit_num = str(item.get("unit_num", "")).strip()
            unit_token = sanitize_token(unit_num) or "unit"
            chapter_token = sanitize_token(str(item.get("chapter_num", "chapter"))) or "chapter"
            section_num = str(item.get("section_num", "")).strip()
            if section_num:
                section_token = sanitize_token(section_num) or "section"
                if unit_num:
                    filename = f"u{unit_token}_ch{chapter_token}_s{section_token}.md"
                else:
                    filename = f"ch{chapter_token}_s{section_token}.md"
            else:
                if unit_num:
                    filename = f"u{unit_token}_ch{chapter_token}.md"
                else:
                    filename = f"ch{chapter_token}.md"

        block = "\n".join(lines[start:end])
        blocks.append((filename, cleanup_markdown(block)))

    info = {
        "mode": "index",
        "matched": len(starts),
        "total": len(index_items),
        "missing": missing,
    }
    return metadata_text, blocks, info


def write_book_outputs(
    record: BookRecord,
    workspace_root: Path,
    overwrite: bool,
) -> Tuple[bool, str, Dict[str, object]]:
    """解析教材、生成 sections_index，并同步切分章节文件。"""
    book_dir = workspace_root / "segmentation" / record.book_prefix
    sections_dir = book_dir / "sections"

    if book_dir.exists() and overwrite:
        shutil.rmtree(book_dir)

    sections_dir.mkdir(parents=True, exist_ok=True)
    content = record.source_md.read_text(encoding="utf-8", errors="ignore")
    toc_entries = parse_toc_entries_from_content(content)
    info: Dict[str, object] = {}

    # Always build index as two levels (chapter -> section) when TOC is available.
    if toc_entries:
        index_items, idx_info = build_two_level_index_items_from_toc_entries(toc_entries)
        info.update(idx_info)
        # metadata: everything before TOC marker when present.
        lines = content.splitlines()
        toc_start = detect_toc_start(lines)
        if toc_start < 0:
            toc_start = detect_pseudo_toc_start(lines)
        metadata_text = "\n".join(lines[:toc_start]) if toc_start > 0 else ""
        info["mode"] = "toc"
        info["toc_entries"] = len(toc_entries)
        info["toc_found"] = True
    else:
        # Fallback: split by headings only, and keep chapter-only index.
        metadata_text, segments, split_info = split_markdown(content, stage=record.stage)
        info.update(split_info)
        if not segments:
            return False, "no_segments", info
        # Build chapter-only items from fallback segments.
        index_items = []
        for seg in segments:
            chapter_token = sanitize_token(seg.chapter_id) or sanitize_token(seg.chapter_title) or "chapter"
            index_items.append(
                {
                    "chapter_num": seg.chapter_id,
                    "chapter_title": _with_chapter_prefix(seg.chapter_id, seg.chapter_title),
                    "section_num": "",
                    "section_title": "",
                    "file": f"ch{chapter_token}.md",
                }
            )
        info["coerced_two_levels"] = True
        info["toc_found"] = False

    if not index_items:
        return False, "empty_index_items", info

    # Math-specific rule for two-level index:
    # - Drop non-lesson columns (阅读与思考/数学活动/小结/信息技术应用/探究与发现/…)
    # - Keep exercise-only sections like 复习题 / 复习参考题 as standalone sections.
    if record.subject == "数学":
        def _is_review_section(item: Dict[str, str]) -> bool:
            t = normalize_text((item.get("section_title") or item.get("chapter_title") or "")).strip()
            s = normalize_text((item.get("section_num") or "")).strip()
            # Keep both "复习题11" and "复习题" style.
            return bool(re.match(r"^复习(参考)?题", t)) or bool(re.match(r"^复习(参考)?题", s))

        def _is_nonlesson_column(item: Dict[str, str]) -> bool:
            s = normalize_text((item.get("section_num") or "")).strip()
            if not s:
                return False
            # Common non-lesson columns across math textbooks
            drop = {
                "阅读与思考",
                "数学活动",
                "小结",
                "信息技术应用",
                "探究与发现",
                "观察与猜想",
                "实验与探究",
                "探究与发现",
                "综合与实践",
                "课题学习",
                "项目",
            }
            return s in drop

        filtered: List[Dict[str, str]] = []
        for it in index_items:
            if _is_review_section(it):
                filtered.append(it)
                continue
            if _is_nonlesson_column(it):
                continue
            filtered.append(it)
        index_items = filtered
        info["math_filtered_sections"] = True
        info["index_items_after_filter"] = len(index_items)

    (book_dir / "metadata.md").write_text(cleanup_markdown(metadata_text), encoding="utf-8")

    index = {
        "book_prefix": record.book_prefix,
        "textbook": record.source_input.name if record.source_input else record.source_md.name,
        "mode": info.get("mode", "unknown"),
        "subject": record.subject,
        "stage": record.stage,
        "grade": record.grade,
        "publisher": record.publisher,
        "sections": index_items,
    }
    index_text = json.dumps(index, ensure_ascii=False, indent=2)
    # Keep backward-compatible filename while also providing the singular form.
    (book_dir / "sections_index.json").write_text(index_text, encoding="utf-8")
    (book_dir / "section_index.json").write_text(index_text, encoding="utf-8")

    _, blocks, block_info = build_file_blocks_from_index(content, index_items, stage=record.stage)
    if not blocks:
        return False, "no_blocks_from_index", block_info
    for filename, cleaned in blocks:
        (sections_dir / filename).write_text(cleaned, encoding="utf-8")

    return True, "ok", {"index_items": len(index_items), "written": len(blocks), **info, **block_info}


def load_book_records(config_path: Optional[str], filter_prefixes: Optional[Sequence[str]]) -> List[BookRecord]:
    config = load_config(config_path)
    wanted = {item.strip() for item in (filter_prefixes or []) if item.strip()}
    records: List[BookRecord] = []
    for book in config.load_books(require_source=False):
        if wanted and book["book_prefix"] not in wanted:
            continue
        source_input = config.resolve_book_source(book)
        source_md = config.resolve_book_markdown(book)
        book_name = source_input.stem if source_input else (source_md.stem if source_md else str(book["book_prefix"]))
        records.append(
            BookRecord(
                book_prefix=str(book["book_prefix"]),
                source_md=source_md,
                stage=str(book["stage"]),
                subject=str(book["subject"]),
                publisher=str(book["publisher"]),
                grade=str(book["grade"]),
                book_name=book_name,
                source_input=source_input,
            )
        )
    return records


def process_all(
    config_path: Optional[str],
    filter_prefixes: Optional[Sequence[str]],
    limit: Optional[int],
    overwrite: bool,
) -> Dict[str, object]:
    config = load_config(config_path)
    summary = {
        "processed": 0,
        "success": 0,
        "failed": 0,
        "failures": [],
    }

    count = 0
    for rec in load_book_records(config_path, filter_prefixes):
        summary["processed"] += 1
        if rec.source_md is None or not rec.source_md.exists():
            summary["failed"] += 1
            summary["failures"].append(
                {
                    "book_prefix": rec.book_prefix,
                    "source_input": str(rec.source_input) if rec.source_input else "",
                    "source_md": str(rec.source_md) if rec.source_md else "",
                    "status": "missing_book_markdown",
                }
            )
            print(f"[FAIL] {rec.book_prefix} (missing_book_markdown)")
            continue

        ok, status, info = write_book_outputs(rec, config.workspace_dir, overwrite=overwrite)

        if ok:
            summary["success"] += 1
            print(f"[OK] {rec.book_prefix} -> {info.get('written', 0)} sections")
        else:
            summary["failed"] += 1
            failure = {
                "book_prefix": rec.book_prefix,
                "source_input": str(rec.source_input) if rec.source_input else "",
                "source_md": str(rec.source_md),
                "subject": rec.subject,
                "stage": rec.stage,
                "publisher": rec.publisher,
                "grade": rec.grade,
                "book_name": rec.book_name,
                "status": status,
                "info": info,
            }
            summary["failures"].append(failure)
            print(f"[FAIL] {rec.book_prefix} ({status})")

        count += 1
        if limit and count >= limit:
            break

    return summary


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Step 1: 解析教材 Markdown，生成 sections_index.json，并切分章节 Markdown。"
    )
    parser.add_argument("--config", default=None, help="Pipeline config path")
    parser.add_argument("--filter-prefix", action="append", default=None, help="Only process specific book_prefix values")
    parser.add_argument("--limit", type=int, default=None, help="Process at most N books")
    parser.add_argument(
        "--no-overwrite",
        action="store_true",
        help="Do not overwrite existing segmentation workspace outputs",
    )
    parser.add_argument(
        "--failure-report",
        default=None,
        help="Optional path to write failure report jsonl",
    )
    args = parser.parse_args()

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
