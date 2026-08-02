"""Unified retrieval service combining routing, Cypher templates, and hybrid search."""

from __future__ import annotations

import logging
import uuid
from typing import Any, Callable, Dict, List, Optional

from retrieval.cypher import CypherTemplates
from retrieval.embeddings import DashScopeEmbedding, Embedder, FastEmbedder, HashEmbedder
from retrieval.models import (
    AuthenticationContext,
    EvidenceNode,
    RetrievalFilters,
    RetrievalRequest,
    RetrievalRequestV1,
    RetrievalResponse,
    RetrievalResponseV1,
)
from retrieval.policy import (
    AuthorizationError,
    can_use_teacher_analysis,
    filter_response_value,
    require_permission,
)
from retrieval.retrievers import (
    FulltextRetriever,
    GraphExpansionRetriever,
    GraphRAGHybridRetriever,
    HybridRetriever,
    VectorRetriever,
)
from retrieval.router import RouteDecision, route_cascade, route_request
from retrieval.security import CypherSecurityError, enforce_limit
from retrieval.settings import RetrievalSettings
from retrieval.store import Neo4jStore
from retrieval.text import response_properties

Text2CypherGenerator = Callable[[RetrievalRequest], str]
LOGGER = logging.getLogger(__name__)


class RetrievalBackendUnavailable(RuntimeError):
    """The graph or a required retrieval adapter could not serve the request."""


class RetrievalService:
    def __init__(
        self,
        store: Optional[Neo4jStore] = None,
        settings: Optional[RetrievalSettings] = None,
        *,
        embedder: Optional[Embedder] = None,
        text2cypher_generator: Optional[Text2CypherGenerator] = None,
        intent_classifier: Optional[Any] = None,
        teacher_plan_generator: Optional[Any] = None,
        retriever: Optional[Any] = None,
    ) -> None:
        self.external_retriever = retriever
        self.store = store
        self.settings = settings or RetrievalSettings.from_env()
        self.embedder = embedder
        self.text2cypher_generator = text2cypher_generator
        self.intent_classifier = intent_classifier
        self.teacher_plan_generator = teacher_plan_generator
        if self.external_retriever is None:
            if self.store is None:
                raise ValueError("store is required when no external retriever is supplied")
            self.__post_init__()

    def __post_init__(self) -> None:
        if self.embedder is None:
            if self.settings.embedding_provider == "openai":
                self.embedder = DashScopeEmbedding(
                    self.settings.embedding_model,
                    self.settings.embedding_dimension,
                    api_key=self.settings.embedding_api_key,
                    base_url=self.settings.embedding_base_url
                    or "https://dashscope.aliyuncs.com/compatible-mode/v1",
                    timeout=2.0,
                )
            elif self.settings.embedding_provider == "hash":
                self.embedder = HashEmbedder(self.settings.embedding_dimension)
            else:
                self.embedder = FastEmbedder(
                    self.settings.embedding_model,
                    self.settings.embedding_dimension,
                )
        assert self.store is not None
        self.cypher = CypherTemplates(self.store, max_depth=self.settings.max_path_depth, timeout=self.settings.query_timeout_seconds)
        self.fulltext = FulltextRetriever(self.store, timeout=self.settings.query_timeout_seconds)
        self.vector = VectorRetriever(self.store, self.embedder, timeout=self.settings.query_timeout_seconds) if self.embedder else None
        local_hybrid = HybridRetriever(self.fulltext, self.vector)
        if self.settings.hybrid_backend == "graphrag" and self.embedder is not None:
            self.hybrid = GraphRAGHybridRetriever(
                self.store,
                self.embedder,
                local_hybrid,
                database=self.settings.neo4j_database,
            )
        else:
            self.hybrid = local_hybrid
        self.expander = GraphExpansionRetriever(self.store, timeout=self.settings.query_timeout_seconds)

    def warm_embedding(self) -> None:
        """Materialize the local model before the API reports startup complete."""

        if isinstance(self.embedder, FastEmbedder):
            self.embedder.embed(["小学数学检索预热"])

    def search(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Dict-oriented facade for HTTP/tests; always sanitizes student output."""

        request = _request_dict(payload)
        student_safe = str(request.get("user_type", "student")).lower() != "teacher"
        if self.external_retriever is not None:
            response = self.external_retriever.retrieve(request)
            return _sanitize_response_dict(dict(response), student_safe=student_safe)
        response = self.retrieve(RetrievalRequest.from_dict(request))
        return response.to_dict(student_safe=student_safe)

    def retrieve(
        self,
        request: RetrievalRequest,
        *,
        decision_override: Optional[RouteDecision] = None,
    ) -> RetrievalResponse:
        top_k = self.settings.clamp_top_k(request.top_k)
        student_safe = request.user_type.lower() != "teacher" or not request.include_answers
        filters = RetrievalFilters.from_request(request)
        decision = decision_override or route_request(request)
        warnings: List[str] = []
        entities: List[EvidenceNode] = []
        evidence: List[EvidenceNode] = []
        paths = []
        locations = []
        cypher_text = None

        try:
            if decision.route == "cypher":
                if decision.intent == "prerequisites":
                    paths = self.cypher.prerequisites(decision.entity_text, filters, top_k=top_k, depth=self.settings.max_path_depth, student_safe=student_safe)
                    evidence = _dedupe_nodes(
                        node for path in paths for node in path.nodes
                    )
                    entities = _path_targets(paths)
                elif decision.intent == "successors":
                    paths = self.cypher.successors(decision.entity_text, filters, top_k=top_k, depth=self.settings.max_path_depth, student_safe=student_safe)
                    evidence = _dedupe_nodes(
                        node for path in paths for node in path.nodes
                    )
                    entities = _path_targets(paths)
                elif decision.intent == "exercises_for":
                    evidence = self.cypher.exercises_for(decision.entity_text, filters, top_k=top_k, student_safe=student_safe)
                elif decision.intent == "location":
                    evidence = self.cypher.entity_detail(
                        decision.entity_text,
                        filters,
                        labels=decision.labels,
                        top_k=1,
                        student_safe=student_safe,
                    )
                else:
                    evidence = self.cypher.concept_detail(decision.entity_text, filters, top_k=top_k, student_safe=student_safe)
                if decision.intent not in {"prerequisites", "successors"}:
                    entities = evidence[:top_k]
            elif decision.route == "text2cypher":
                response = self._text2cypher(request, decision, filters, top_k=top_k, student_safe=student_safe)
                if response is not None:
                    return response
                warnings.append("Text2Cypher unavailable or rejected; fell back to hybrid retrieval.")
                evidence = self._hybrid_search(
                    request.question,
                    labels=decision.labels,
                    filters=filters,
                    top_k=top_k,
                    student_safe=student_safe,
                    warnings=warnings,
                )
                entities = evidence
            elif decision.route == "fulltext":
                evidence = self.fulltext.search(
                    decision.entity_text or request.question,
                    labels=decision.labels,
                    filters=filters,
                    top_k=top_k,
                    student_safe=student_safe,
                )
                entities = evidence
            elif decision.route == "vector":
                if self.vector is None:
                    raise RuntimeError("vector retriever is not configured")
                evidence = self.vector.search(
                    decision.entity_text or request.question,
                    labels=decision.labels,
                    filters=filters,
                    top_k=top_k,
                    student_safe=student_safe,
                )
                entities = evidence
            else:
                evidence = self._hybrid_search(
                    decision.entity_text or request.question,
                    labels=decision.labels,
                    filters=filters,
                    top_k=top_k,
                    student_safe=student_safe,
                    warnings=warnings,
                )
                entities = evidence
                expanded = self.expander.expand([node.id for node in entities], top_k=top_k, student_safe=student_safe)
                evidence = entities + [node for node in expanded if node.id not in {e.id for e in entities}]

            locations.extend(
                self.cypher.textbook_locations_for_nodes(
                    [node.id for node in entities[:top_k]],
                    top_k=top_k,
                )
            )
        except Exception as exc:
            LOGGER.exception("retrieval backend failed")
            raise RetrievalBackendUnavailable("retrieval backend unavailable") from exc

        if not evidence and not warnings:
            warnings.append("No matching knowledge graph evidence found.")
        return RetrievalResponse(
            intent=decision.intent,
            route=decision.route,
            query=request.question,
            entities=entities,
            evidence_nodes=evidence[: self.settings.max_top_k],
            relation_paths=paths,
            textbook_locations=_dedupe_locations(locations)[: self.settings.max_top_k],
            warnings=warnings,
            cypher=cypher_text,
        )

    def _hybrid_search(
        self,
        query: str,
        *,
        labels: tuple[str, ...],
        filters: RetrievalFilters,
        top_k: int,
        student_safe: bool,
        warnings: List[str],
    ) -> List[EvidenceNode]:
        kwargs = {
            "labels": labels,
            "filters": filters,
            "top_k": top_k,
            "student_safe": student_safe,
        }
        if isinstance(self.hybrid, GraphRAGHybridRetriever):
            return self.hybrid.search(query, warning_sink=warnings, **kwargs)
        return self.hybrid.search(query, **kwargs)

    def search_v1(
        self,
        request: RetrievalRequestV1,
        context: AuthenticationContext,
        *,
        request_id: Optional[str] = None,
    ) -> RetrievalResponseV1:
        """Canonical authenticated retrieval path."""

        require_permission(context, "retrieval:read")
        classifier = self.intent_classifier
        classifier_call = None
        if classifier is not None:
            classifier_call = (
                classifier.classify if hasattr(classifier, "classify") else classifier
            )
        cascade = route_cascade(
            request,
            role=context.role,
            classifier=classifier_call,
        )
        rid = request_id or str(uuid.uuid4())
        if cascade.needs_clarification:
            return RetrievalResponseV1(
                request_id=rid,
                intents=list(cascade.intents),
                route="none",
                reason_code=(
                    cascade.reason_code
                    if cascade.reason_code.startswith("ROUTER_")
                    else "CLARIFICATION_REQUIRED"
                ),
                needs_clarification=True,
            )

        legacy_responses: List[RetrievalResponse] = []
        for intent, route in zip(
            cascade.intents[:2], cascade.routes[:2], strict=True
        ):
            if route == "teacher_analysis":
                if not can_use_teacher_analysis(context):
                    raise AuthorizationError(
                        "retrieval:text2cypher permission is required"
                    )
                teacher_response = self._teacher_analysis_v1(
                    request,
                    context,
                    intent=intent,
                )
                if teacher_response is not None:
                    legacy_responses.append(teacher_response)
                continue
            legacy = request.to_legacy(context)
            legacy_route = "cypher" if route == "cypher_template" else "hybrid"
            legacy_responses.append(
                self.retrieve(
                    legacy,
                    decision_override=RouteDecision(
                        intent=intent,
                        route=legacy_route,
                        entity_text=cascade.entity_text,
                        labels=_labels_for_intent(intent),
                    ),
                )
            )

        entities = _dedupe_nodes(
            node for response in legacy_responses for node in response.entities
        )
        evidence = _dedupe_nodes(
            node
            for response in legacy_responses
            for node in response.evidence_nodes
        )
        paths = [
            path for response in legacy_responses for path in response.relation_paths
        ]
        locations = _dedupe_locations(
            location
            for response in legacy_responses
            for location in response.textbook_locations
        )
        analysis_results = [
            item
            for response in legacy_responses
            for item in response.analysis_results
        ]
        warnings = list(
            dict.fromkeys(
                warning
                for response in legacy_responses
                for warning in response.warnings
            )
        )
        no_result = not entities and not evidence and not paths
        reason_code = _response_reason_code(warnings, no_result=no_result)
        selected_route = (
            cascade.routes[0]
            if len(set(cascade.routes)) == 1
            else "hybrid"
        )
        payload = {
            "request_id": rid,
            "intents": list(cascade.intents),
            "route": selected_route,
            "entities": [_node_v1(node) for node in entities],
            "evidence_nodes": [_node_v1(node) for node in evidence],
            "relation_paths": [
                {
                    "start_id": path.start_id,
                    "end_id": path.end_id,
                    "relationships": path.relationships[:3],
                    "nodes": [_node_v1(node) for node in path.nodes],
                }
                for path in paths
            ],
            "textbook_locations": locations,
            "analysis_results": analysis_results,
            "scores": [
                {"node_id": node.id, "final": node.score} for node in entities
            ],
            "warnings": warnings,
            "reason_code": reason_code,
            "needs_clarification": False,
        }
        return RetrievalResponseV1.model_validate(
            filter_response_value(payload, context)
        )

    def _teacher_analysis_v1(
        self,
        request: RetrievalRequestV1,
        context: AuthenticationContext,
        *,
        intent: str,
    ) -> Optional[RetrievalResponse]:
        if self.teacher_plan_generator is None or self.store is None:
            return RetrievalResponse(
                intent=intent,
                route="teacher_analysis",
                query=request.question,
                warnings=["TEACHER_ANALYSIS_DISABLED"],
            )
        from retrieval.teacher_analysis import compile_teacher_analysis_plan

        try:
            plan = self.teacher_plan_generator.generate(
                request.question,
                {
                    "grade": request.grade,
                    "semester": request.semester,
                    "edition": request.edition,
                    "book_id": request.book_id,
                    "section_id": request.section_id,
                    "exercise_type": request.exercise_type,
                    "difficulty": request.difficulty,
                },
            )
            compiled = compile_teacher_analysis_plan(plan)
        except Exception:
            LOGGER.exception("teacher analysis plan generation or validation failed")
            return RetrievalResponse(
                intent=intent,
                route="teacher_analysis",
                query=request.question,
                warnings=["TEACHER_ANALYSIS_PLAN_INVALID"],
            )
        try:
            rows = self.store.run_read(
                compiled.cypher,
                compiled.parameters,
                timeout=self.settings.query_timeout_seconds,
            )
        except Exception as exc:
            LOGGER.exception("teacher analysis execution failed")
            raise RetrievalBackendUnavailable(
                "teacher analysis backend unavailable"
            ) from exc
        nodes: List[EvidenceNode] = []
        for row in rows:
            if {"id", "label", "name"}.issubset(row):
                label = str(row["label"])
                props = row.get("properties", {})
                if not isinstance(props, dict):
                    props = {}
                nodes.append(
                    EvidenceNode(
                        id=str(row["id"]),
                        label=label,
                        name=str(row["name"]),
                        properties=props,
                    )
                )
        return RetrievalResponse(
            intent=intent,
            route="teacher_analysis",
            query=request.question,
            entities=nodes,
            evidence_nodes=nodes,
            analysis_results=[
                {
                    key: value
                    for key, value in row.items()
                    if key in {"key", "value"}
                }
                for row in rows
                if "value" in row
            ],
            warnings=[] if rows else ["NO_RESULT"],
        )

    def _text2cypher(
        self,
        request: RetrievalRequest,
        decision: RouteDecision,
        filters: RetrievalFilters,
        *,
        top_k: int,
        student_safe: bool,
    ) -> Optional[RetrievalResponse]:
        if request.user_type.lower() != "teacher" or self.text2cypher_generator is None:
            return None
        try:
            cypher_text = enforce_limit(self.text2cypher_generator(request), limit=top_k)
        except CypherSecurityError:
            return None
        try:
            rows = self.store.run_read(
                cypher_text,
                filters.as_params(),
                timeout=self.settings.query_timeout_seconds,
            )
        except Exception:
            LOGGER.exception("Text2Cypher execution failed; falling back")
            return None
        nodes: List[EvidenceNode] = []
        for row in rows:
            if {"id", "label", "name"}.issubset(row):
                props = row.get("properties", {}) if isinstance(row.get("properties"), dict) else {}
                label = str(row["label"])
                nodes.append(
                    EvidenceNode(
                        str(row["id"]),
                        label,
                        str(row["name"]),
                        float(row.get("score", 0.0) or 0.0),
                        response_properties(label, props, student_safe=student_safe),
                    )
                )
        return RetrievalResponse(
            intent=decision.intent,
            route=decision.route,
            query=request.question,
            entities=nodes,
            evidence_nodes=nodes,
            warnings=[],
            cypher=cypher_text if not student_safe else None,
        )


def _request_dict(payload: Dict[str, Any]) -> Dict[str, Any]:
    query = payload.get("query", payload.get("question", ""))
    out = dict(payload)
    out["query"] = str(query).strip()
    out["question"] = out["query"]
    out.setdefault("user_type", "student")
    out.setdefault("top_k", 5)
    return out


def _dedupe_nodes(nodes: Any) -> List[EvidenceNode]:
    unique: Dict[str, EvidenceNode] = {}
    for node in nodes:
        unique.setdefault(node.id, node)
    return list(unique.values())


def _path_targets(paths: Any) -> List[EvidenceNode]:
    target_ids = {path.start_id for path in paths}
    return _dedupe_nodes(
        node
        for path in paths
        for node in path.nodes
        if node.id in target_ids
    )


def _dedupe_locations(locations: Any) -> List[Dict[str, Any]]:
    unique: Dict[tuple[Any, ...], Dict[str, Any]] = {}
    for location in locations:
        key = tuple(
            location.get(field)
            for field in ("edition", "book_id", "chapter_id", "section_id")
        )
        unique.setdefault(key, location)
    return list(unique.values())


def _sanitize_response_dict(response: Dict[str, Any], *, student_safe: bool) -> Dict[str, Any]:
    blocked = {"embedding", "search_text", "teacher_search_text", "cjk_search_text"}
    if student_safe:
        blocked.update({"answer", "analysis", "explanation", "solution", "cypher"})

    def scrub(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                key: scrub(item)
                for key, item in value.items()
                if key not in blocked and not key.endswith("_search_text")
            }
        if isinstance(value, list):
            return [scrub(item) for item in value]
        return value

    return scrub(response)


def _node_v1(node: EvidenceNode) -> Dict[str, Any]:
    return {
        "id": node.id,
        "label": node.label,
        "name": node.name,
        "score": max(0.0, float(node.score)),
        "properties": dict(node.properties),
    }


def _labels_for_intent(intent: str) -> tuple[str, ...]:
    if intent == "similar_exercises":
        return ("Exercise",)
    if intent in {"prerequisites", "successors", "exercises_for"}:
        return ("Concept", "Skill")
    if intent == "concept_detail":
        return ("Concept", "Skill")
    return ("Concept", "Skill", "Exercise")


def _response_reason_code(warnings: List[str], *, no_result: bool) -> str:
    explicit_codes = (
        "TEACHER_ANALYSIS_DISABLED",
        "TEACHER_ANALYSIS_PLAN_INVALID",
    )
    for code in explicit_codes:
        if code in warnings:
            return code
    return "NO_RESULT" if no_result else "OK"
