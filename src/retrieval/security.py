"""Safety checks for teacher-side Text2Cypher and student-safe responses."""

from __future__ import annotations

import re
import unicodedata
from typing import Any, Dict, Iterable, List, Mapping

from retrieval.text import response_properties

MAX_SAFE_LIMIT = 20
READ_START_KEYWORDS = {"MATCH", "OPTIONAL", "WITH", "UNWIND"}
FORBIDDEN_CYPHER_RE = re.compile(
    r"\b("
    r"CREATE|MERGE|DELETE|DETACH|SET|REMOVE|DROP|LOAD\s+CSV|LOAD|"
    r"ALTER|GRANT|DENY|REVOKE|START|STOP|USE|SHOW|TERMINATE|FOREACH|"
    r"CREATE\s+INDEX|DROP\s+INDEX|CREATE\s+CONSTRAINT|DROP\s+CONSTRAINT|"
    r"CREATE\s+USER|DROP\s+USER|CREATE\s+ROLE|DROP\s+ROLE|SET\s+PASSWORD|"
    r"IMPORT|EXPORT"
    r")\b",
    re.IGNORECASE,
)
PATH_RANGE_RE = re.compile(r"\*\s*(\d*)\s*\.\.\s*(\d*)")
VARIABLE_LENGTH_PATH_RE = re.compile(r"\*\s*(?:(\d+)\s*)?(?:\.\.\s*(\d*)\s*)?(?=[]):]?|\])")
COMMENT_RE = re.compile(r"//|/\*|\*/")
SUBQUERY_RE = re.compile(r"\b(?:EXISTS|COUNT|COLLECT)\s*\{", re.IGNORECASE)
LIMIT_RE = re.compile(r"\bLIMIT\s+(\$?[A-Za-z_][A-Za-z0-9_]*|-?\d+)\b", re.IGNORECASE)
UNSAFE_QUESTION_RE = re.compile(
    r"\b(?:CREATE|MERGE|DELETE|DETACH|SET|REMOVE|DROP|LOAD\s+CSV|CALL|"
    r"APOC(?:\.[A-Za-z_][A-Za-z0-9_]*)?|"
    r"GDS(?:\.[A-Za-z_][A-Za-z0-9_]*)?|MATCH|RETURN)\b",
    re.IGNORECASE,
)


class CypherSecurityError(ValueError):
    pass


UnsafeCypherError = CypherSecurityError


class UnsafeQuestionError(ValueError):
    pass


def normalize_cypher(cypher: str) -> str:
    return unicodedata.normalize("NFKC", str(cypher or "")).strip()


def validate_question_input(question: str) -> str:
    """Reject Cypher-shaped prompt injection before routing or retrieval."""

    text = unicodedata.normalize("NFKC", str(question or "")).strip()
    if COMMENT_RE.search(text) or UNSAFE_QUESTION_RE.search(text):
        raise UnsafeQuestionError("question contains an unsafe query instruction")
    return text


def validate_readonly_cypher(
    cypher: str,
    *,
    max_depth: int = 3,
    max_limit: int = MAX_SAFE_LIMIT,
    parameters: Mapping[str, Any] | None = None,
) -> str:
    text = normalize_cypher(cypher)
    if not text:
        raise CypherSecurityError("empty Cypher")
    if ";" in text:
        raise CypherSecurityError("Cypher statements must not contain semicolons")
    if COMMENT_RE.search(text):
        raise CypherSecurityError("Cypher comments are not allowed")
    if re.search(r"\bCALL\b", text, re.IGNORECASE):
        raise CypherSecurityError("Cypher CALL is outside the read-only procedure allowlist")
    if SUBQUERY_RE.search(text):
        raise CypherSecurityError("Cypher subqueries are not allowed")
    if FORBIDDEN_CYPHER_RE.search(text):
        raise CypherSecurityError("Cypher contains forbidden write/admin/external-data operation")
    first = re.match(r"^\s*(\w+)", text)
    if not first or first.group(1).upper() not in READ_START_KEYWORDS:
        raise CypherSecurityError("Cypher must start with a read-only clause")
    for match in PATH_RANGE_RE.finditer(text):
        upper = match.group(2)
        if not upper:
            raise CypherSecurityError("unbounded variable-length paths are not allowed")
        if int(upper) > max_depth:
            raise CypherSecurityError(f"variable-length path depth exceeds {max_depth}")
    for match in VARIABLE_LENGTH_PATH_RE.finditer(text):
        if ".." not in match.group(0):
            raise CypherSecurityError("unbounded variable-length paths are not allowed")
        upper = match.group(2)
        if not upper:
            raise CypherSecurityError("unbounded variable-length paths are not allowed")
        if int(upper) > max_depth:
            raise CypherSecurityError(f"variable-length path depth exceeds {max_depth}")
    limits = list(LIMIT_RE.finditer(text))
    if not limits:
        raise CypherSecurityError("read-only Cypher must include an explicit LIMIT")
    for match in limits:
        _validate_limit_token(match.group(1), max_limit=max_limit, parameters=parameters)
    return text


def enforce_limit(cypher: str, *, limit: int) -> str:
    maximum = int(limit)
    if maximum < 1:
        raise CypherSecurityError("row limit must be positive")
    if maximum > MAX_SAFE_LIMIT:
        raise CypherSecurityError(f"Cypher row limit exceeds {MAX_SAFE_LIMIT}")
    text = normalize_cypher(cypher)
    if re.search(r"\bLIMIT\s+-\d+\b", text, re.IGNORECASE):
        raise CypherSecurityError("Cypher row limit must be positive")
    try:
        text = validate_readonly_cypher(text, max_limit=maximum)
    except CypherSecurityError as exc:
        if "explicit LIMIT" not in str(exc):
            raise
        text = text.strip()
    declared_limits = [
        int(value)
        for value in re.findall(r"\bLIMIT\s+(\d+)\b", text, re.IGNORECASE)
    ]
    if any(value < 1 for value in declared_limits):
        raise CypherSecurityError("Cypher row limit must be positive")
    if any(value > maximum for value in declared_limits):
        raise CypherSecurityError(f"Cypher row limit exceeds {maximum}")
    if declared_limits:
        return text
    return f"{text}\nLIMIT {maximum}"


def _validate_limit_token(token: str, *, max_limit: int, parameters: Mapping[str, Any] | None) -> None:
    if token.startswith("$"):
        if parameters is None:
            raise CypherSecurityError("parameterized LIMIT requires bound parameters")
        name = token[1:]
        value = parameters.get(name)
    else:
        value = int(token)
    if isinstance(value, bool) or not isinstance(value, int):
        raise CypherSecurityError("Cypher row limit must be an integer")
    if value < 1:
        raise CypherSecurityError("Cypher row limit must be positive")
    if value > max_limit:
        raise CypherSecurityError(f"Cypher row limit exceeds {max_limit}")


def sanitize_node_record(record: Dict[str, Any], *, student_safe: bool) -> Dict[str, Any]:
    label = str(record.get("label", ""))
    props = record.get("properties", {}) if isinstance(record.get("properties"), dict) else {}
    out = dict(record)
    out["properties"] = response_properties(label, props, student_safe=student_safe)
    if student_safe:
        out.pop("cypher", None)
    return out


def sanitize_records(records: Iterable[Dict[str, Any]], *, student_safe: bool) -> List[Dict[str, Any]]:
    return [sanitize_node_record(item, student_safe=student_safe) for item in records]
