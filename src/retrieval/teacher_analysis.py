"""Strict teacher-analysis plans compiled to static parameterized Cypher."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from retrieval.security import validate_readonly_cypher

Operation = Literal["count", "list", "distribution"]
EntityType = Literal["Concept", "Skill", "Exercise"]
GroupBy = Literal["book", "chapter", "grade", "semester", "edition", "none"]

ENTITY_MATCH = {
    "Concept": "MATCH (n:Concept)",
    "Skill": "MATCH (n:Skill)",
    "Exercise": "MATCH (n:Exercise)",
}

FILTER_PROPERTIES = {
    "grade": "n.grade",
    "semester": "n.semester",
    "edition": "n.edition",
    "book_id": "n.book_id",
    "section_id": "n.section_id",
    "exercise_type": "n.type",
    "difficulty": "n.difficulty",
}

GROUP_EXPRESSIONS = {
    "grade": "n.grade",
    "semester": "n.semester",
    "edition": "n.edition",
    "book": "coalesce(book.name, book.id, n.book_id)",
    "chapter": "coalesce(chapter.name, chapter.id)",
}


class TeacherAnalysisFiltersV1(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    grade: str | None = None
    semester: str | None = None
    edition: str | None = None
    book_id: str | None = None
    section_id: str | None = None
    exercise_type: str | None = None
    difficulty: int | None = None


class TeacherAnalysisPlanV1(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    operation: Operation
    entity_type: EntityType
    group_by: GroupBy = "none"
    filters: TeacherAnalysisFiltersV1 = Field(default_factory=TeacherAnalysisFiltersV1)
    limit: int = Field(default=10, ge=1, le=20)

    @model_validator(mode="after")
    def validate_operation_shape(self) -> "TeacherAnalysisPlanV1":
        if self.operation == "distribution" and self.group_by == "none":
            raise ValueError("distribution requires a concrete group_by field")
        if self.operation != "distribution" and self.group_by != "none":
            raise ValueError("group_by is only supported for distribution")
        return self


@dataclass(frozen=True)
class CompiledTeacherAnalysis:
    cypher: str
    parameters: dict[str, Any]


class DeepSeekTeacherPlanGenerator:
    """OpenAI-compatible adapter that returns only a validated analysis plan."""

    def __init__(
        self,
        *,
        model_name: str = "deepseek-v4-flash",
        api_key: str | None = None,
        base_url: str = "https://api.deepseek.com",
        timeout: float = 4.0,
        client: Any | None = None,
    ) -> None:
        self.model_name = model_name
        self.api_key = api_key
        self.base_url = base_url
        self.timeout = timeout
        self._client = client

    def generate(
        self,
        question: str,
        filters: dict[str, Any] | None = None,
    ) -> TeacherAnalysisPlanV1:
        response = self._client_for_request().chat.completions.create(
            model=self.model_name,
            messages=[
                {"role": "system", "content": _PLAN_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": json.dumps(
                        {"question": question, "filters": filters or {}},
                        ensure_ascii=False,
                    ),
                },
            ],
            temperature=0,
            timeout=self.timeout,
            response_format={"type": "json_object"},
        )
        choices = getattr(response, "choices", None)
        content = (
            getattr(getattr(choices[0], "message", None), "content", None)
            if choices
            else None
        )
        if not isinstance(content, str) or not content.strip():
            raise ValueError("teacher analysis model returned empty content")
        return TeacherAnalysisPlanV1.model_validate_json(content)

    def _client_for_request(self) -> Any:
        if self._client is not None:
            return self._client
        if not self.api_key:
            raise ValueError("missing teacher analysis API key")
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise RuntimeError("Install openai to use teacher analysis") from exc
        self._client = OpenAI(
            api_key=self.api_key,
            base_url=self.base_url,
            timeout=self.timeout,
            max_retries=1,
        )
        return self._client


def compile_teacher_analysis_plan(plan: TeacherAnalysisPlanV1 | dict[str, Any]) -> CompiledTeacherAnalysis:
    if not isinstance(plan, TeacherAnalysisPlanV1):
        plan = TeacherAnalysisPlanV1.model_validate(plan)

    parameters: dict[str, Any] = {"limit": plan.limit}
    clauses = [ENTITY_MATCH[plan.entity_type]]
    if plan.group_by in {"book", "chapter"}:
        clauses.extend(
            [
                "OPTIONAL MATCH (n)-[:appears_in]->(section:Section)",
                "OPTIONAL MATCH (section)-[:is_part_of]->(chapter:Chapter)",
                "OPTIONAL MATCH (chapter)-[:is_part_of]->(book:Book)",
            ]
        )

    conditions = _filter_conditions(plan.filters, parameters)
    if conditions:
        clauses.append("WHERE " + " AND ".join(conditions))

    if plan.operation == "count":
        clauses.append("RETURN count(DISTINCT n) AS value")
        clauses.append("LIMIT $limit")
    elif plan.operation == "list":
        clauses.append(
            "RETURN n.id AS id, labels(n)[0] AS label, n.name AS name, "
            "n { .id, .name, .grade, .semester, .edition, .book_id, .section_id, "
            ".type, .difficulty, .answer, .analysis } AS properties"
        )
        clauses.append("ORDER BY id")
        clauses.append("LIMIT $limit")
    else:
        group_expr = GROUP_EXPRESSIONS[plan.group_by]
        clauses.append(f"WITH {group_expr} AS key, count(DISTINCT n) AS value")
        clauses.append("RETURN key, value")
        clauses.append("ORDER BY key")
        clauses.append("LIMIT $limit")

    cypher = "\n".join(clauses)
    validate_readonly_cypher(cypher, parameters=parameters)
    return CompiledTeacherAnalysis(cypher=cypher, parameters=parameters)


def compile_teacher_analysis_cypher(plan: TeacherAnalysisPlanV1 | dict[str, Any]) -> tuple[str, dict[str, Any]]:
    compiled = compile_teacher_analysis_plan(plan)
    return compiled.cypher, compiled.parameters


def _filter_conditions(filters: TeacherAnalysisFiltersV1, parameters: dict[str, Any]) -> list[str]:
    conditions: list[str] = []
    for key, prop in FILTER_PROPERTIES.items():
        value = getattr(filters, key)
        if value is None:
            continue
        parameters[key] = value
        conditions.append(f"{prop} = ${key}")
    return conditions


_PLAN_SYSTEM_PROMPT = """
你是小学数学知识图谱的教师分析计划生成器。只输出json，严格使用：
{"operation":"count|list|distribution","entity_type":"Concept|Skill|Exercise",
"group_by":"book|chapter|grade|semester|edition|none","filters":{},"limit":10}
不得输出Cypher、CALL、标签名、属性名、解释、Markdown或额外字段。
count/list的group_by必须是none；distribution必须指定非none分组。
filters只允许grade, semester, edition, book_id, section_id, exercise_type, difficulty。
""".strip()
