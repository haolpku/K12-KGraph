"""Neo4j access wrapper with optional dependency loading."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import AbstractContextManager
from threading import Barrier
from typing import Any, Dict, List, Optional

from retrieval.settings import RetrievalSettings


class Neo4jDependencyError(RuntimeError):
    pass


class Neo4jStore(AbstractContextManager["Neo4jStore"]):
    def __init__(self, settings: RetrievalSettings, *, readonly: bool = False) -> None:
        self.settings = settings
        self.readonly = readonly
        self._driver = None

    @classmethod
    def connect(cls, settings: RetrievalSettings, *, readonly: bool = False) -> "Neo4jStore":
        store = cls(settings, readonly=readonly)
        store.open()
        return store

    def open(self) -> None:
        try:
            from neo4j import GraphDatabase
        except ImportError as exc:
            raise Neo4jDependencyError("Install neo4j to use the retrieval service: pip install neo4j") from exc
        user = self.settings.readonly_user if self.readonly and self.settings.readonly_user else self.settings.neo4j_user
        password = self.settings.readonly_password if self.readonly and self.settings.readonly_password else self.settings.neo4j_password
        if not password:
            raise Neo4jDependencyError("NEO4J_PASSWORD is required to connect to Neo4j")
        driver_timeout = max(0.1, float(self.settings.query_timeout_seconds))
        self._driver = GraphDatabase.driver(
            self.settings.neo4j_uri,
            auth=(user, password),
            connection_timeout=driver_timeout,
            max_transaction_retry_time=driver_timeout,
        )

    def close(self) -> None:
        if self._driver is not None:
            self._driver.close()
            self._driver = None

    @property
    def driver(self) -> Any:
        if self._driver is None:
            self.open()
        assert self._driver is not None
        return self._driver

    def __exit__(self, *exc: object) -> None:
        self.close()

    def verify_connectivity(self) -> Dict[str, Any]:
        if self._driver is None:
            self.open()
        assert self._driver is not None
        self._driver.verify_connectivity()
        rows = self.run_read("CALL dbms.components() YIELD name, versions, edition RETURN name, versions, edition")
        return rows[0] if rows else {}

    def warm_read_pool(self, concurrency: int) -> None:
        workers = max(1, min(int(concurrency), 32))
        start = Barrier(workers)

        def warm(_index: int) -> None:
            start.wait()
            self.run_read("RETURN 1 AS value")

        with ThreadPoolExecutor(max_workers=workers) as executor:
            list(executor.map(warm, range(workers)))

    def run_read(self, cypher: str, params: Optional[Dict[str, Any]] = None, *, timeout: Optional[float] = None) -> List[Dict[str, Any]]:
        return self._run_transaction(cypher, params=params, timeout=timeout, write=False)

    def run_write(self, cypher: str, params: Optional[Dict[str, Any]] = None, *, timeout: Optional[float] = None) -> List[Dict[str, Any]]:
        if self.readonly:
            raise PermissionError("readonly Neo4jStore cannot run writes")
        return self._run_transaction(cypher, params=params, timeout=timeout, write=True)

    def _run_transaction(
        self,
        cypher: str,
        *,
        params: Optional[Dict[str, Any]],
        timeout: Optional[float],
        write: bool,
    ) -> List[Dict[str, Any]]:
        if self._driver is None:
            self.open()
        assert self._driver is not None
        config: Dict[str, Any] = {}
        if self.settings.neo4j_database:
            config["database"] = self.settings.neo4j_database
        with self._driver.session(**config) as session:
            execute = session.execute_write if write else session.execute_read

            def collect(transaction: Any) -> List[Dict[str, Any]]:
                result = transaction.run(cypher, params or {})
                return [dict(record) for record in result]

            work = collect
            if timeout is not None:
                from neo4j import unit_of_work

                work = unit_of_work(timeout=float(timeout))(collect)
            return execute(work)
