"""Prompt template loading and rendering.

All prompt files in this repository use Python's ``str.format`` placeholders
(``{name}``); literal braces in a template must be doubled (``{{``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Union


def load_prompt(path: Union[str, Path]) -> str:
    """Read a prompt file (tolerates a UTF-8 BOM)."""
    return Path(path).read_text(encoding="utf-8-sig")


def load_prompt_dir(directory: Union[str, Path]) -> Dict[str, str]:
    """Load every ``*.txt`` in *directory* as ``{stem: text}``."""
    root = Path(directory)
    return {path.stem: load_prompt(path) for path in sorted(root.glob("*.txt"))}


def render_prompt(template: str, **values: Any) -> str:
    """Fill ``{placeholders}`` in *template*."""
    return template.format(**values)
