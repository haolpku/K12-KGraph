import pytest

templates = pytest.importorskip("retrieval.cypher_templates")


class FakeSession:
    def __init__(self):
        self.calls = []

    def run(self, query, parameters=None, **kwargs):
        self.calls.append((query, parameters or {}, kwargs))
        return []


def test_prerequisite_query_limits_path_depth_to_three_hops():
    session = FakeSession()
    templates.find_prerequisites(session, concept_id="concept_fraction_basic", max_depth=3, limit=10)
    query, parameters, _ = session.calls[0]
    assert "prerequisites_for*1..3" in query
    assert parameters["concept_id"] == "concept_fraction_basic"
    assert parameters["limit"] == 10


def test_prerequisite_query_rejects_depth_greater_than_three():
    session = FakeSession()
    with pytest.raises(ValueError):
        templates.find_prerequisites(session, concept_id="concept_fraction_basic", max_depth=4)


def test_exercise_query_uses_parameters_for_filters():
    session = FakeSession()
    templates.find_exercises_for_concept(
        session,
        concept_id="concept_multiplication",
        filters={"grade": 3, "edition": "人教版", "difficulty": "基础"},
        limit=5,
    )
    query, parameters, _ = session.calls[0]
    assert "$concept_id" in query
    assert "$grade" in query
    assert "$edition" in query
    assert "$difficulty" in query
    assert "concept_multiplication" not in query
    assert parameters["limit"] == 5


def test_textbook_location_returns_book_chapter_and_section_evidence():
    session = FakeSession()
    templates.find_textbook_locations(session, concept_id="concept_decimal_basic", limit=3)
    query, parameters, _ = session.calls[0]
    assert "Book" in query
    assert "Chapter" in query
    assert "Section" in query
    assert parameters["concept_id"] == "concept_decimal_basic"
