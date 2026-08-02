import pytest

router_module = pytest.importorskip("retrieval.router")


def test_router_sends_prerequisite_questions_to_cypher_templates():
    router = router_module.RetrievalRouter()
    route = router.route("学习分数前要会什么", user_type="student")
    assert route.method == "cypher"
    assert route.intent == "prerequisite"


def test_router_sends_similar_exercise_questions_to_hybrid_retrieval():
    router = router_module.RetrievalRouter()
    route = router.route("找和平均分苹果意思差不多的题", user_type="student")
    assert route.method == "hybrid"
    assert route.intent == "similar_exercise"


def test_router_sends_teacher_analysis_questions_to_text2cypher():
    router = router_module.RetrievalRouter()
    route = router.route("教师端分析分数相关易错题", user_type="teacher")
    assert route.method == "text2cypher"
    assert route.intent == "teacher_analysis"


def test_router_does_not_send_student_queries_to_text2cypher():
    router = router_module.RetrievalRouter()
    route = router.route("分析分数相关易错题", user_type="student")
    assert route.method != "text2cypher"
