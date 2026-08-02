from __future__ import annotations

from types import SimpleNamespace

import pytest

from retrieval.import_neo4j import import_nodes
from retrieval.models import EvidenceNode, RetrievalFilters, RetrievalRequest
from retrieval.retrievers import GraphRAGHybridRetriever
from retrieval.service import RetrievalBackendUnavailable, RetrievalService
from retrieval.settings import RetrievalSettings


class FakeEmbedder:
    def embed(self, texts):
        return [[0.0, 1.0] for _ in texts]


class FailingEmbedder:
    def embed(self, _texts):
        raise TimeoutError("embedding adapter unavailable")


class RecordingStore:
    def __init__(self, responses=None):
        self.responses = list(responses or [])
        self.calls = []

    def run_read(self, query, parameters=None, *, timeout=None):
        self.calls.append((query, parameters or {}, timeout))
        if self.responses:
            response = self.responses.pop(0)
            if isinstance(response, Exception):
                raise response
            return response
        return []

    def run_write(self, query, parameters=None):
        self.calls.append((query, parameters or {}, None))
        return []


def test_request_wires_textbook_and_exercise_filters():
    request = RetrievalRequest.from_dict(
        {
            "question": "找练习题",
            "book_id": "math_3b_rjb",
            "section_id": "math_3b_rjb_ch2_s1",
            "type": "选择题",
            "difficulty": "2",
        }
    )
    filters = RetrievalFilters.from_request(request)
    assert filters.book_id == "math_3b_rjb"
    assert filters.section_id == "math_3b_rjb_ch2_s1"
    assert filters.exercise_type == "选择题"
    assert filters.difficulty == 2


def test_exercise_template_passes_all_scope_filters():
    store = RecordingStore()
    service = RetrievalService(store=store, settings=RetrievalSettings(), embedder=FakeEmbedder())
    service.retrieve(
        RetrievalRequest(
            question="哪些题考察【乘法】？",
            book_id="math_3b_rjb",
            section_id="math_3b_rjb_ch2_s1",
            exercise_type="选择题",
            difficulty=2,
        )
    )
    query, parameters, _ = store.calls[0]
    assert "$book_id" in query and parameters["book_id"] == "math_3b_rjb"
    assert "$section_id" in query and parameters["section_id"] == "math_3b_rjb_ch2_s1"
    assert "$exercise_type" in query and parameters["exercise_type"] == "选择题"
    assert "$difficulty" in query and parameters["difficulty"] == 2
    assert "WHEN n.name = $name THEN 0" in query
    assert "LIMIT 1" in query


def test_importer_rejects_non_allowlisted_node_label():
    store = RecordingStore()
    with pytest.raises(ValueError, match="unsupported node labels"):
        import_nodes(
            store,
            [{"id": "bad-1", "label": "Concept`) CREATE (:Injected", "name": "bad"}],
            batch_size=10,
        )
    assert not store.calls


def test_text2cypher_execution_failure_falls_back_to_hybrid():
    store = RecordingStore([RuntimeError("database rejected generated query")])
    service = RetrievalService(
        store=store,
        settings=RetrievalSettings(),
        embedder=FakeEmbedder(),
        text2cypher_generator=lambda _request: (
            "MATCH (n:Concept) RETURN n.id AS id, labels(n)[0] AS label, "
            "n.name AS name, properties(n) AS properties LIMIT 5"
        ),
    )

    class FakeHybrid:
        def search(self, *args, **kwargs):
            return []

    service.hybrid = FakeHybrid()
    response = service.retrieve(
        RetrievalRequest(question="分析知识点分布", user_type="teacher", top_k=5)
    )
    assert any("fell back to hybrid" in warning for warning in response.warnings)
    assert not any("backend unavailable" in warning for warning in response.warnings)


def test_location_query_supports_skill_and_returns_textbook_evidence():
    store = RecordingStore(
        [
            [
                {
                    "id": "skill-fraction",
                    "label": "Skill",
                    "name": "分数计算",
                    "properties": {},
                    "score": 1.0,
                }
            ],
            [
                {
                    "node_id": "skill-fraction",
                    "locations": [
                        {
                            "book_id": "math_5a_rjb",
                            "section_id": "math_5a_rjb_ch2_s1",
                            "section_name": "分数计算",
                        }
                    ],
                }
            ],
        ]
    )
    service = RetrievalService(store=store, settings=RetrievalSettings(), embedder=FakeEmbedder())
    response = service.retrieve(
        RetrievalRequest(question="【分数计算】在哪一节？", top_k=5)
    )
    assert response.entities == [
        EvidenceNode("skill-fraction", "Skill", "分数计算", 1.0, {})
    ]
    assert response.textbook_locations[0]["section_id"] == "math_5a_rjb_ch2_s1"
    assert "n:Skill" in store.calls[0][0]
    assert store.calls[0][1]["top_k"] == 1


def test_repeated_entity_locations_are_deduplicated():
    entity_rows = [
        {
            "id": node_id,
            "label": "Concept",
            "name": name,
            "properties": {},
            "score": 1.0,
        }
        for node_id, name in (("concept-fraction", "分数"), ("concept-decimal", "小数"))
    ]
    location = {
        "edition": "人教版",
        "book_id": "math_3b_rjb",
        "chapter_id": "math_3b_rjb_ch2",
        "section_id": "math_3b_rjb_ch2_s1",
    }
    store = RecordingStore([entity_rows, [{"locations": [location]}], [{"locations": [location]}]])
    service = RetrievalService(store=store, settings=RetrievalSettings(), embedder=FakeEmbedder())
    response = service.retrieve(
        RetrievalRequest(question="什么是【分数】？", route="cypher", top_k=5)
    )
    assert response.textbook_locations == [location]


def test_hybrid_location_evidence_is_loaded_in_one_bounded_batch():
    store = RecordingStore([[], []])
    service = RetrievalService(
        store=store,
        settings=RetrievalSettings(hybrid_backend="local"),
        embedder=FakeEmbedder(),
    )
    nodes = [
        EvidenceNode(f"concept-{index}", "Concept", f"知识点{index}", 1.0, {})
        for index in range(10)
    ]
    service.hybrid = SimpleNamespace(search=lambda *args, **kwargs: nodes)

    service.retrieve(RetrievalRequest(question="解释【分数】", top_k=10))

    assert len(store.calls) == 2
    location_query, location_params, _ = store.calls[1]
    assert "UNWIND $node_ids" in location_query
    assert location_params["node_ids"] == [node.id for node in nodes]


def test_prerequisite_response_separates_target_from_deduplicated_evidence():
    path_row = {
        "start_id": "concept-fraction",
        "end_id": "concept-division",
        "relationships": ["prerequisites_for"],
        "nodes": [
            {
                "id": "concept-division",
                "label": "Concept",
                "name": "除法",
                "properties": {},
                "score": 1.0,
            },
            {
                "id": "concept-fraction",
                "label": "Concept",
                "name": "分数",
                "properties": {},
                "score": 1.0,
            },
        ],
    }
    store = RecordingStore([[path_row, path_row], []])
    service = RetrievalService(store=store, settings=RetrievalSettings(), embedder=FakeEmbedder())
    response = service.retrieve(
        RetrievalRequest(question="学习【分数】前要会什么？", top_k=5)
    )
    assert [node.id for node in response.entities] == ["concept-fraction"]
    assert [node.id for node in response.evidence_nodes] == [
        "concept-division",
        "concept-fraction",
    ]


def test_official_graphrag_backend_overretrieves_filters_and_sanitizes(monkeypatch):
    calls = []

    class OfficialRetriever:
        def search(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(
                items=[
                    SimpleNamespace(
                        content={
                            "id": "concept-fraction",
                            "label": "Concept",
                            "name": "分数",
                            "properties": {
                                "definition": "整体平均分",
                                "embedding": [0.1, 0.2],
                            },
                        },
                        metadata={"score": 0.9},
                    )
                ]
            )

    monkeypatch.setattr(
        "retrieval.retrievers.make_hybrid_cypher_retriever",
        lambda *args, **kwargs: OfficialRetriever(),
    )
    store = SimpleNamespace(driver=object())
    fallback = SimpleNamespace(
        search=lambda *args, **kwargs: [
            EvidenceNode(
                "concept-decimal",
                "Concept",
                "小数",
                0.8,
                {"definition": "十进分数"},
            )
        ]
    )
    retriever = GraphRAGHybridRetriever(
        store,  # type: ignore[arg-type]
        FakeEmbedder(),
        fallback,  # type: ignore[arg-type]
        database="neo4j",
    )
    results = retriever.search(
        "什么是分数/小数",
        labels=("Concept",),
        filters=RetrievalFilters(subject="数学", stage="小学", grade="3"),
        top_k=3,
        student_safe=True,
    )
    assert [node.id for node in results] == [
        "concept-fraction",
        "concept-decimal",
    ]
    assert results[0].properties == {"definition": "整体平均分"}
    assert calls[0]["top_k"] == 15
    assert calls[0]["effective_search_ratio"] == 5
    assert calls[0]["query_params"]["stage"] == "小学"
    assert calls[0]["query_text"] == r"什么是分数\/小数"
    assert calls[0]["query_vector"] == [0.0, 1.0]


def test_graphrag_reuses_query_vector_for_local_fusion(monkeypatch):
    class CountingEmbedder:
        def __init__(self):
            self.calls = 0

        def embed(self, texts):
            self.calls += 1
            return [[0.0, 1.0] for _ in texts]

    class OfficialRetriever:
        def search(self, **_kwargs):
            return SimpleNamespace(items=[])

    class LocalFallback:
        def __init__(self, embedder):
            self.embedder = embedder
            self.query_vector = None

        def search(self, query, *, query_vector=None, **_kwargs):
            self.query_vector = query_vector
            if query_vector is None:
                self.embedder.embed([query])
            return []

    monkeypatch.setattr(
        "retrieval.retrievers.make_hybrid_cypher_retriever",
        lambda *args, **kwargs: OfficialRetriever(),
    )
    embedder = CountingEmbedder()
    fallback = LocalFallback(embedder)
    retriever = GraphRAGHybridRetriever(
        SimpleNamespace(driver=object()),  # type: ignore[arg-type]
        embedder,  # type: ignore[arg-type]
        fallback,  # type: ignore[arg-type]
    )

    retriever.search(
        "分数",
        labels=("Concept",),
        filters=RetrievalFilters(subject="数学"),
        top_k=5,
        student_safe=True,
    )

    assert embedder.calls == 1
    assert fallback.query_vector == [0.0, 1.0]


def test_retrieval_backend_failure_is_not_returned_as_no_result():
    store = RecordingStore([RuntimeError("neo4j unavailable")])
    service = RetrievalService(
        store=store,
        settings=RetrievalSettings(hybrid_backend="local"),
        embedder=FakeEmbedder(),
    )

    with pytest.raises(RetrievalBackendUnavailable):
        service.retrieve(RetrievalRequest(question="【分数】在哪一章？"))


def test_graphrag_failure_reports_structured_local_fallback_warning(monkeypatch):
    monkeypatch.setattr(
        "retrieval.retrievers.make_hybrid_cypher_retriever",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("official backend failed")),
    )
    fallback = SimpleNamespace(
        search=lambda *args, **kwargs: [
            EvidenceNode("concept-fraction", "Concept", "分数", 1.0, {})
        ]
    )
    retriever = GraphRAGHybridRetriever(
        SimpleNamespace(driver=object()),  # type: ignore[arg-type]
        FakeEmbedder(),
        fallback,  # type: ignore[arg-type]
    )
    warnings = []

    results = retriever.search(
        "分数",
        labels=("Concept",),
        filters=RetrievalFilters(subject="数学"),
        top_k=5,
        student_safe=True,
        warning_sink=warnings,
    )

    assert [node.id for node in results] == ["concept-fraction"]
    assert warnings == ["GRAPHRAG_FALLBACK_LOCAL"]


def test_embedding_failure_is_backend_unavailable_not_no_result():
    store = RecordingStore([[]])
    service = RetrievalService(
        store=store,
        settings=RetrievalSettings(hybrid_backend="local"),
        embedder=FailingEmbedder(),
    )

    with pytest.raises(RetrievalBackendUnavailable):
        service.retrieve(RetrievalRequest(question="解释【分数】"))


def test_service_exposes_graphrag_local_fallback_warning(monkeypatch):
    monkeypatch.setattr(
        "retrieval.retrievers.make_hybrid_cypher_retriever",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("official backend failed")),
    )
    store = RecordingStore([[], []])
    service = RetrievalService(
        store=store,
        settings=RetrievalSettings(hybrid_backend="graphrag"),
        embedder=FakeEmbedder(),
    )
    service.hybrid.fallback = SimpleNamespace(  # type: ignore[union-attr]
        search=lambda *args, **kwargs: [
            EvidenceNode("concept-fraction", "Concept", "分数", 1.0, {})
        ]
    )

    response = service.retrieve(RetrievalRequest(question="解释【分数】"))

    assert [node.id for node in response.evidence_nodes] == ["concept-fraction"]
    assert response.warnings == ["GRAPHRAG_FALLBACK_LOCAL"]
