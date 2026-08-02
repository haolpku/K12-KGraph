import pytest

service_module = pytest.importorskip("retrieval.service")


class FakeRetriever:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def retrieve(self, request):
        self.calls.append(request)
        return self.response


def test_student_response_hides_answers_internal_cypher_and_explanations():
    retriever = FakeRetriever(
        {
            "intent": "exercise_by_concept",
            "entities": [{"id": "concept_division", "label": "除法"}],
            "results": [
                {
                    "id": "exercise_division_1",
                    "question": "12个苹果平均分给3人",
                    "answer": "4个",
                    "explanation": "12 ÷ 3 = 4",
                    "cypher": "MATCH (n) RETURN n",
                }
            ],
            "evidence": [{"type": "path", "nodes": ["concept_division", "exercise_division_1"]}],
        }
    )
    service = service_module.RetrievalService(retriever=retriever)
    response = service.search({"query": "找除法练习", "user_type": "student", "top_k": 5})
    serialized = str(response)
    assert "4个" not in serialized
    assert "12 ÷ 3 = 4" not in serialized
    assert "MATCH" not in serialized
    assert response["evidence"]


def test_teacher_response_may_include_explanations_but_keeps_evidence():
    retriever = FakeRetriever(
        {
            "intent": "exercise_by_concept",
            "entities": [{"id": "concept_division", "label": "除法"}],
            "results": [{"id": "exercise_division_1", "explanation": "12 ÷ 3 = 4"}],
            "evidence": [{"type": "textbook", "book": "三年级下册"}],
        }
    )
    service = service_module.RetrievalService(retriever=retriever)
    response = service.search({"query": "分析除法练习", "user_type": "teacher", "top_k": 5})
    assert "12 ÷ 3 = 4" in str(response)
    assert response["evidence"][0]["type"] == "textbook"


def test_teacher_response_still_hides_internal_vectors_and_search_text():
    retriever = FakeRetriever(
        {
            "intent": "exercise_by_concept",
            "results": [
                {
                    "id": "exercise_division_1",
                    "answer": "4个",
                    "embedding": [0.1, 0.2],
                    "search_text": "学生检索文本",
                    "teacher_search_text": "学生检索文本 4个",
                }
            ],
        }
    )
    service = service_module.RetrievalService(retriever=retriever)
    response = service.search({"query": "分析除法练习", "user_type": "teacher"})
    serialized = str(response)
    assert "4个" in serialized
    assert "embedding" not in serialized
    assert "search_text" not in serialized


def test_search_request_accepts_grade_semester_edition_and_top_k_filters():
    retriever = FakeRetriever({"intent": "concept_lookup", "entities": [], "results": [], "evidence": []})
    service = service_module.RetrievalService(retriever=retriever)
    request = {
        "query": "三年级下册小数",
        "user_type": "student",
        "grade": 3,
        "semester": "下",
        "edition": "人教版",
        "top_k": 3,
    }
    service.search(request)
    assert retriever.calls[0]["grade"] == 3
    assert retriever.calls[0]["semester"] == "下"
    assert retriever.calls[0]["edition"] == "人教版"
    assert retriever.calls[0]["top_k"] == 3
