"""Load and resolve the unified pipeline configuration (``config/default.yaml``).

Expands ``${ENV_VAR}`` placeholders after optionally loading ``config/.env`` into
the process environment, and exposes resolved filesystem paths as properties.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

_ENV_VAR_RE = re.compile(r"\$\{(\w+)\}")


def repo_root() -> Path:
    """Return the repository root (directory containing ``config/`` and ``src/``)."""
    return Path(__file__).resolve().parent.parent.parent


def load_repo_dotenv() -> Path:
    """Load ``<repo>/config/.env`` into ``os.environ`` (without overriding existing keys)."""
    from utils.envfile import load_env_file

    env_path = repo_root() / "config" / ".env"
    load_env_file(env_path)
    return env_path


def _expand_env(value: str) -> str:
    """Replace ``${VAR}`` placeholders with environment variable values."""
    def _repl(m: re.Match) -> str:
        return os.environ.get(m.group(1), "")
    return _ENV_VAR_RE.sub(_repl, value)


def _resolve_value(value: Any) -> Any:
    if isinstance(value, str):
        return _expand_env(value)
    if isinstance(value, dict):
        return {k: _resolve_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve_value(v) for v in value]
    return value


class PipelineConfig:
    """Parsed pipeline config with resolved, absolute paths."""

    def __init__(self, raw: Dict[str, Any], config_dir: Path) -> None:
        self._raw = _resolve_value(raw)
        self._config_dir = config_dir.resolve()

    def _resolve_path(self, rel: str) -> Path:
        """Resolve a path relative to *config_dir*."""
        p = Path(rel)
        if p.is_absolute():
            return p
        return (self._config_dir / p).resolve()

    # ---- path helpers -------------------------------------------------------

    @property
    def books_yaml(self) -> Path:
        return self._resolve_path(self._raw.get("paths", {}).get("books_yaml", "../books.yaml"))

    @property
    def input_dir(self) -> Path:
        return self._resolve_path(self._raw.get("paths", {}).get("input_dir", "../input"))

    @property
    def workspace_dir(self) -> Path:
        return self._resolve_path(self._raw.get("paths", {}).get("workspace_dir", "../workspace"))

    @property
    def output_dir(self) -> Path:
        return self._resolve_path(self._raw.get("paths", {}).get("output_dir", "../data"))

    @property
    def chapter_kg_dir(self) -> Path:
        return self.output_dir / "chapter_kg"

    @property
    def book_kg_dir(self) -> Path:
        return self.output_dir / "book_kg"

    @property
    def subject_stage_kg_dir(self) -> Path:
        return self.output_dir / "subject_stage_kg"

    @property
    def subject_kg_dir(self) -> Path:
        return self.output_dir / "subject_kg"

    @property
    def global_kg_dir(self) -> Path:
        return self.output_dir / "global_kg"

    @property
    def afterclass_exercises_dir(self) -> Path:
        return self.output_dir / "afterclass_exercises"

    @property
    def segmentation_workspace_dir(self) -> Path:
        return self.workspace_dir / "segmentation"

    @property
    def pdf_to_md_workspace_dir(self) -> Path:
        return self.workspace_dir / "pdf_to_md"

    @property
    def build_graph_workspace_dir(self) -> Path:
        return self.workspace_dir / "build_graph"

    @property
    def afterclass_exercises_workspace_dir(self) -> Path:
        return self.workspace_dir / "afterclass_exercises"

    @property
    def check_graph_workspace_dir(self) -> Path:
        return self.workspace_dir / "check_graph"

    # ---- LLM settings -------------------------------------------------------

    @property
    def llm(self) -> Dict[str, Any]:
        return self._raw.get("llm", {})

    @property
    def mineru(self) -> Dict[str, Any]:
        return self._raw.get("mineru", {})

    # ---- merge settings ------------------------------------------------------

    @property
    def merge_normalize(self) -> str:
        return self._raw.get("merge", {}).get("normalize", "nfkc_lower")

    @property
    def merge_dedup_labels(self) -> List[str]:
        return self._raw.get("merge", {}).get("dedup_labels", ["Concept", "Skill", "Experiment", "Exercise"])

    # ---- books.yaml loading --------------------------------------------------

    def load_books(self, *, require_source: bool = False) -> List[Dict[str, Any]]:
        """Return validated book records from ``books.yaml``."""
        with open(self.books_yaml, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        books = data.get("books", [])
        if not isinstance(books, list):
            raise ValueError("books.yaml must contain a top-level 'books' list")

        required = {"book_prefix", "subject", "stage", "grade", "publisher"}
        normalized: List[Dict[str, Any]] = []
        seen_prefixes: set[str] = set()

        for idx, raw_book in enumerate(books, start=1):
            if not isinstance(raw_book, dict):
                raise ValueError(f"books[{idx}] must be an object")
            missing = [key for key in required if not str(raw_book.get(key, "")).strip()]
            has_any_source = any(
                str(raw_book.get(key, "")).strip()
                for key in ("source_pdf", "source_md")
            )
            if require_source and not has_any_source:
                missing.append("source_pdf|source_md")
            if missing:
                raise ValueError(f"books[{idx}] missing required fields: {', '.join(missing)}")

            book = dict(raw_book)
            book["book_prefix"] = str(book["book_prefix"]).strip()
            for key in ("source_pdf", "source_md"):
                if key in book:
                    book[key] = str(book.get(key, "")).strip()
            if book["book_prefix"] in seen_prefixes:
                raise ValueError(f"duplicate book_prefix in books.yaml: {book['book_prefix']}")
            seen_prefixes.add(book["book_prefix"])
            normalized.append(book)

        return normalized

    def _resolve_book_path(self, book: Dict[str, Any], *keys: str) -> Optional[Path]:
        for key in keys:
            source_value = str(book.get(key, "")).strip()
            if not source_value:
                continue
            source = Path(source_value)
            if source.is_absolute():
                return source
            return (self.books_yaml.parent / source).resolve()
        return None

    def resolve_book_source(self, book: Dict[str, Any]) -> Optional[Path]:
        return self._resolve_book_path(book, "source_pdf", "source_md")

    def resolve_book_markdown(self, book: Dict[str, Any]) -> Optional[Path]:
        return self._resolve_book_path(book, "source_md")

    def resolve_book_pdf(self, book: Dict[str, Any]) -> Optional[Path]:
        return self._resolve_book_path(book, "source_pdf")

    def segmentation_book_dir(self, book_prefix: str) -> Path:
        return self.segmentation_workspace_dir / book_prefix

    def sections_dir_for(self, book_prefix: str) -> Path:
        return self.segmentation_book_dir(book_prefix) / "sections"

    def sections_index_for(self, book: Dict[str, Any]) -> Path:
        return self.segmentation_book_dir(str(book["book_prefix"])) / "sections_index.json"

    def build_graph_book_dir(self, book_prefix: str) -> Path:
        return self.build_graph_workspace_dir / book_prefix

    def afterclass_exercises_book_dir(self, book_prefix: str) -> Path:
        return self.afterclass_exercises_workspace_dir / book_prefix

    def afterclass_exercises_output_for(self, book_prefix: str) -> Path:
        return self.afterclass_exercises_dir / f"{book_prefix}.json"

    def pdf_to_md_book_dir(self, book_prefix: str) -> Path:
        return self.pdf_to_md_workspace_dir / book_prefix

    def generated_book_md_for(self, book_prefix: str) -> Path:
        return self.pdf_to_md_book_dir(book_prefix) / "book.md"

    def generated_book_manifest_for(self, book_prefix: str) -> Path:
        return self.pdf_to_md_book_dir(book_prefix) / "manifest.json"

    # ---- multimodal paths ---------------------------------------------------

    @property
    def mm_workspace_dir(self) -> Path:
        """Intermediate artifacts of the multimodal pipeline (alignment, partials)."""
        return self.workspace_dir / "mm"

    def mm_book_dir(self, book_prefix: str) -> Path:
        return self.mm_workspace_dir / book_prefix

    def aligned_figures_for(self, book_prefix: str) -> Path:
        """Section markdown aligned with the figures of each section."""
        return self.mm_book_dir(book_prefix) / "aligned_sections.json"

    def mm_partials_dir_for(self, book_prefix: str) -> Path:
        return self.mm_book_dir(book_prefix) / "partials"

    @property
    def mmkg_dir(self) -> Path:
        """One complete graph per book: text nodes plus figure nodes."""
        return self.output_dir / "mmkg"

    def mmkg_book_graph_for(self, book_prefix: str) -> Path:
        return self.mmkg_dir / f"{book_prefix}.json"

    @property
    def vqa_dir(self) -> Path:
        """Multimodal QA data derived from the per-book graphs."""
        return self.mmkg_dir / "vqa"

    @property
    def sft_vqa_root(self) -> Path:
        return self.vqa_dir / "sft_vqa"

    def sft_vqa_dir(self, book_prefix: str) -> Path:
        return self.sft_vqa_root / book_prefix

    @property
    def vqa_assets_dir(self) -> Path:
        return self.vqa_dir / "assets"

    @property
    def vqa_alpaca_dir(self) -> Path:
        return self.vqa_dir / "alpaca"

    def hybrid_dir_for(self, book_prefix: str) -> Optional[Path]:
        """Directory holding the MinerU markdown together with its ``images/`` folder."""
        mineru_root = self.pdf_to_md_book_dir(book_prefix) / "mineru_output"
        if mineru_root.is_dir():
            candidates = [
                path
                for path in mineru_root.rglob("hybrid_auto")
                if path.is_dir() and any(path.glob("*.md")) and (path / "images").is_dir()
            ]
            if candidates:
                candidates.sort(key=lambda path: (len(path.parts), str(path)))
                return candidates[0].resolve()

        book = next(
            (item for item in self.load_books(require_source=False) if item["book_prefix"] == book_prefix),
            None,
        )
        if book is not None:
            markdown = self.resolve_book_markdown(book)
            if markdown is not None and (markdown.parent / "images").is_dir():
                return markdown.parent.resolve()
        return None


def load_config(config_path: Optional[str] = None) -> PipelineConfig:
    """Load a :class:`PipelineConfig` from a YAML file.

    If *config_path* is ``None``, the default ``config/default.yaml`` relative
    to the repository root is used.
    """
    if config_path is None:
        config_path_obj = repo_root() / "config" / "default.yaml"
    else:
        config_path_obj = Path(config_path)

    load_repo_dotenv()

    with open(config_path_obj, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    return PipelineConfig(raw, config_path_obj.parent)
