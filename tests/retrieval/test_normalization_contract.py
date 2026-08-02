import pytest

normalization = pytest.importorskip("retrieval.normalization")


def test_build_concept_search_text_includes_name_alias_definition_formula_and_examples():
    concept = {
        "name": "长方形面积",
        "aliases": ["矩形面积"],
        "definition": "长方形所占平面的大小",
        "formula": "长 × 宽",
        "examples": ["求教室地面的面积"],
        "answer": "不应出现",
    }
    text = normalization.build_search_text("Concept", concept, audience="student")
    assert "长方形面积" in text
    assert "矩形面积" in text
    assert "长 × 宽" in text
    assert "求教室地面的面积" in text
    assert "不应出现" not in text


def test_build_exercise_search_text_excludes_answer_and_explanation_for_students():
    exercise = {
        "question": "把12个苹果平均分给3人，每人几个？",
        "type": "应用题",
        "difficulty": "基础",
        "answer": "4个",
        "explanation": "12 ÷ 3 = 4",
    }
    text = normalization.build_search_text("Exercise", exercise, audience="student")
    assert "把12个苹果平均分给3人" in text
    assert "应用题" in text
    assert "基础" in text
    assert "4个" not in text
    assert "12 ÷ 3 = 4" not in text


def test_build_exercise_search_text_allows_answer_for_teacher_audience():
    exercise = {"question": "6×7等于多少？", "answer": "42", "explanation": "乘法口诀"}
    text = normalization.build_search_text("Exercise", exercise, audience="teacher")
    assert "42" in text
    assert "乘法口诀" in text


def test_normalize_math_symbols_canonicalizes_common_fullwidth_symbols():
    assert normalization.normalize_math_symbols("６×７＝４２") == "6 × 7 = 42"
