import pytest

from retrieval.embeddings import HashEmbedder
from retrieval.models import AuthenticationContext, RetrievalRequestV1
from retrieval.policy import AuthorizationError
from retrieval.service import RetrievalBackendUnavailable, RetrievalService
from retrieval.settings import RetrievalSettings
from retrieval.teacher_analysis import TeacherAnalysisPlanV1


class RecordingStore:
    def __init__(self):
        self.calls = []

    def run_read(self, query, parameters=None, *, timeout=None):
        self.calls.append((query, parameters or {}, timeout))
        if "count(DISTINCT n) AS value" in query:
            return [{"value": 12}]
        return []


class FixedTeacherPlanGenerator:
    def generate(self, question, filters):
        assert "统计" in question
        return TeacherAnalysisPlanV1(
            operation="count",
            entity_type="Concept",
            filters={"grade": "四年级"},
            limit=5,
        )


class InvalidTeacherPlanGenerator:
    def generate(self, question, filters):
        raise ValueError("invalid plan")


class FailingTeacherAnalysisStore(RecordingStore):
    def run_read(self, query, parameters=None, *, timeout=None):
        raise ConnectionError("neo4j unavailable")


def _service(store):
    settings = RetrievalSettings(
        hybrid_backend="local",
        embedding_provider="hash",
    )
    return RetrievalService(
        store,  # type: ignore[arg-type]
        settings,
        embedder=HashEmbedder(512),
        teacher_plan_generator=FixedTeacherPlanGenerator(),
    )


def test_teacher_analysis_executes_only_static_compiler_output():
    store = RecordingStore()
    service = _service(store)
    context = AuthenticationContext(
        principal_id="teacher-1",
        role="teacher",
        permissions=frozenset(
            {
                "retrieval:read",
                "retrieval:answers",
                "retrieval:text2cypher",
            }
        ),
    )
    response = service.search_v1(
        RetrievalRequestV1(question="统计四年级知识点数量"),
        context,
    )
    assert response.route == "teacher_analysis"
    assert response.analysis_results == [{"value": 12}]
    query, params, _timeout = store.calls[0]
    assert query.startswith("MATCH (n:Concept)")
    assert "CALL" not in query
    assert params == {"limit": 5, "grade": "四年级"}


def test_teacher_analysis_requires_explicit_permission():
    service = _service(RecordingStore())
    context = AuthenticationContext(
        principal_id="teacher-1",
        role="teacher",
        permissions=frozenset({"retrieval:read"}),
    )
    with pytest.raises(AuthorizationError):
        service.search_v1(
            RetrievalRequestV1(question="统计四年级知识点数量"),
            context,
        )


def test_teacher_analysis_reports_disabled_and_invalid_plan_reason_codes():
    context = AuthenticationContext(
        principal_id="teacher-1",
        role="teacher",
        permissions=frozenset({"retrieval:read", "retrieval:text2cypher"}),
    )
    disabled = RetrievalService(
        RecordingStore(),  # type: ignore[arg-type]
        RetrievalSettings(hybrid_backend="local", embedding_provider="hash"),
        embedder=HashEmbedder(512),
    )
    disabled_response = disabled.search_v1(
        RetrievalRequestV1(question="统计四年级知识点数量"),
        context,
    )
    assert disabled_response.reason_code == "TEACHER_ANALYSIS_DISABLED"

    invalid = RetrievalService(
        RecordingStore(),  # type: ignore[arg-type]
        RetrievalSettings(hybrid_backend="local", embedding_provider="hash"),
        embedder=HashEmbedder(512),
        teacher_plan_generator=InvalidTeacherPlanGenerator(),
    )
    invalid_response = invalid.search_v1(
        RetrievalRequestV1(question="统计四年级知识点数量"),
        context,
    )
    assert invalid_response.reason_code == "TEACHER_ANALYSIS_PLAN_INVALID"


def test_teacher_analysis_execution_failure_is_backend_unavailable():
    service = _service(FailingTeacherAnalysisStore())
    context = AuthenticationContext(
        principal_id="teacher-1",
        role="teacher",
        permissions=frozenset({"retrieval:read", "retrieval:text2cypher"}),
    )

    with pytest.raises(RetrievalBackendUnavailable):
        service.search_v1(
            RetrievalRequestV1(question="统计四年级知识点数量"),
            context,
        )
