"""Request, response, evidence, and authentication models for retrieval."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Annotated, Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

IntentName = Literal[
    "concept_detail",
    "prerequisites",
    "successors",
    "exercises_for",
    "similar_exercises",
    "location",
    "semantic_search",
    "teacher_analysis",
]
RouteName = Literal["cypher_template", "hybrid", "teacher_analysis", "none"]
Identifier = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{1,64}$")]


class AuthenticationContext(BaseModel):
    """Trusted identity produced by the authentication layer."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    principal_id: str = Field(min_length=1, max_length=200)
    role: Literal["student", "teacher", "admin"] = "student"
    permissions: frozenset[
        Literal["retrieval:read", "retrieval:answers", "retrieval:text2cypher"]
    ] = frozenset({"retrieval:read"})
    tenant_id: Optional[str] = None
    authenticated: bool = True


class RetrievalRequestV1(BaseModel):
    """Canonical public request. Security controls are intentionally absent."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    question: str = Field(min_length=1, max_length=500)
    grade: Optional[str] = Field(default=None, min_length=1, max_length=20)
    semester: Optional[Literal["上册", "下册"]] = None
    edition: Optional[str] = Field(default=None, min_length=1, max_length=30)
    book_id: Optional[Identifier] = None
    section_id: Optional[Identifier] = None
    exercise_type: Optional[str] = Field(default=None, min_length=1, max_length=30)
    difficulty: Optional[int] = Field(default=None, ge=1, le=5)
    top_k: int = Field(default=5, ge=1, le=20)

    def to_legacy(
        self,
        context: AuthenticationContext,
        *,
        route: Optional[str] = None,
    ) -> "RetrievalRequest":
        return RetrievalRequest(
            question=self.question,
            user_type=context.role,
            grade=self.grade,
            semester=self.semester,
            edition=self.edition,
            top_k=self.top_k,
            include_answers="retrieval:answers" in context.permissions,
            route=route,
            book_id=self.book_id,
            section_id=self.section_id,
            exercise_type=self.exercise_type,
            difficulty=self.difficulty,
        )


class EvidenceNodeV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    label: Literal["Concept", "Skill", "Exercise", "Book", "Chapter", "Section"]
    name: str
    score: float = Field(default=0.0, ge=0.0)
    properties: Dict[str, Any] = Field(default_factory=dict)


class RelationPathV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start_id: str
    end_id: str
    relationships: List[str] = Field(default_factory=list, max_length=3)
    nodes: List[EvidenceNodeV1] = Field(default_factory=list)


class RetrievalResponseV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: str
    intents: List[IntentName] = Field(min_length=1, max_length=2)
    route: RouteName
    entities: List[EvidenceNodeV1] = Field(default_factory=list)
    evidence_nodes: List[EvidenceNodeV1] = Field(default_factory=list)
    relation_paths: List[RelationPathV1] = Field(default_factory=list)
    textbook_locations: List[Dict[str, Any]] = Field(default_factory=list)
    analysis_results: List[Dict[str, Any]] = Field(default_factory=list)
    scores: List[Dict[str, Any]] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)
    reason_code: str = "OK"
    needs_clarification: bool = False


@dataclass
class RetrievalRequest:
    question: str
    user_type: str = "student"
    grade: Optional[str] = None
    semester: Optional[str] = None
    edition: Optional[str] = None
    subject: str = "数学"
    stage: str = "小学"
    top_k: int = 5
    include_answers: bool = False
    route: Optional[str] = None
    book_id: Optional[str] = None
    section_id: Optional[str] = None
    exercise_type: Optional[str] = None
    difficulty: Optional[int] = None

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "RetrievalRequest":
        return cls(
            question=str(payload.get("question", payload.get("query", ""))).strip(),
            user_type=str(payload.get("user_type", "student")).strip() or "student",
            grade=_optional_str(payload.get("grade")),
            semester=_optional_str(payload.get("semester")),
            edition=_optional_str(payload.get("edition")),
            subject=str(payload.get("subject", "数学")).strip() or "数学",
            stage=str(payload.get("stage", "小学")).strip() or "小学",
            top_k=int(payload.get("top_k", 5)),
            include_answers=bool(payload.get("include_answers", False)),
            route=_optional_str(payload.get("route")),
            book_id=_optional_str(payload.get("book_id")),
            section_id=_optional_str(payload.get("section_id")),
            exercise_type=_optional_str(payload.get("exercise_type", payload.get("type"))),
            difficulty=_optional_int(payload.get("difficulty")),
        )

    def is_teacher(self) -> bool:
        return self.user_type.strip().lower() in {"teacher", "教师", "admin"}


@dataclass
class RetrievalFilters:
    subject: Optional[str] = None
    stage: Optional[str] = None
    grade: Optional[str] = None
    semester: Optional[str] = None
    edition: Optional[str] = None
    exercise_type: Optional[str] = None
    difficulty: Optional[int] = None
    book_id: Optional[str] = None
    section_id: Optional[str] = None

    @classmethod
    def from_request(cls, request: RetrievalRequest) -> "RetrievalFilters":
        return cls(
            subject=request.subject,
            stage=request.stage,
            grade=request.grade,
            semester=request.semester,
            edition=request.edition,
            exercise_type=request.exercise_type,
            difficulty=request.difficulty,
            book_id=request.book_id,
            section_id=request.section_id,
        )

    def as_params(self) -> Dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v not in (None, "")}


@dataclass
class EvidenceNode:
    id: str
    label: str
    name: str
    score: float = 0.0
    properties: Dict[str, Any] = field(default_factory=dict)


@dataclass
class RelationPath:
    start_id: str
    end_id: str
    relationships: List[str] = field(default_factory=list)
    nodes: List[EvidenceNode] = field(default_factory=list)


@dataclass
class EvidencePath:
    """Compact path shape used by the Neo4j import/query CLI."""

    nodes: List[EvidenceNode] = field(default_factory=list)
    relationships: List[str] = field(default_factory=list)


@dataclass
class RetrievalResponse:
    intent: str
    query: str
    route: str = ""
    entities: List[EvidenceNode] = field(default_factory=list)
    evidence_nodes: List[EvidenceNode] = field(default_factory=list)
    relation_paths: List[RelationPath] = field(default_factory=list)
    textbook_locations: List[Dict[str, Any]] = field(default_factory=list)
    analysis_results: List[Dict[str, Any]] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    cypher: Optional[str] = None

    def to_dict(self, *, student_safe: bool = True) -> Dict[str, Any]:
        payload = asdict(self)
        if student_safe:
            payload.pop("cypher", None)
        return payload


def _optional_str(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _optional_int(value: Any) -> Optional[int]:
    if value in (None, ""):
        return None
    return int(value)
