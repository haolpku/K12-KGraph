"""Chinese math text normalization and search text construction."""

from __future__ import annotations

import re
import unicodedata
from typing import Any, Dict, Iterable, List

MATH_SYMBOLS = {
    "−": "-",
    "－": "-",
    "＋": "+",
    "×": "×",
    "÷": "÷",
    "＝": "=",
    "＞": ">",
    "＜": "<",
    "（": "(",
    "）": ")",
    "，": ",",
    "。": ".",
    "：": ":",
    "；": ";",
}


def normalize_math_text(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    for src, dst in MATH_SYMBOLS.items():
        text = text.replace(src, dst)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def clean_list(values: Any) -> List[str]:
    if not isinstance(values, list):
        return []
    out: List[str] = []
    seen = set()
    for item in values:
        text = normalize_math_text(item)
        if text and text not in seen:
            seen.add(text)
            out.append(text)
    return out


def join_fields(values: Iterable[Any]) -> str:
    parts: List[str] = []
    seen = set()
    for value in values:
        if isinstance(value, list):
            candidates = [normalize_math_text(x) for x in value]
        else:
            candidates = [normalize_math_text(value)]
        for item in candidates:
            if item and item not in seen:
                seen.add(item)
                parts.append(item)
    return " ".join(parts)


def build_search_text(node: Dict[str, Any], *, student_safe: bool = True) -> str:
    props = node.get("properties", {}) if isinstance(node.get("properties"), dict) else {}
    label = str(node.get("label", ""))
    if label == "Concept":
        return join_fields(
            [
                node.get("name"),
                props.get("aliases"),
                props.get("definition"),
                props.get("formula"),
                props.get("examples"),
                props.get("importance"),
            ]
        )
    if label == "Skill":
        return join_fields([node.get("name"), props.get("aliases"), props.get("description"), props.get("examples")])
    if label == "Exercise":
        fields = [node.get("name"), props.get("stem"), props.get("question"), props.get("type"), props.get("difficulty")]
        if not student_safe:
            fields.extend([props.get("answer"), props.get("analysis"), props.get("explanation")])
        return join_fields(fields)
    return join_fields([node.get("name"), props.values()])


def response_properties(
    label: str,
    properties: Dict[str, Any],
    *,
    student_safe: bool,
) -> Dict[str, Any]:
    blocked = {"embedding", "search_text", "teacher_search_text", "cjk_search_text"}
    if student_safe:
        blocked.update({"answer", "analysis", "solution", "explanation", "解析", "答案"})
    return {
        key: value
        for key, value in properties.items()
        if key not in blocked and not key.endswith("_search_text")
    }


def student_safe_properties(label: str, properties: Dict[str, Any]) -> Dict[str, Any]:
    return response_properties(label, properties, student_safe=True)
