#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Prepare the image assets the VQA training data refers to.

For every book graph under ``data/mmkg``:

- copy the figure images referenced by ``Figure`` nodes
- draw the bounding box of each ``VisualElement`` onto its parent figure
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path
from typing import Any

# ``python src/qa/vqa_assets.py`` puts src/qa (not src) on sys.path.
_SRC_DIR = Path(__file__).resolve().parents[1]
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from utils.config import load_config  # noqa: E402
from utils.io import read_json, write_json  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Prepare figure and boxed visual-element images for VQA training")
    parser.add_argument("--config", default=None, help="Pipeline config path; defaults to config/default.yaml")
    parser.add_argument("--input-root", default=None, help="Book graphs; defaults to <data>/mmkg")
    parser.add_argument("--output-root", default=None, help="Defaults to <data>/mmkg/vqa/assets")
    parser.add_argument("--book", default=None, help="Optional single book to process")
    parser.add_argument("--line-width", type=int, default=6, help="Bounding box line width")
    parser.add_argument("--color", default="#ff3b30", help="Bounding box color")
    return parser.parse_args()


def safe_props(node: dict[str, Any]) -> dict[str, Any]:
    return node.get("properties", {}) or {}


def edge_source_id(edge: dict[str, Any]) -> str:
    props = edge.get("properties", {}) or {}
    return str(props.get("source_id", edge.get("source", "")) or edge.get("source", ""))


def edge_target_id(edge: dict[str, Any]) -> str:
    props = edge.get("properties", {}) or {}
    return str(props.get("target_id", edge.get("target", "")) or edge.get("target", ""))


def choose_figure_path(props: dict[str, Any]) -> Path | None:
    img_path = str(props.get("img_path", "") or "")
    if img_path:
        path = Path(img_path)
        if path.exists():
            return path
    for candidate in props.get("image_paths", []) or []:
        path = Path(str(candidate))
        if path.exists():
            return path
    return None


def clamp_bbox(bbox: list[float], width: int, height: int) -> tuple[int, int, int, int] | None:
    if len(bbox) != 4:
        return None
    # BBox annotations are normalized to 0-1000 and stored as
    # [ymin, xmin, ymax, xmax].
    ymin, xmin, ymax, xmax = [float(v) for v in bbox]
    x1 = int(round(xmin / 1000.0 * width))
    y1 = int(round(ymin / 1000.0 * height))
    x2 = int(round(xmax / 1000.0 * width))
    y2 = int(round(ymax / 1000.0 * height))
    x1 = max(0, min(x1, width - 1))
    y1 = max(0, min(y1, height - 1))
    x2 = max(0, min(x2, width - 1))
    y2 = max(0, min(y2, height - 1))
    if x2 <= x1 or y2 <= y1:
        return None
    return x1, y1, x2, y2


def copy_figure_image(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        return
    shutil.copy2(src, dst)


def render_boxed_image(src: Path, dst: Path, bbox: list[float], color: str, line_width: int) -> bool:
    # Pillow is only needed for the boxed visual-element images.
    try:
        from PIL import Image, ImageDraw
    except ImportError as exc:
        raise ImportError("Install Pillow to render boxed visual-element images: pip install Pillow") from exc

    dst.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(src) as im:
        image = im.convert("RGB")
        box = clamp_bbox(bbox, image.width, image.height)
        if box is None:
            return False
        draw = ImageDraw.Draw(image)
        for offset in range(max(1, line_width)):
            draw.rectangle(
                (box[0] - offset, box[1] - offset, box[2] + offset, box[3] + offset),
                outline=color,
                width=1,
            )
        image.save(dst)
    return True


def main() -> None:
    args = parse_args()
    config = load_config(args.config)
    input_root = Path(args.input_root).resolve() if args.input_root else config.mmkg_dir
    output_root = Path(args.output_root).resolve() if args.output_root else config.vqa_assets_dir
    figures_root = output_root / "figures"
    boxed_root = output_root / "ve_boxed"
    output_root.mkdir(parents=True, exist_ok=True)

    summary: dict[str, Any] = {
        "input_root": str(input_root),
        "output_root": str(output_root),
        "books": [],
        "totals": {
            "book_count": 0,
            "figure_images_copied": 0,
            "boxed_images_created": 0,
            "boxed_images_failed": 0,
        },
    }

    for graph_path in sorted(input_root.glob("*.json"), key=lambda p: p.name):
        book = graph_path.stem
        if args.book and book != args.book:
            continue
        graph = read_json(graph_path)
        nodes = [node for node in graph.get("nodes", []) if isinstance(node, dict)]
        edges = [edge for edge in graph.get("edges", []) if isinstance(edge, dict)]
        figure_nodes = [node for node in nodes if node.get("label") == "Figure"]
        ve_nodes = [node for node in nodes if node.get("label") == "VisualElement"]
        refers_to_edges = [edge for edge in edges if edge.get("type") == "refers_to"]

        figure_map = {str(node.get("id", "")): node for node in figure_nodes if node.get("id")}
        ve_map = {str(node.get("id", "")): node for node in ve_nodes if node.get("id")}

        figure_manifest: dict[str, str] = {}
        boxed_manifest: dict[str, str] = {}
        copied = 0
        boxed = 0
        failed = 0

        for figure_id, node in figure_map.items():
            props = safe_props(node)
            src = choose_figure_path(props)
            if src is None:
                continue
            suffix = src.suffix.lower() or ".jpg"
            rel = Path(book) / f"{figure_id}{suffix}"
            dst = figures_root / rel
            copy_figure_image(src, dst)
            figure_manifest[figure_id] = str(rel.as_posix())
            copied += 1

        for edge in refers_to_edges:
            ve_id = edge_source_id(edge)
            ve_node = ve_map.get(ve_id)
            if not ve_node:
                failed += 1
                continue
            ve_props = safe_props(ve_node)
            bbox = ve_props.get("bbox_2d")
            fig_id = str(ve_props.get("source_figure", "") or "")
            figure_node = figure_map.get(fig_id)
            if not bbox or not figure_node:
                failed += 1
                continue
            fig_props = safe_props(figure_node)
            src = choose_figure_path(fig_props)
            if src is None:
                failed += 1
                continue
            suffix = src.suffix.lower() or ".jpg"
            rel = Path(book) / f"{ve_id}{suffix}"
            dst = boxed_root / rel
            ok = render_boxed_image(src, dst, bbox, args.color, args.line_width)
            if not ok:
                failed += 1
                continue
            boxed_manifest[ve_id] = str(rel.as_posix())
            boxed += 1

        book_summary = {
            "book": book,
            "figure_node_count": len(figure_nodes),
            "visual_element_node_count": len(ve_nodes),
            "refers_to_edge_count": len(refers_to_edges),
            "figure_images_copied": copied,
            "boxed_images_created": boxed,
            "boxed_images_failed": failed,
            "figure_manifest": figure_manifest,
            "boxed_manifest": boxed_manifest,
        }
        summary["books"].append(book_summary)
        summary["totals"]["book_count"] += 1
        summary["totals"]["figure_images_copied"] += copied
        summary["totals"]["boxed_images_created"] += boxed
        summary["totals"]["boxed_images_failed"] += failed

        write_json(output_root / book / "asset_manifest.json", book_summary)
        print(
            f"{book}: figures={copied} boxed={boxed} failed={failed}",
            flush=True,
        )

    write_json(output_root / "summary.json", summary)
    totals = summary["totals"]
    print(
        f"books: {totals['book_count']} | figures copied: {totals['figure_images_copied']} | "
        f"boxed: {totals['boxed_images_created']} | failed: {totals['boxed_images_failed']}",
        flush=True,
    )


if __name__ == "__main__":
    main()

