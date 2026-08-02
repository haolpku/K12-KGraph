"""Question intent routing for K12 retrieval."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Any, Callable, Optional

from retrieval.models import RetrievalRequest, RetrievalRequestV1


@dataclass(frozen=True)
class RouteDecision:
    intent: str
    route: str
    entity_text: str
    labels: tuple[str, ...]

    @property
    def method(self) -> str:
        return self.route


@dataclass(frozen=True)
class CascadeDecision:
    intents: tuple[str, ...]
    routes: tuple[str, ...]
    entity_text: str
    labels: tuple[str, ...]
    confidence: float
    needs_clarification: bool = False
    reason_code: str = "OK"


StructuredClassifier = Callable[[str, dict[str, Any]], Any]


ENTITY_PATTERNS = [
    re.compile(r"【([^】]+)】"),
    re.compile(r"“([^”]+)”"),
    re.compile(r"\"([^\"]+)\""),
    re.compile(r"(?:学习|学)?(.+?)(?:前要会|先要会|之前要会|的前置知识|的先修知识)"),
    re.compile(r"(?:哪些题|什么题|练习题).*?(?:考察|测试|用到)(?:了)?(.+?)(?:这个|这一|知识点|技能|[?？。]|$)"),
    re.compile(r"(?:什么是|解释一下|怎么理解)(.+?)(?:[?？。]|$)"),
    re.compile(r"中的(.+?)(?:这个|这一|的|有哪些|在哪|是什么|$)"),
]

INTENT_TOKENS = {
    "prerequisites": ("前置", "先修", "之前", "应先", "需要学习", "前要会", "先要会"),
    "successors": ("后续", "之后", "下一步", "继续学习"),
    "exercises_for": ("题目", "练习", "例题", "考察", "测试", "题"),
    "location": ("哪里", "在哪", "教材", "章节", "出处", "位置"),
    "semantic_search": ("类似", "相似", "差不多", "什么意思", "怎么理解", "解释"),
    "teacher_analysis": ("统计", "多少个", "分布", "列出所有", "按", "分析"),
}
CONJUNCTION_TOKENS = ("并且", "同时", "以及", "再给", "并给", "还有", "，并", "，再")
QUOTED_ENTITY_PATTERN = re.compile(r"【[^】]*】|“[^”]*”|\"[^\"]*\"")


def route_request(request: RetrievalRequest) -> RouteDecision:
    forced = (request.route or "").strip().lower()
    question = request.question.strip()
    routing_text = _routing_text(question)
    entity = extract_entity_text(question)
    user_type = request.user_type.lower()
    if forced:
        natural = route_request(replace(request, route=None))
        return RouteDecision(natural.intent, forced, natural.entity_text, natural.labels)
    if user_type == "teacher" and any(token in routing_text for token in ("统计", "多少个", "分布", "列出所有", "按", "分析")):
        return RouteDecision("teacher_analysis", "text2cypher", entity, ("Concept", "Skill", "Exercise"))
    if any(token in routing_text for token in ("前置", "先修", "之前", "应先", "需要学习", "前要会", "先要会")):
        return RouteDecision("prerequisites", "cypher", entity, ("Concept", "Skill"))
    if any(token in routing_text for token in ("后续", "之后", "下一步", "继续学习")):
        return RouteDecision("successors", "cypher", entity, ("Concept", "Skill"))
    if any(token in routing_text for token in ("题目", "练习", "例题", "考察", "测试", "题")):
        if any(token in routing_text for token in ("相似", "类似", "差不多", "近义")):
            return RouteDecision("similar_exercises", "hybrid", entity or question, ("Exercise",))
        return RouteDecision("exercises_for", "cypher", entity, ("Concept", "Skill"))
    if any(token in routing_text for token in ("哪里", "在哪", "教材", "章节", "出处", "位置")):
        return RouteDecision("location", "cypher", entity, ("Concept", "Skill", "Exercise"))
    if any(token in routing_text for token in ("类似", "相似", "差不多", "什么意思", "怎么理解", "解释")):
        return RouteDecision("semantic_search", "hybrid", entity or question, ("Concept", "Skill"))
    return RouteDecision("concept_detail", "hybrid", entity or question, ("Concept", "Skill"))


def route_cascade(
    request: RetrievalRequestV1,
    *,
    role: str = "student",
    classifier: Optional[StructuredClassifier] = None,
) -> CascadeDecision:
    """Deterministic first-stage cascade with structured fallback."""

    question = request.question.strip()
    routing_text = _routing_text(question)
    entity = extract_entity_text(question) or question
    matched = _matched_intents(routing_text, role=role)
    has_conjunction = any(token in routing_text for token in CONJUNCTION_TOKENS)

    if len(matched) == 1 and not has_conjunction:
        intent = matched[0]
        if intent in {"prerequisites", "successors", "exercises_for", "location"}:
            return CascadeDecision(
                (intent,),
                ("cypher_template",),
                entity,
                _labels_for_intent(intent),
                0.95,
                reason_code="CASCADE_RULE_DETERMINISTIC",
            )
        if intent == "similar_exercises":
            return CascadeDecision(
                (intent,),
                ("hybrid",),
                entity,
                ("Exercise",),
                0.90,
                reason_code="CASCADE_RULE_SEMANTIC",
            )
        if intent == "teacher_analysis":
            return CascadeDecision(
                (intent,),
                ("teacher_analysis",),
                entity,
                ("Concept", "Skill", "Exercise"),
                0.90,
                reason_code="CASCADE_RULE_TEACHER_ANALYSIS",
            )
        return CascadeDecision(
            (intent,),
            ("hybrid",),
            entity,
            _labels_for_intent(intent),
            0.90,
            reason_code="CASCADE_RULE_SEMANTIC",
        )

    if not matched and not has_conjunction:
        return CascadeDecision(
            ("concept_detail",),
            ("hybrid",),
            entity,
            ("Concept", "Skill"),
            0.85,
            reason_code="CASCADE_RULE_DEFAULT",
        )

    if classifier is not None:
        try:
            classified = classifier(
                question,
                {
                    "grade": request.grade,
                    "semester": request.semester,
                    "edition": request.edition,
                    "book_id": request.book_id,
                },
            )
            intents = tuple(str(item) for item in classified.intents[:2])
            if not intents or len(classified.intents) > 2:
                raise ValueError("unsupported number of intents")
            routes = tuple(_route_for_intent(intent) for intent in intents)
            labels = tuple(
                dict.fromkeys(
                    label for intent in intents for label in _labels_for_intent(intent)
                )
            )
            return CascadeDecision(
                intents,
                routes,
                str(classified.entities[0].text).strip()
                if getattr(classified, "entities", None)
                else entity,
                labels,
                0.80,
                bool(getattr(classified, "needs_clarification", False)),
                "CASCADE_STRUCTURED",
            )
        except Exception:
            return CascadeDecision(
                tuple(matched[:2]) or ("concept_detail",),
                tuple(_route_for_intent(item) for item in matched[:2])
                or ("hybrid",),
                entity,
                ("Concept", "Skill", "Exercise"),
                0.50,
                True,
                "ROUTER_INVALID_OUTPUT",
            )

    if 1 <= len(matched) <= 2:
        intents = tuple(matched)
        return CascadeDecision(
            intents,
            tuple(_route_for_intent(item) for item in intents),
            entity,
            tuple(
                dict.fromkeys(
                    label for intent in intents for label in _labels_for_intent(intent)
                )
            ),
            0.80,
            False,
            "CASCADE_RULE_MULTI_INTENT",
        )
    return CascadeDecision(
        tuple(matched[:2]) or ("concept_detail",),
        tuple(_route_for_intent(item) for item in matched[:2]) or ("hybrid",),
        entity,
        ("Concept", "Skill", "Exercise"),
        0.50,
        True,
        "CLARIFICATION_REQUIRED",
    )


class RetrievalRouter:
    """Public router facade used by service tests and lightweight callers."""

    def route(self, question: str, *, user_type: str = "student", **filters: object) -> RouteDecision:
        request = RetrievalRequest(question=question, user_type=user_type, **{k: v for k, v in filters.items() if hasattr(RetrievalRequest, k)})
        decision = route_request(request)
        intent_map = {
            "prerequisites": "prerequisite",
            "similar_exercises": "similar_exercise",
        }
        if user_type.lower() == "teacher" and "分析" in question:
            return RouteDecision("teacher_analysis", "text2cypher", decision.entity_text, decision.labels)
        return RouteDecision(intent_map.get(decision.intent, decision.intent), decision.route, decision.entity_text, decision.labels)


def extract_entity_text(question: str) -> str:
    for pattern in ENTITY_PATTERNS:
        match = pattern.search(question)
        if match:
            return match.group(1).strip()
    cleaned = re.sub(
        r"(请问|什么是|哪些|什么|是什么|有哪些|在哪.*|考察.*|前置知识|后续知识|学习|找|[?？。])",
        " ",
        question,
    )
    return " ".join(cleaned.split()).strip()


def _routing_text(question: str) -> str:
    """Mask quoted entity names so their words cannot create extra intents."""

    return QUOTED_ENTITY_PATTERN.sub(
        lambda match: " " * len(match.group(0)),
        question,
    )


def _intent_for_route(route: str) -> str:
    return {
        "cypher": "concept_detail",
        "hybrid": "semantic_search",
        "fulltext": "semantic_search",
        "vector": "semantic_search",
        "text2cypher": "teacher_analysis",
    }.get(route, "semantic_search")


def _labels_for_intent(intent: str) -> tuple[str, ...]:
    if intent == "similar_exercises":
        return ("Exercise",)
    if intent in {"prerequisites", "successors"}:
        return ("Concept", "Skill")
    return ("Concept", "Skill", "Exercise")


def _matched_intents(question: str, *, role: str) -> list[str]:
    positioned: list[tuple[int, int, str]] = []
    similar = any(token in question for token in INTENT_TOKENS["semantic_search"])
    for configured_order, (intent, tokens) in enumerate(INTENT_TOKENS.items()):
        if intent == "teacher_analysis" and role not in {"teacher", "admin"}:
            continue
        positions = [
            question.find(token) for token in tokens if question.find(token) >= 0
        ]
        if not positions:
            continue
        actual = (
            "similar_exercises"
            if intent == "exercises_for" and similar
            else intent
        )
        positioned.append((min(positions), configured_order, actual))
    positioned.sort()
    matched = list(dict.fromkeys(item[2] for item in positioned))
    if "similar_exercises" in matched and "semantic_search" in matched:
        matched.remove("semantic_search")
    return matched


def _route_for_intent(intent: str) -> str:
    if intent in {"prerequisites", "successors", "exercises_for", "location"}:
        return "cypher_template"
    if intent == "teacher_analysis":
        return "teacher_analysis"
    return "hybrid"
