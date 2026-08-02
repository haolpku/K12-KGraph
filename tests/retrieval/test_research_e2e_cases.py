from eval.retrieval.generate_research_e2e_cases import build_cases
from eval.retrieval.run_research_e2e import evaluate_case, forbidden_fields


def test_research_e2e_cases_cover_multi_intent_no_result_and_security():
    rows = build_cases()
    assert len(rows) == 60
    assert {row["category"] for row in rows} == {
        "multi_intent",
        "no_result",
        "security",
    }
    assert all(row["source"] == "synthetic_not_sme_reviewed" for row in rows)
    assert len({row["case_id"] for row in rows}) == len(rows)
    no_result = [row for row in rows if row["category"] == "no_result"]
    assert all(row["request"]["book_id"].startswith("missing-book-") for row in no_result)


def test_e2e_result_evaluator_checks_intent_reason_and_leaks():
    case = {
        "expected_status": 200,
        "expected_intents": ["prerequisites"],
        "expected_reason_code": "OK",
        "forbidden_response_fields": ["answer", "embedding"],
    }
    body = {
        "intents": ["prerequisites"],
        "reason_code": "OK",
        "evidence_nodes": [{"properties": {"answer": "secret"}}],
    }
    failures = evaluate_case(case, 200, body)
    assert failures == ["forbidden_fields=['answer']"]
    assert forbidden_fields(body, {"answer"}) == {"answer"}


def test_e2e_result_evaluator_checks_non_200_reason_code():
    case = {
        "expected_status": 403,
        "expected_error_reason_code": "UNSAFE_QUERY_REJECTED",
    }

    assert evaluate_case(
        case,
        403,
        {"detail": {"reason_code": "UNSAFE_QUERY_REJECTED"}},
    ) == []
    assert evaluate_case(
        case,
        403,
        {"detail": {"reason_code": "FORBIDDEN_TOOL"}},
    ) == [
        "error_reason_code='FORBIDDEN_TOOL', expected='UNSAFE_QUERY_REJECTED'"
    ]
