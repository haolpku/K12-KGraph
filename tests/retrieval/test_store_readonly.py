from __future__ import annotations

import uuid
from typing import Any

import pytest

from retrieval.settings import RetrievalSettings
from retrieval.store import Neo4jStore


class FakeTransaction:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, dict[str, Any]]] = []

    def run(self, query: Any, parameters: dict[str, Any]):
        self.calls.append((query, parameters))
        return [{"value": 1}]


class FakeSession:
    def __init__(self) -> None:
        self.transaction = FakeTransaction()
        self.execute_read_calls = 0
        self.execute_write_calls = 0

    def __enter__(self) -> "FakeSession":
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def execute_read(self, work):
        self.execute_read_calls += 1
        return work(self.transaction)

    def execute_write(self, work):
        self.execute_write_calls += 1
        return work(self.transaction)


class FakeDriver:
    def __init__(self) -> None:
        self.sessions: list[tuple[dict[str, Any], FakeSession]] = []

    def session(self, **config: Any) -> FakeSession:
        session = FakeSession()
        self.sessions.append((config, session))
        return session


def make_store(*, readonly: bool) -> tuple[Neo4jStore, FakeDriver]:
    store = Neo4jStore(
        RetrievalSettings(neo4j_password=uuid.uuid4().hex, neo4j_database="neo4j"),
        readonly=readonly,
    )
    driver = FakeDriver()
    store._driver = driver
    return store, driver


def test_run_read_uses_driver_read_transaction() -> None:
    store, driver = make_store(readonly=True)

    cypher = "MATCH (n) RETURN count(n) AS value"
    rows = store.run_read(cypher, {"limit": 1}, timeout=5)

    config, session = driver.sessions[0]
    assert config == {"database": "neo4j"}
    assert session.execute_read_calls == 1
    assert session.execute_write_calls == 0
    assert rows == [{"value": 1}]
    assert session.transaction.calls[0][0] == cypher
    assert session.transaction.calls[0][1] == {"limit": 1}


def test_readonly_store_rejects_run_write_before_opening_session() -> None:
    store, driver = make_store(readonly=True)

    with pytest.raises(PermissionError, match="readonly Neo4jStore"):
        store.run_write("CREATE (:Concept {id: $id})", {"id": "forbidden"})

    assert driver.sessions == []


def test_non_readonly_store_uses_driver_write_transaction() -> None:
    store, driver = make_store(readonly=False)

    rows = store.run_write("UNWIND $rows AS row RETURN row", {"rows": [1]})

    _, session = driver.sessions[0]
    assert session.execute_write_calls == 1
    assert session.execute_read_calls == 0
    assert rows == [{"value": 1}]


def test_driver_connection_and_retry_windows_follow_query_timeout(monkeypatch) -> None:
    calls = []

    class OpenedDriver:
        def close(self):
            return None

    def driver(uri, **kwargs):
        calls.append((uri, kwargs))
        return OpenedDriver()

    monkeypatch.setattr("neo4j.GraphDatabase.driver", driver)
    settings = RetrievalSettings(
        neo4j_uri="bolt://127.0.0.1:1",
        neo4j_password=uuid.uuid4().hex,
        query_timeout_seconds=0.5,
    )

    store = Neo4jStore(settings, readonly=True)
    store.open()

    assert calls == [
        (
            "bolt://127.0.0.1:1",
            {
                "auth": ("neo4j", settings.neo4j_password),
                "connection_timeout": 0.5,
                "max_transaction_retry_time": 0.5,
            },
        )
    ]


def test_warm_read_pool_opens_requested_read_transactions() -> None:
    store, driver = make_store(readonly=True)

    store.warm_read_pool(8)

    assert len(driver.sessions) == 8
    assert all(session.execute_read_calls == 1 for _, session in driver.sessions)
    assert all(session.execute_write_calls == 0 for _, session in driver.sessions)
    assert all(
        session.transaction.calls == [("RETURN 1 AS value", {})]
        for _, session in driver.sessions
    )
