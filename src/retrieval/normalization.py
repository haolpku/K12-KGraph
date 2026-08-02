"""Public normalization helpers built on the canonical retrieval text module."""

from __future__ import annotations

import re
import unicodedata
from typing import Any, Mapping, Optional

from retrieval.text import build_search_text as _build_node_search_text


def normalize_math_symbols(value: Any) -> str:
    """Normalize full-width characters and add readable math-operator spacing."""

    text = unicodedata.normalize("NFKC", str(value or ""))
    text = text.replace("−", "-").replace("－", "-")
    for symbol in ("+", "-", "×", "÷", "=", ">", "<"):
        text = re.sub(rf"\s*{re.escape(symbol)}\s*", f" {symbol} ", text)
    return " ".join(text.split())


def build_search_text(
    label: str,
    properties: Mapping[str, Any],
    *,
    audience: Optional[str] = None,
    student: bool = True,
) -> str:
    """Build node-type-specific retrieval text.

    Student exercise text excludes answers and explanations; teacher text may
    include them.
    """

    if audience is not None:
        student = str(audience).lower() not in {"teacher", "教师", "admin"}
    props = dict(properties)
    node = {
        "label": label,
        "name": props.get("name", ""),
        "properties": props,
    }
    return _build_node_search_text(node, student_safe=student)
