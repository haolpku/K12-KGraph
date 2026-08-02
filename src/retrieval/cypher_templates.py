"""Backward-compatible import surface for Cypher retrieval templates."""

from retrieval.cypher import CypherTemplates

__all__ = ["CypherTemplates", "find_prerequisites", "find_exercises_for_concept", "find_textbook_locations"]


def _depth(max_depth: int) -> int:
    if max_depth > 3:
        raise ValueError("prerequisite path depth must not exceed 3")
    return max(1, int(max_depth))


def find_prerequisites(session, *, concept_id: str, max_depth: int = 3, limit: int = 10):
    depth = _depth(max_depth)
    query = (
        "MATCH path=(pre:Concept)-[:prerequisites_for*1..%d]->(c:Concept {id: $concept_id}) "
        "RETURN pre, path LIMIT $limit"
    ) % depth
    return session.run(query, {"concept_id": concept_id, "limit": int(limit)})


def find_exercises_for_concept(session, *, concept_id: str, filters=None, limit: int = 5):
    filters = dict(filters or {})
    parameters = {"concept_id": concept_id, "limit": int(limit)}
    conditions = ["c.id = $concept_id"]
    for key in ("grade", "semester", "edition", "difficulty", "type"):
        if key in filters:
            param = "exercise_type" if key == "type" else key
            prop = "type" if key == "type" else key
            parameters[param] = filters[key]
            conditions.append(f"e.{prop} = ${param}")
    query = (
        "MATCH (e:Exercise)-[:tests_concept|tests_skill]->(c:Concept) "
        "WHERE " + " AND ".join(conditions) + " "
        "RETURN e.id AS id, e.name AS name, properties(e) AS properties LIMIT $limit"
    )
    return session.run(query, parameters)


def find_textbook_locations(session, *, concept_id: str, limit: int = 5):
    query = (
        "MATCH (n {id: $concept_id})-[:appears_in]->(s:Section) "
        "OPTIONAL MATCH (s)-[:is_part_of]->(c:Chapter) "
        "OPTIONAL MATCH (c)-[:is_part_of]->(b:Book) "
        "RETURN b AS Book, c AS Chapter, s AS Section LIMIT $limit"
    )
    return session.run(query, {"concept_id": concept_id, "limit": int(limit)})
