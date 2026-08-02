from eval.retrieval.generate_eval_set import build_examples
from retrieval.cypher import CypherTemplates
from retrieval.data_prep import enrich_graph, filter_graph, normalize_node
from retrieval.import_neo4j import _edge_payload
from retrieval.models import RetrievalFilters
from retrieval.settings import RetrievalSettings


class RecordingStore:
    def __init__(self):
        self.calls = []

    def run_read(self, query, parameters=None, *, timeout=None):
        self.calls.append((query, parameters or {}, timeout))
        return []


def test_stable_book_id_overrides_inconsistent_source_scope_metadata():
    node = normalize_node(
        {
            "id": "math_4b_rjb",
            "label": "Book",
            "name": "四年级下册",
            "properties": {
                "subject": "数学",
                "grade": "四年级下册",
                "publisher": "人教版",
            },
        }
    )

    assert node["properties"]["grade"] == "4"
    assert node["properties"]["semester"] == "下册"
    assert node["properties"]["edition"] == "人教版"
    assert node["properties"]["book_id"] == "math_4b_rjb"


def test_filter_graph_keeps_only_primary_math_and_valid_aggregated_targets():
    nodes = [
        {"id": "math_1a_rjb_cpt1", "label": "Concept", "name": "数数"},
        {"id": "math_1a_rjb_skl1", "label": "Skill", "name": "逐一计数"},
        {"id": "math_7a_rjb_cpt1", "label": "Concept", "name": "有理数"},
    ]
    edges = [
        {
            "source": "math_1a_rjb_cpt1",
            "type": "relates_to",
            "target": "math_1a_rjb_skl1",
        },
        {
            "source": "math_1a_rjb_cpt1",
            "type": "tests_skill",
            "target_name_to_ids": [
                {"target": "math_1a_rjb_skl1", "target_name": "逐一计数"},
                {"target": "math_7a_rjb_cpt1", "target_name": "有理数"},
            ],
        },
    ]
    selected_nodes, selected_edges = filter_graph(
        nodes,
        edges,
        subject="数学",
        stage="小学",
    )
    assert {node["id"] for node in selected_nodes} == {
        "math_1a_rjb_cpt1",
        "math_1a_rjb_skl1",
    }
    assert selected_edges[1]["target_name_to_ids"] == [
        {"target": "math_1a_rjb_skl1", "target_name": "逐一计数"}
    ]


def test_chapter_location_is_not_mislabeled_as_section():
    graph = enrich_graph(
        [
            {"id": "math_4b_rjb_cpt1", "label": "Concept", "name": "四则运算"},
            {"id": "math_4b_rjb_ch1", "label": "Chapter", "name": "四则运算"},
        ],
        [
            {
                "source": "math_4b_rjb_cpt1",
                "target": "math_4b_rjb_ch1",
                "type": "appears_in",
            }
        ],
        embed=False,
        settings=RetrievalSettings(),
        offline_hash=False,
    )
    properties = graph["nodes"][0]["properties"]
    assert properties["chapter_id"] == "math_4b_rjb_ch1"
    assert properties["chapter_ids"] == ["math_4b_rjb_ch1"]
    assert "section_id" not in properties
    assert "section_ids" not in properties


def test_textbook_location_query_supports_direct_chapter_edges():
    store = RecordingStore()
    CypherTemplates(store).textbook_locations("math_4b_rjb_cpt1", top_k=5)  # type: ignore[arg-type]
    query = store.calls[0][0]
    assert "direct_chapter:Chapter" in query
    assert "coalesce(section_chapter, direct_chapter)" in query


def test_prerequisite_query_traverses_concepts_and_skills():
    store = RecordingStore()
    CypherTemplates(store).prerequisites(  # type: ignore[arg-type]
        "分数除法的意义",
        RetrievalFilters(subject="数学", stage="小学"),
        top_k=5,
        depth=3,
        student_safe=True,
    )
    query = store.calls[0][0]
    assert "WHERE (n:Concept OR n:Skill)" in query
    assert "WHERE (m:Concept OR m:Skill)" in query
    assert "prerequisites_for*1..3" in query


def test_nested_edge_evidence_is_preserved_as_json_for_neo4j():
    payload = _edge_payload(
        {
            "source": "math_1a_rjb_cpt1",
            "target": "math_1a_rjb_cpt2",
            "properties": {
                "evidence": {"page": "P3", "text": "先数后比"},
                "confidence": 0.9,
            },
        }
    )
    assert payload["properties"] == {
        "evidence": '{"page": "P3", "text": "先数后比"}',
        "confidence": 0.9,
    }


def test_generated_primary_evaluation_is_stratified_across_intents():
    graph = {
        "nodes": [
            {
                "id": "math_4b_rjb_cpt1",
                "label": "Concept",
                "name": "加法",
                "properties": {
                    "subject": "数学",
                    "stage": "小学",
                    "grade": "4",
                    "semester": "下册",
                    "edition": "人教版",
                    "book_id": "math_4b_rjb",
                },
            },
            {
                "id": "math_4b_rjb_cpt2",
                "label": "Concept",
                "name": "减法",
                "properties": {
                    "subject": "数学",
                    "stage": "小学",
                    "grade": "4",
                    "semester": "下册",
                    "edition": "人教版",
                    "book_id": "math_4b_rjb",
                },
            },
            {
                "id": "math_4b_rjb_exe1",
                "label": "Exercise",
                "name": "计算加法",
                "properties": {
                    "subject": "数学",
                    "stage": "小学",
                    "grade": "4",
                    "semester": "下册",
                    "edition": "人教版",
                    "book_id": "math_4b_rjb",
                    "type": "计算题",
                    "difficulty": 1,
                },
            },
        ],
        "edges": [
            {
                "source": "math_4b_rjb_cpt1",
                "target": "math_4b_rjb_cpt2",
                "type": "prerequisites_for",
            },
            {
                "source": "math_4b_rjb_exe1",
                "target": "math_4b_rjb_cpt1",
                "type": "tests_concept",
            },
        ],
    }
    rows = build_examples(graph, 6)
    assert {row["intent"] for row in rows} == {
        "concept_detail",
        "location",
        "prerequisites",
        "successors",
        "exercises_for",
        "similar_exercises",
    }
    assert all(row["filters"]["book_id"] == "math_4b_rjb" for row in rows)


def test_same_named_concepts_in_different_books_do_not_merge_exercise_goldens():
    graph = {
        "nodes": [
            {
                "id": "math_1b_rjb_cpt1",
                "label": "Concept",
                "name": "退位减法",
                "properties": {"grade": "1", "semester": "下册", "book_id": "math_1b_rjb"},
            },
            {
                "id": "math_2a_rjb_cpt1",
                "label": "Concept",
                "name": "退位减法",
                "properties": {"grade": "2", "semester": "上册", "book_id": "math_2a_rjb"},
            },
            {
                "id": "math_1b_rjb_exe1",
                "label": "Exercise",
                "name": "20以内退位减法",
                "properties": {"grade": "1", "semester": "下册", "book_id": "math_1b_rjb"},
            },
            {
                "id": "math_2a_rjb_exe1",
                "label": "Exercise",
                "name": "100以内退位减法",
                "properties": {"grade": "2", "semester": "上册", "book_id": "math_2a_rjb"},
            },
        ],
        "edges": [
            {"source": "math_1b_rjb_exe1", "target": "math_1b_rjb_cpt1", "type": "tests_concept"},
            {"source": "math_2a_rjb_exe1", "target": "math_2a_rjb_cpt1", "type": "tests_concept"},
        ],
    }

    rows = [
        row
        for row in build_examples(graph, 20)
        if row["intent"] == "exercises_for"
    ]

    assert len(rows) == 2
    assert len({row["query_id"] for row in rows}) == 2
    assert {
        (row["filters"]["book_id"], tuple(row["expected_ids"]))
        for row in rows
    } == {
        ("math_1b_rjb", ("math_1b_rjb_exe1",)),
        ("math_2a_rjb", ("math_2a_rjb_exe1",)),
    }
