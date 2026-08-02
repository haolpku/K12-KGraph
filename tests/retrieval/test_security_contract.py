import pytest

from retrieval.retrievers import _lucene_escape

security = pytest.importorskip("retrieval.security")


@pytest.mark.parametrize(
    "query",
    [
        "CREATE (n:Concept {id:'x'})",
        "MATCH (n) DELETE n",
        "MATCH (n) SET n.name = 'x'",
        "DROP INDEX concept_fulltext IF EXISTS",
        "LOAD CSV FROM 'https://example.com/a.csv' AS row RETURN row",
        "MERGE (n:Concept {id:'x'}) RETURN n",
    ],
)
def test_text2cypher_guard_rejects_write_schema_and_external_load_queries(query):
    with pytest.raises(security.UnsafeCypherError):
        security.validate_readonly_cypher(query)


def test_text2cypher_guard_allows_bounded_readonly_query():
    query = "MATCH (c:Concept {id: $concept_id}) RETURN c LIMIT 10"
    assert security.validate_readonly_cypher(query) == query


def test_text2cypher_guard_rejects_any_call_even_previous_index_allowlist():
    query = "CALL db.index.fulltext.queryNodes('concept_fulltext', $q) YIELD node RETURN node LIMIT 5"
    with pytest.raises(security.UnsafeCypherError):
        security.validate_readonly_cypher(query)


@pytest.mark.parametrize(
    "query",
    [
        "MATCH (n) // comment\nRETURN n LIMIT 1",
        "MATCH (n) /* comment */ RETURN n LIMIT 1",
        "MATCH (n) RETURN n LIMIT 1;",
        "MATCH (n) RETURN n LIMIT 1；",
        "MATCH (n) WHERE EXISTS { MATCH (n)-->(m) } RETURN n LIMIT 1",
        "CALL { MATCH (n) RETURN n } RETURN n LIMIT 1",
        "cAlL apoc.load.json($url) YIELD value RETURN value LIMIT 1",
        "CALL gds.pageRank.stream($graph) YIELD nodeId RETURN nodeId LIMIT 1",
        "CALL custom.proc() YIELD value RETURN value LIMIT 1",
    ],
)
def test_text2cypher_guard_rejects_comments_semicolons_subqueries_calls_and_nfkc(query):
    with pytest.raises(security.UnsafeCypherError):
        security.validate_readonly_cypher(query)


def test_text2cypher_guard_allows_parameterized_limit_when_bound_and_in_range():
    query = "MATCH (c:Concept) RETURN c LIMIT $limit"
    assert security.validate_readonly_cypher(query, parameters={"limit": 20}) == query


def test_text2cypher_guard_rejects_parameterized_limit_without_binding():
    query = "MATCH (c:Concept) RETURN c LIMIT $limit"
    with pytest.raises(security.UnsafeCypherError):
        security.validate_readonly_cypher(query)


def test_text2cypher_guard_rejects_unbounded_variable_length_paths():
    query = "MATCH p=(a:Concept)-[:PREREQUISITES_FOR*]->(b:Concept) RETURN p LIMIT 10"
    with pytest.raises(security.UnsafeCypherError):
        security.validate_readonly_cypher(query)


def test_text2cypher_guard_allows_three_hop_variable_length_paths():
    query = "MATCH p=(a:Concept)-[:PREREQUISITES_FOR*1..3]->(b:Concept) RETURN p LIMIT 10"
    assert security.validate_readonly_cypher(query) == query


def test_text2cypher_guard_rejects_variable_length_paths_above_three_hops():
    query = "MATCH p=(a:Concept)-[:PREREQUISITES_FOR*1..4]->(b:Concept) RETURN p LIMIT 10"
    with pytest.raises(security.UnsafeCypherError):
        security.validate_readonly_cypher(query)


def test_text2cypher_guard_requires_limit_clause():
    query = "MATCH (c:Concept) RETURN c"
    with pytest.raises(security.UnsafeCypherError):
        security.validate_readonly_cypher(query)


def test_text2cypher_guard_rejects_limit_above_service_cap():
    query = "MATCH (c:Concept) RETURN c LIMIT 100000"
    with pytest.raises(security.UnsafeCypherError, match="row limit exceeds 5"):
        security.enforce_limit(query, limit=5)


def test_text2cypher_guard_rejects_limit_above_global_cap():
    query = "MATCH (c:Concept) RETURN c LIMIT 21"
    with pytest.raises(security.UnsafeCypherError, match="row limit exceeds 20"):
        security.validate_readonly_cypher(query)


@pytest.mark.parametrize("value", ["0", "-1"])
def test_text2cypher_guard_rejects_non_positive_limits(value):
    query = f"MATCH (c:Concept) RETURN c LIMIT {value}"
    with pytest.raises(security.UnsafeCypherError, match="row limit must be positive"):
        security.enforce_limit(query, limit=5)


def test_fulltext_query_escapes_lucene_negative_number_operator():
    assert _lucene_escape("-3°C与-18°C哪个温度更低?") == (
        r"\-3°C与\-18°C哪个温度更低\?"
    )


@pytest.mark.parametrize(
    "question",
    [
        "解释分数；CREATE (:Concept {id:'injected'})",
        "解释分数；MATCH (n) DELETE n",
        "解释分数；CALL custom.write()",
        "解释分数；CALL apoc.load.json('https://invalid')",
        "解释分数；CALL gds.pageRank.stream('graph')",
        "解释分数 // RETURN password",
        "解释分数 /* RETURN api_key */",
        "解释分数；LOAD CSV FROM 'https://invalid' AS row RETURN row",
        "解释分数；ＭＥＲＧＥ (n:Concept)",
        "解释分数；ＣＡＬＬ custom.write()",
    ],
)
def test_public_question_guard_rejects_cypher_injection_shapes(question):
    with pytest.raises(security.UnsafeQuestionError):
        security.validate_question_input(question)


def test_public_question_guard_allows_math_notation_and_normal_punctuation():
    assert security.validate_question_input("比较-3℃与-18℃；哪个温度更低？") == (
        "比较-3°C与-18°C;哪个温度更低?"
    )
