from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from retrieval import api
from retrieval.data_prep import enrich_graph, metadata_from_id, normalize_node
from retrieval.graphrag_adapter import INDEXES
from retrieval.indexes import fulltext_statements, vector_statements
from retrieval.models import RetrievalRequest
from retrieval.router import extract_entity_text, route_request
from retrieval.security import UnsafeCypherError, validate_readonly_cypher
from retrieval.settings import RetrievalSettings
from retrieval.text2cypher import OpenAIText2CypherGenerator, generator_from_env


def test_primary_math_scope_is_inferred_from_book_id():
    assert metadata_from_id("math_3b_rjb_cpt1") == {
        "subject": "数学",
        "stage": "小学",
        "grade": "3",
        "semester": "下册",
        "edition": "人教版",
        "book_id": "math_3b_rjb",
    }


def test_normalized_student_exercise_text_never_contains_answer_or_analysis():
    node = normalize_node(
        {
            "id": "math_3b_rjb_exe1",
            "label": "Exercise",
            "name": "平均分",
            "properties": {
                "stem": "12个苹果平均分给3人",
                "answer": "4个",
                "analysis": "12 ÷ 3 = 4",
            },
        }
    )
    assert "12个苹果" in node["properties"]["search_text"]
    assert "4个" not in node["properties"]["search_text"]
    assert "12 / 3 = 4" not in node["properties"]["search_text"]
    assert "4个" in node["properties"]["teacher_search_text"]


def test_student_safe_properties_remove_vectors_and_teacher_search_text():
    from retrieval.text import student_safe_properties

    properties = student_safe_properties(
        "Exercise",
        {
            "stem": "12个苹果平均分",
            "answer": "4个",
            "embedding": [0.1, 0.2],
            "search_text": "学生检索文本",
            "teacher_search_text": "学生检索文本 4个",
            "cjk_search_text": "内部全文索引文本",
        },
    )
    assert properties == {"stem": "12个苹果平均分"}


def test_student_safe_properties_remove_answer_fields_from_every_label():
    from retrieval.text import student_safe_properties

    properties = student_safe_properties(
        "Concept",
        {
            "definition": "数轴上的距离",
            "answer": None,
            "analysis": None,
        },
    )
    assert properties == {"definition": "数轴上的距离"}


def test_all_vector_indexes_use_the_same_embedding_property_as_importers():
    statements = vector_statements(512)
    assert len(statements) == 3
    assert all("ON (n.embedding)" in statement for statement in statements)
    assert set(INDEXES) == {"Concept", "Skill", "Exercise"}


def test_fulltext_indexes_use_cjk_analyzer_for_chinese_math():
    statements = fulltext_statements()
    assert len(statements) == 3
    assert all("`fulltext.analyzer`: 'cjk'" in statement for statement in statements)


def test_appears_in_edges_are_denormalized_for_scope_filtering():
    graph = enrich_graph(
        [{"id": "math_3b_rjb_cpt1", "label": "Concept", "name": "小数", "properties": {}}],
        [
            {
                "source": "math_3b_rjb_cpt1",
                "target": "math_3b_rjb_ch2_s1",
                "type": "appears_in",
            }
        ],
        embed=False,
        settings=RetrievalSettings(),
        offline_hash=False,
    )
    props = graph["nodes"][0]["properties"]
    assert props["section_id"] == "math_3b_rjb_ch2_s1"
    assert props["chapter_id"] == "math_3b_rjb_ch2"


def test_entity_extraction_handles_common_primary_math_questions():
    assert extract_entity_text("学习分数前要会什么？") == "分数"
    assert extract_entity_text("什么是小数？") == "小数"
    assert extract_entity_text("哪些题考察乘法？") == "乘法"


def test_forced_retrieval_method_preserves_natural_intent():
    decision = route_request(
        RetrievalRequest(
            question="学习分数前要会什么？",
            route="vector",
        )
    )
    assert decision.route == "vector"
    assert decision.intent == "prerequisites"
    assert decision.labels == ("Concept", "Skill")


def test_text2cypher_rejects_non_allowlisted_call():
    try:
        validate_readonly_cypher("CALL custom.writeProcedure() YIELD value RETURN value LIMIT 1")
    except UnsafeCypherError:
        pass
    else:
        raise AssertionError("non-allowlisted CALL must be rejected")


def test_text2cypher_generator_strips_fences_and_output_is_guarded():
    class FakeClient:
        def generate(self, prompt, **kwargs):
            assert "只读 Cypher" in prompt
            return "```cypher\nMATCH (c:Concept) RETURN c.id AS id LIMIT 3\n```"

    generated = OpenAIText2CypherGenerator(FakeClient())(  # type: ignore[arg-type]
        RetrievalRequest(question="列出知识点", user_type="teacher", top_k=3)
    )
    assert generated.startswith("MATCH")
    assert validate_readonly_cypher(generated) == generated


def test_text2cypher_is_disabled_by_default(monkeypatch):
    monkeypatch.delenv("K12_TEXT2CYPHER_ENABLED", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert generator_from_env() is None


def test_api_exposes_compatible_and_canonical_search_routes(monkeypatch):
    monkeypatch.delenv("NEO4J_PASSWORD", raising=False)
    monkeypatch.setenv("K12_RETRIEVAL_EMBEDDING_PROVIDER", "hash")
    monkeypatch.setenv("K12_RETRIEVAL_HYBRID_BACKEND", "local")
    monkeypatch.setattr("retrieval.store.Neo4jStore.run_read", lambda *args, **kwargs: [])
    with TestClient(api.create_app()) as client:
        payload = {"query": "学习分数前要会什么", "user_type": "student", "top_k": 3}
        for path in ("/retrieve", "/retrieval/search"):
            response = client.post(path, json=payload)
            assert response.status_code == 200
            body = response.json()
            assert body["intent"] == "prerequisites"
            assert "cypher" not in body


def test_legacy_search_returns_503_when_graph_backend_is_unavailable(monkeypatch):
    monkeypatch.delenv("NEO4J_PASSWORD", raising=False)
    with TestClient(api.create_app()) as client:
        response = client.post(
            "/retrieve",
            json={"query": "学习分数前要会什么", "top_k": 3},
        )

    assert response.status_code == 503
    assert response.json() == {
        "detail": {"reason_code": "GRAPH_BACKEND_UNAVAILABLE"}
    }


def test_legacy_search_rejects_unsafe_question_before_retrieval(monkeypatch):
    monkeypatch.delenv("NEO4J_PASSWORD", raising=False)
    monkeypatch.setenv("K12_RETRIEVAL_EMBEDDING_PROVIDER", "hash")
    monkeypatch.setenv("K12_RETRIEVAL_HYBRID_BACKEND", "local")
    def fail_if_request_reaches_neo4j(_store, query, *args, **kwargs):
        if query == "RETURN 1 AS value":
            return []
        pytest.fail("unsafe input reached Neo4j")

    monkeypatch.setattr("retrieval.store.Neo4jStore.run_read", fail_if_request_reaches_neo4j)
    with TestClient(api.create_app()) as client:
        for path in ("/retrieve", "/retrieval/search"):
            response = client.post(
                path,
                json={"query": "解释分数；ＣＡＬＬ apoc.load.json('https://example')"},
            )
            assert response.status_code == 403
            assert response.json() == {
                "detail": {"reason_code": "UNSAFE_QUERY_REJECTED"}
            }


def test_health_is_unhealthy_without_neo4j_credentials(monkeypatch):
    monkeypatch.delenv("NEO4J_PASSWORD", raising=False)
    with TestClient(api.create_app()) as client:
        response = client.get("/health")
        assert response.status_code == 503
        assert response.json()["ok"] is False


def test_fastembed_is_warmed_before_application_becomes_ready(monkeypatch):
    warmed = []
    monkeypatch.delenv("NEO4J_PASSWORD", raising=False)
    monkeypatch.setenv("K12_RETRIEVAL_EMBEDDING_PROVIDER", "fastembed")
    monkeypatch.setattr(
        "retrieval.store.Neo4jStore.run_read",
        lambda *args, **kwargs: [],
    )
    monkeypatch.setattr(
        "retrieval.service.RetrievalService.warm_embedding",
        lambda self: warmed.append(self.settings.embedding_provider),
        raising=False,
    )

    with TestClient(api.create_app()) as client:
        assert client.get("/health").status_code == 503

    assert warmed == ["fastembed"]


def test_checked_in_golden_set_has_no_secret_material():
    text = Path("eval/retrieval/retrieval_goldens.jsonl").read_text(encoding="utf-8")
    assert "NEO4J_PASSWORD" not in text
    assert "OPENAI_API_KEY" not in text
