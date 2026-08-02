import pytest
from pydantic import ValidationError

teacher_analysis = pytest.importorskip("retrieval.teacher_analysis")


def test_teacher_analysis_plan_is_strict_and_forbids_unknown_fields():
    with pytest.raises(ValidationError):
        teacher_analysis.TeacherAnalysisPlanV1.model_validate(
            {
                "operation": "list",
                "entity_type": "Concept",
                "limit": "5",
                "filters": {"grade": "三年级", "unknown": "x"},
            }
        )


@pytest.mark.parametrize(
    "payload",
    [
        {"operation": "delete", "entity_type": "Concept", "limit": 5},
        {"operation": "list", "entity_type": "User", "limit": 5},
        {"operation": "list", "entity_type": "Concept", "group_by": "subject", "limit": 5},
        {"operation": "list", "entity_type": "Concept", "limit": 0},
        {"operation": "list", "entity_type": "Concept", "limit": 21},
        {"operation": "distribution", "entity_type": "Exercise", "group_by": "none", "limit": 5},
        {"operation": "count", "entity_type": "Exercise", "group_by": "grade", "limit": 5},
    ],
)
def test_teacher_analysis_plan_rejects_out_of_contract_shapes(payload):
    with pytest.raises(ValidationError):
        teacher_analysis.TeacherAnalysisPlanV1.model_validate(payload)


def test_teacher_analysis_compiles_list_to_static_parameterized_cypher():
    compiled = teacher_analysis.compile_teacher_analysis_plan(
        {
            "operation": "list",
            "entity_type": "Exercise",
            "filters": {
                "grade": "三年级",
                "semester": "上册",
                "edition": "人教版",
                "book_id": "book_3a",
                "section_id": "section_fraction",
                "exercise_type": "应用题",
                "difficulty": 2,
            },
            "limit": 7,
        }
    )

    assert compiled.cypher.startswith("MATCH (n:Exercise)")
    assert "n.type = $exercise_type" in compiled.cypher
    assert "应用题" not in compiled.cypher
    assert "LIMIT $limit" in compiled.cypher
    assert compiled.parameters == {
        "limit": 7,
        "grade": "三年级",
        "semester": "上册",
        "edition": "人教版",
        "book_id": "book_3a",
        "section_id": "section_fraction",
        "exercise_type": "应用题",
        "difficulty": 2,
    }


def test_teacher_analysis_compiles_count_without_model_generated_fragments():
    compiled = teacher_analysis.compile_teacher_analysis_plan(
        {"operation": "count", "entity_type": "Concept", "filters": {"grade": "三年级"}, "limit": 1}
    )
    assert compiled.cypher == (
        "MATCH (n:Concept)\n"
        "WHERE n.grade = $grade\n"
        "RETURN count(DISTINCT n) AS value\n"
        "LIMIT $limit"
    )
    assert compiled.parameters == {"limit": 1, "grade": "三年级"}


@pytest.mark.parametrize("group_by, expected", [("grade", "n.grade"), ("semester", "n.semester"), ("edition", "n.edition")])
def test_teacher_analysis_compiles_scalar_distributions(group_by, expected):
    compiled = teacher_analysis.compile_teacher_analysis_plan(
        {"operation": "distribution", "entity_type": "Exercise", "group_by": group_by, "limit": 10}
    )
    assert f"WITH {expected} AS key, count(DISTINCT n) AS value" in compiled.cypher
    assert compiled.parameters == {"limit": 10}


@pytest.mark.parametrize("group_by, expected", [("book", "book"), ("chapter", "chapter")])
def test_teacher_analysis_compiles_book_and_chapter_distributions_with_fixed_optional_matches(group_by, expected):
    compiled = teacher_analysis.compile_teacher_analysis_plan(
        {"operation": "distribution", "entity_type": "Skill", "group_by": group_by, "limit": 5}
    )
    assert "OPTIONAL MATCH (n)-[:appears_in]->(section:Section)" in compiled.cypher
    assert "OPTIONAL MATCH (section)-[:is_part_of]->(chapter:Chapter)" in compiled.cypher
    assert "OPTIONAL MATCH (chapter)-[:is_part_of]->(book:Book)" in compiled.cypher
    assert expected in compiled.cypher
    assert "CALL" not in compiled.cypher
    assert ";" not in compiled.cypher
