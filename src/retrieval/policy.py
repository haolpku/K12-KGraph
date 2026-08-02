"""Authorization and response field policy for retrieval."""

from __future__ import annotations

from typing import Any, Dict

from retrieval.models import AuthenticationContext


class AuthorizationError(PermissionError):
    pass


PUBLIC_FIELDS = {
    "Concept": {
        "id",
        "name",
        "aliases",
        "definition",
        "formula",
        "examples",
        "importance",
        "subject",
        "stage",
        "grade",
        "semester",
        "edition",
        "book_id",
        "section_id",
        "section_ids",
        "chapter_id",
    },
    "Skill": {
        "id",
        "name",
        "aliases",
        "description",
        "examples",
        "subject",
        "stage",
        "grade",
        "semester",
        "edition",
        "book_id",
        "section_id",
        "section_ids",
        "chapter_id",
    },
    "Exercise": {
        "id",
        "name",
        "stem",
        "question",
        "type",
        "difficulty",
        "subject",
        "stage",
        "grade",
        "semester",
        "edition",
        "book_id",
        "section_id",
        "section_ids",
        "chapter_id",
    },
}
TEACHER_EXTRA_FIELDS = {
    "Concept": {"teacher_note", "teaching_notes"},
    "Skill": {"teaching_advice", "teacher_note"},
    "Exercise": {"answer", "analysis", "solution", "explanation", "rubric"},
}


def require_permission(context: AuthenticationContext, permission: str) -> None:
    if not context.authenticated:
        raise AuthorizationError("authentication is required")
    if permission not in context.permissions:
        raise AuthorizationError(f"missing permission: {permission}")


def can_use_teacher_analysis(context: AuthenticationContext) -> bool:
    return (
        context.role in {"teacher", "admin"}
        and "retrieval:text2cypher" in context.permissions
    )


def filter_properties(
    label: str,
    properties: Dict[str, Any],
    context: AuthenticationContext,
) -> Dict[str, Any]:
    allowed = set(PUBLIC_FIELDS.get(label, set()))
    if "retrieval:answers" in context.permissions and context.role in {
        "teacher",
        "admin",
    }:
        allowed.update(TEACHER_EXTRA_FIELDS.get(label, set()))
    return {key: value for key, value in properties.items() if key in allowed}


def filter_response_value(value: Any, context: AuthenticationContext) -> Any:
    """Apply role × node-type allowlists recursively; unknown fields fail closed."""

    if isinstance(value, list):
        return [filter_response_value(item, context) for item in value]
    if not isinstance(value, dict):
        return value
    label = str(value.get("label", ""))
    out: Dict[str, Any] = {}
    for key, item in value.items():
        if key in {"embedding", "search_text", "teacher_search_text", "cypher"}:
            continue
        if key == "properties" and isinstance(item, dict):
            out[key] = filter_properties(label, item, context)
        else:
            out[key] = filter_response_value(item, context)
    return out
