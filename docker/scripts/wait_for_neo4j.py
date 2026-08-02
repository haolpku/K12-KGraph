#!/usr/bin/env python3
"""Wait until Neo4j accepts a simple Bolt query."""

from __future__ import annotations

import os
import sys
import time


def main() -> int:
    from neo4j import GraphDatabase

    uri = os.getenv("NEO4J_URI", "bolt://neo4j:7687")
    user = os.getenv("NEO4J_USER", "neo4j")
    password = os.getenv("NEO4J_PASSWORD", "")
    database = os.getenv("NEO4J_DATABASE", "neo4j")
    deadline = time.monotonic() + int(os.getenv("NEO4J_WAIT_SECONDS", "120"))

    last_error = ""
    while time.monotonic() < deadline:
        try:
            with GraphDatabase.driver(uri, auth=(user, password)) as driver:
                driver.verify_connectivity()
                with driver.session(database=database) as session:
                    session.run("RETURN 1 AS ok").consume()
            return 0
        except Exception as exc:  # noqa: BLE001 - CLI health output needs the raw error.
            last_error = str(exc)
            time.sleep(2)

    print(f"Neo4j did not become ready: {last_error}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
