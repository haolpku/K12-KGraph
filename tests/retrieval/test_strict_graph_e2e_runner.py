from __future__ import annotations

from eval.retrieval.generate_strict_e2e_cases import FORBIDDEN_FIELDS, build_strict_cases
from eval.retrieval.run_strict_graph_e2e import (
    evaluate_gold_case,
    hard_quality_metrics,
    metric_error,
    payload_from_gold,
    ranked_ids,
    response_observation,
)


def test_strict_cases_add_at_least_forty_schema_security_and_boundary_checks() -> None:
    rows = build_strict_cases()
    by_id = {row["case_id"]: row for row in rows}

    assert len(rows) >= 40
    assert {"schema", "security", "no_result", "valid_boundary", "clarification"} <= {
        row["category"] for row in rows
    }
    assert len({row["case_id"] for row in rows}) == len(rows)
    assert all(set(FORBIDDEN_FIELDS) <= set(row["forbidden_response_fields"]) for row in rows)
    assert by_id["strict_valid_trimmed"]["expected_intents"] == ["concept_detail"]
    assert by_id["strict_valid_fullwidth"]["expected_intents"] == ["concept_detail"]


def test_gold_payload_only_contains_v1_public_filters() -> None:
    payload = payload_from_gold(
        {
            "query": "解释小数",
            "filters": {
                "subject": "数学",
                "stage": "小学",
                "grade": "3",
                "semester": "下册",
                "book_id": "math_3b_rjb",
            },
        }
    )

    assert payload == {
        "question": "解释小数",
        "top_k": 10,
        "grade": "3",
        "semester": "下册",
        "book_id": "math_3b_rjb",
    }


def test_ranked_ids_deduplicates_evidence_entities_paths_and_locations() -> None:
    body = {
        "evidence_nodes": [{"id": "a"}],
        "entities": [{"id": "a"}, {"id": "b"}],
        "relation_paths": [{"nodes": [{"id": "c"}]}],
        "textbook_locations": [{"node_id": "d"}],
    }

    assert ranked_ids(body) == ["a", "b", "c", "d"]


def test_gold_evaluator_rejects_wrong_path_and_student_leak() -> None:
    row = {
        "intent": "prerequisites",
        "expected_ids": ["target"],
    }
    body = {
        "request_id": "fd8722c3-1ca3-4d9b-93ee-148280ae7531",
        "intents": ["prerequisites"],
        "route": "cypher_template",
        "reason_code": "OK",
        "needs_clarification": False,
        "warnings": [],
        "evidence_nodes": [
            {"id": "target", "label": "Concept", "properties": {"answer": "secret"}}
        ],
        "entities": [],
        "relation_paths": [
            {
                "relationships": ["prerequisites_for"] * 4,
                "nodes": [{"id": "target"}],
            }
        ],
        "textbook_locations": [],
    }

    p0, p1 = evaluate_gold_case(row, 200, body)

    assert p0 == ["forbidden_fields=['answer']"]
    assert "invalid_path_length=['prerequisites_for', 'prerequisites_for', 'prerequisites_for', 'prerequisites_for']" in p1


def test_metric_error_only_marks_transport_or_backend_failures() -> None:
    assert metric_error(200, {"warnings": []}) is None
    assert metric_error(503, {}) == "http_status=503"
    assert metric_error(
        200,
        {"warnings": ["retrieval backend unavailable"]},
    ) == "retrieval_backend_unavailable"


def test_location_evaluator_accepts_v1_location_without_node_id() -> None:
    row = {
        "intent": "location",
        "expected_ids": ["math_1a_rjb_cpt1"],
        "filters": {"book_id": "math_1a_rjb"},
    }
    body = {
        "request_id": "fd8722c3-1ca3-4d9b-93ee-148280ae7531",
        "intents": ["location"],
        "route": "cypher_template",
        "reason_code": "OK",
        "needs_clarification": False,
        "warnings": [],
        "entities": [{"id": "math_1a_rjb_cpt1"}],
        "evidence_nodes": [{"id": "math_1a_rjb_cpt1", "label": "Concept"}],
        "relation_paths": [],
        "textbook_locations": [
            {
                "book_id": "math_1a_rjb",
                "chapter_id": "math_1a_rjb_ch1",
                "chapter_name": "准备课",
            }
        ],
    }

    assert evaluate_gold_case(row, 200, body) == ([], [])


def test_path_evaluator_checks_graph_direction_from_returned_node_order() -> None:
    row = {
        "intent": "prerequisites",
        "expected_ids": ["prerequisite"],
        "expected_evidence_types": ["path", "textbook"],
        "filters": {"book_id": "math_1a_rjb"},
    }
    body = {
        "request_id": "fd8722c3-1ca3-4d9b-93ee-148280ae7531",
        "intents": ["prerequisites"],
        "route": "cypher_template",
        "reason_code": "OK",
        "needs_clarification": False,
        "warnings": [],
        "entities": [{"id": "target"}],
        "evidence_nodes": [{"id": "prerequisite"}, {"id": "target"}],
        "relation_paths": [
            {
                "start_id": "target",
                "end_id": "prerequisite",
                "relationships": ["prerequisites_for"],
                "nodes": [{"id": "prerequisite"}, {"id": "target"}],
            }
        ],
        "textbook_locations": [
            {"book_id": "math_1a_rjb", "chapter_id": "math_1a_rjb_ch1"}
        ],
    }

    assert evaluate_gold_case(
        row,
        200,
        body,
        graph_edges={("prerequisite", "prerequisites_for", "target")},
    ) == ([], [])
    _, p1 = evaluate_gold_case(
        row,
        200,
        body,
        graph_edges={("target", "prerequisites_for", "prerequisite")},
    )
    assert p1 == ["invalid_path_direction_or_endpoints"]


def test_response_observation_records_evidence_without_node_properties() -> None:
    body = {
        "request_id": "fd8722c3-1ca3-4d9b-93ee-148280ae7531",
        "intents": ["concept_detail"],
        "route": "hybrid",
        "evidence_nodes": [
            {
                "id": "concept-fraction",
                "label": "Concept",
                "score": 0.9,
                "properties": {"definition": "hidden from result artifact"},
            }
        ],
        "relation_paths": [],
        "textbook_locations": [{"book_id": "math_5a_rjb"}],
        "warnings": ["GRAPHRAG_FALLBACK_LOCAL"],
    }

    observation = response_observation(body)

    assert observation["request_id"] == body["request_id"]
    assert observation["evidence_nodes"] == [
        {"id": "concept-fraction", "label": "Concept", "score": 0.9}
    ]
    assert observation["fallback_used"] is True
    assert "properties" not in str(observation)


def test_hard_quality_metrics_exposes_acceptance_rates() -> None:
    gold_rows = [
        {"query_id": "g1", "expected_evidence_types": ["textbook"]},
        {"query_id": "g2", "expected_evidence_types": ["path", "textbook"]},
    ]
    results = [
        {
            "query_id": "g1",
            "category": "gold",
            "has_textbook_evidence": True,
            "path_correct": None,
            "fallback_used": False,
            "leaked_fields": [],
            "p0": [],
            "p1": [],
        },
        {
            "query_id": "g2",
            "category": "gold",
            "has_textbook_evidence": True,
            "path_correct": True,
            "fallback_used": True,
            "leaked_fields": [],
            "p0": [],
            "p1": [],
        },
        {
            "case_id": "attack",
            "category": "security",
            "expected_status": 403,
            "status": 403,
            "fallback_used": False,
            "leaked_fields": [],
            "p0": [],
            "p1": [],
        },
    ]

    metrics = hard_quality_metrics(gold_rows, results)

    assert metrics == {
        "fixed_e2e_failures": 0,
        "textbook_evidence_coverage": 1.0,
        "path_correctness": 1.0,
        "student_leak_rate": 0.0,
        "illegal_request_acceptance_rate": 0.0,
        "fallback_rate": 0.5,
    }
