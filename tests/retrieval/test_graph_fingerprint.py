from __future__ import annotations

from datetime import datetime, timezone

from eval.retrieval.graph_fingerprint import canonical_value, create_fingerprint, digest_rows


class FakeStore:
    def run_read(self, query: str):
        normalized = " ".join(query.split())
        if normalized.startswith("MATCH (n)"):
            return [
                {
                    "element_id": "4:node:0",
                    "labels": ["Concept"],
                    "properties": {"embedding": [0.1, 0.2], "name": "分数"},
                }
            ]
        if normalized.startswith("MATCH (source)"):
            return [
                {
                    "element_id": "5:relationship:0",
                    "type": "prerequisites_for",
                    "source_element_id": "4:node:0",
                    "target_element_id": "4:node:1",
                    "properties": {},
                }
            ]
        if normalized.startswith("SHOW INDEXES"):
            return [{"name": "concept_id", "state": "ONLINE"}]
        if normalized.startswith("SHOW CONSTRAINTS"):
            return [{"name": "concept_id_unique", "type": "UNIQUENESS"}]
        if normalized.startswith("CALL dbms.components"):
            return [{"name": "Neo4j Kernel", "versions": ["5.26.26"], "edition": "community"}]
        raise AssertionError(f"unexpected query: {normalized}")


def test_canonical_value_is_stable_for_key_order_and_temporal_values() -> None:
    first = {"b": 2, "a": datetime(2026, 8, 2, tzinfo=timezone.utc)}
    second = {"a": datetime(2026, 8, 2, tzinfo=timezone.utc), "b": 2}

    assert canonical_value(first) == canonical_value(second)
    assert digest_rows([first]) == digest_rows([second])


def test_graph_fingerprint_covers_data_schema_and_components() -> None:
    fingerprint = create_fingerprint(FakeStore())  # type: ignore[arg-type]

    assert set(fingerprint["sections"]) == {
        "nodes",
        "relationships",
        "indexes",
        "constraints",
        "components",
    }
    assert fingerprint["sections"]["nodes"]["count"] == 1
    assert fingerprint["sections"]["relationships"]["count"] == 1
    assert len(fingerprint["combined_sha256"]) == 64


def test_graph_fingerprint_changes_when_embedding_changes() -> None:
    original = digest_rows([{"properties": {"embedding": [0.1, 0.2]}}])
    changed = digest_rows([{"properties": {"embedding": [0.1, 0.3]}}])

    assert original["sha256"] != changed["sha256"]
