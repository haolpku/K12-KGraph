"""Structured LLM intent classification adapters for retrieval routing."""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

ALLOWED_INTENTS = {
    "concept_detail",
    "prerequisites",
    "successors",
    "exercises_for",
    "similar_exercises",
    "location",
    "semantic_search",
    "teacher_analysis",
}
ALLOWED_LABELS = {"Concept", "Skill", "Exercise"}


class IntentEntityV1(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    text: str = Field(min_length=1, max_length=200)
    type: str = "Concept"

    @field_validator("type")
    @classmethod
    def _validate_type(cls, value: str) -> str:
        if value not in ALLOWED_LABELS:
            raise ValueError("unsupported entity type")
        return value


class IntentDecisionV1(BaseModel):
    """Versioned, strict JSON contract returned by intent classifiers."""

    model_config = ConfigDict(extra="forbid", strict=True)

    version: str = Field(default="v1")
    intents: tuple[str, ...] = Field(min_length=1, max_length=2)
    entities: tuple[IntentEntityV1, ...] = Field(default_factory=tuple)
    filters: dict[str, str | int | None] = Field(default_factory=dict)
    needs_clarification: bool = False

    @field_validator("version")
    @classmethod
    def _validate_version(cls, value: str) -> str:
        if value != "v1":
            raise ValueError("unsupported intent decision version")
        return value

    @field_validator("intents")
    @classmethod
    def _validate_intents(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if set(value) - ALLOWED_INTENTS:
            raise ValueError("unsupported intent")
        return value

    @field_validator("filters")
    @classmethod
    def _validate_filters(
        cls, value: dict[str, str | int | None]
    ) -> dict[str, str | int | None]:
        allowed = {
            "grade",
            "semester",
            "edition",
            "book_id",
            "section_id",
            "exercise_type",
            "difficulty",
        }
        if set(value) - allowed:
            raise ValueError("unsupported filters")
        return value


class DeepSeekIntentClassifier:
    """DeepSeek chat-completions adapter with strict structured JSON parsing."""

    def __init__(
        self,
        *,
        model_name: str = "deepseek-v4-flash",
        api_key: str | None = None,
        base_url: str = "https://api.deepseek.com",
        timeout: float = 30.0,
        client: Any | None = None,
    ) -> None:
        self.model_name = model_name
        self.timeout = timeout
        self._client = client
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")

    def classify(
        self,
        question: str,
        filters: dict[str, Any] | None = None,
    ) -> IntentDecisionV1:
        response = _retry_once_if_retryable(lambda: self._client_for_request().chat.completions.create(
            model=self.model_name,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": _request_payload(str(question), filters or {}),
                },
            ],
            temperature=0,
            timeout=self.timeout,
            response_format={"type": "json_object"},
        ))
        content = _message_content(response)
        try:
            return IntentDecisionV1.model_validate_json(content)
        except ValidationError as exc:
            raise ValueError("intent classifier returned invalid IntentDecisionV1 JSON") from exc

    def __call__(
        self,
        question: str,
        filters: dict[str, Any] | None = None,
    ) -> IntentDecisionV1:
        return self.classify(question, filters)

    def _client_for_request(self) -> Any:
        if self._client is not None:
            return self._client
        if not self._api_key or not self._api_key.strip():
            raise ValueError("missing DeepSeek api_key")
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise RuntimeError("Install openai to use DeepSeek intent classification") from exc
        self._client = OpenAI(api_key=self._api_key, base_url=self._base_url, timeout=self.timeout)
        return self._client


_SYSTEM_PROMPT = """
你是小学数学知识图谱检索的意图分类器。只输出一个 JSON 对象，必须符合 IntentDecisionV1：
{"version":"v1","intents":["..."],"entities":[{"text":"...","type":"Concept"}],"filters":{},"needs_clarification":false}
禁止输出解释、Markdown 或额外字段。

intent 只能是：concept_detail, prerequisites, successors, exercises_for, similar_exercises, location, semantic_search, teacher_analysis。
实体type只能是：Concept, Skill, Exercise。最多返回两个intents。
不得输出检索工具、权限或置信度。歧义时设置needs_clarification=true。
""".strip()


def _request_payload(question: str, filters: dict[str, Any]) -> str:
    return json.dumps(
        {
            "question": question,
            "filters": filters,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _message_content(response: Any) -> str:
    choices = _response_value(response, "choices")
    if not choices:
        raise ValueError("intent classifier returned no choices")
    message = _response_value(choices[0], "message")
    content = _response_value(message, "content")
    if not isinstance(content, str) or not content.strip():
        raise ValueError("intent classifier returned empty content")
    return content


def _response_value(item: Any, key: str) -> Any:
    if isinstance(item, dict):
        return item.get(key)
    return getattr(item, key, None)


def _retry_once_if_retryable(operation: Any) -> Any:
    try:
        return operation()
    except Exception as exc:  # noqa: BLE001 - external SDKs expose several retryable exception classes.
        if not _is_retryable_llm_error(exc):
            raise RuntimeError("intent classifier request failed") from None
    try:
        return operation()
    except Exception:  # noqa: BLE001 - suppress provider details that may include credentials.
        raise RuntimeError("intent classifier request failed after retry") from None


def _is_retryable_llm_error(exc: Exception) -> bool:
    status_code = getattr(exc, "status_code", None)
    response = getattr(exc, "response", None)
    if status_code is None and response is not None:
        status_code = getattr(response, "status_code", None)
    if status_code == 429:
        return True
    name = exc.__class__.__name__.lower()
    return any(token in name for token in ("timeout", "connection", "network", "connect", "readtimeout"))
