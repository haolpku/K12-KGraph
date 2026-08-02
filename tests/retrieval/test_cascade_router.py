from types import SimpleNamespace

from retrieval.models import RetrievalRequestV1
from retrieval.router import route_cascade


def test_cascade_routes_deterministic_relation_to_cypher():
    decision = route_cascade(
        RetrievalRequestV1(question="学习分数前要会什么？")
    )
    assert decision.intents == ("prerequisites",)
    assert decision.routes == ("cypher_template",)
    assert decision.confidence == 0.95


def test_cascade_routes_similar_exercise_to_hybrid():
    decision = route_cascade(
        RetrievalRequestV1(question="找一道和平均分苹果相似的题")
    )
    assert decision.intents == ("similar_exercises",)
    assert decision.routes == ("hybrid",)
    assert decision.confidence == 0.90


def test_cascade_uses_structured_classifier_for_multi_intent():
    calls = []

    def classify(question, filters):
        calls.append((question, filters))
        return SimpleNamespace(
            intents=("prerequisites", "exercises_for"),
            entities=(SimpleNamespace(text="分数除法"),),
            needs_clarification=False,
        )

    decision = route_cascade(
        RetrievalRequestV1(
            question="学习分数除法前要会什么，并给两道练习题？",
            grade="四年级",
        ),
        classifier=classify,
    )
    assert decision.intents == ("prerequisites", "exercises_for")
    assert decision.routes == ("cypher_template", "cypher_template")
    assert decision.entity_text == "分数除法"
    assert decision.confidence == 0.80
    assert len(calls) == 1


def test_cascade_fails_to_clarification_for_invalid_classifier_output():
    def classify(_question, _filters):
        return SimpleNamespace(
            intents=("concept_detail", "location", "exercises_for"),
            entities=(),
            needs_clarification=False,
        )

    decision = route_cascade(
        RetrievalRequestV1(question="小数在哪里并给题"),
        classifier=classify,
    )
    assert decision.needs_clarification
    assert decision.reason_code == "ROUTER_INVALID_OUTPUT"


def test_multi_intent_order_follows_the_question():
    decision = route_cascade(
        RetrievalRequestV1(question="分数在哪一章，同时找一道相似题？")
    )
    assert decision.intents == ("location", "similar_exercises")


def test_entity_name_tokens_do_not_create_false_secondary_intents():
    decision = route_cascade(
        RetrievalRequestV1(
            question="学习【数轴上正数和负数的位置】之前需要哪些前置知识？"
        )
    )

    assert decision.intents == ("prerequisites",)
    assert decision.entity_text == "数轴上正数和负数的位置"


def test_intent_tokens_outside_entity_quotes_still_form_multi_intent():
    decision = route_cascade(
        RetrievalRequestV1(question="【小数点位置确定规则】在哪一章，同时找一道相似题？")
    )

    assert decision.intents == ("location", "similar_exercises")


def test_structured_classifier_exception_requires_clarification():
    def classify(_question, _filters):
        raise TimeoutError("structured model unavailable")

    decision = route_cascade(
        RetrievalRequestV1(question="分数在哪一章，同时找一道相似题？"),
        classifier=classify,
    )

    assert decision.needs_clarification is True
    assert decision.reason_code == "ROUTER_INVALID_OUTPUT"
