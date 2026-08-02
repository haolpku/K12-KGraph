#!/usr/bin/env python3
"""Create a credential-free canonical fingerprint of a Neo4j graph and schema."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from datetime import date, datetime, time
from pathlib import Path
from typing import Any, Iterable, Mapping

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from retrieval.settings import RetrievalSettings  # noqa: E402
from retrieval.store import Neo4jStore  # noqa: E402

NODE_QUERY = """
MATCH (n)
RETURN elementId(n) AS element_id, labels(n) AS labels, properties(n) AS properties
ORDER BY element_id
"""

RELATIONSHIP_QUERY = """
MATCH (source)-[relationship]->(target)
RETURN elementId(relationship) AS element_id,
       type(relationship) AS type,
       elementId(source) AS source_element_id,
       elementId(target) AS target_element_id,
       properties(relationship) AS properties
ORDER BY element_id
"""

INDEX_QUERY = """
SHOW INDEXES
YIELD name, type, entityType, labelsOrTypes, properties, state,
      indexProvider, owningConstraint, options
RETURN name, type, entityType, labelsOrTypes, properties, state,
       indexProvider, owningConstraint, options
ORDER BY name
"""

CONSTRAINT_QUERY = """
SHOW CONSTRAINTS
YIELD name, type, entityType, labelsOrTypes, properties, ownedIndex
RETURN name, type, entityType, labelsOrTypes, properties, ownedIndex
ORDER BY name
"""

COMPONENT_QUERY = """
CALL dbms.components()
YIELD name, versions, edition
RETURN name, versions, edition
ORDER BY name
"""


def canonical_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if math.isfinite(value):
            return value
        return {"type": "float", "value": str(value)}
    if isinstance(value, bytes):
        return {"type": "bytes", "sha256": hashlib.sha256(value).hexdigest()}
    if isinstance(value, Mapping):
        return {
            str(key): canonical_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [canonical_value(item) for item in value]
    if isinstance(value, (date, datetime, time)):
        return {"type": type(value).__name__, "value": value.isoformat()}
    if hasattr(value, "iso_format"):
        return {"type": type(value).__name__, "value": value.iso_format()}
    return {"type": type(value).__name__, "value": str(value)}


def digest_rows(rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    hasher = hashlib.sha256()
    count = 0
    for row in rows:
        encoded = json.dumps(
            canonical_value(row),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        hasher.update(len(encoded).to_bytes(8, "big"))
        hasher.update(encoded)
        count += 1
    return {"count": count, "sha256": hasher.hexdigest()}


def create_fingerprint(store: Neo4jStore) -> dict[str, Any]:
    sections = {
        "nodes": digest_rows(store.run_read(NODE_QUERY)),
        "relationships": digest_rows(store.run_read(RELATIONSHIP_QUERY)),
        "indexes": digest_rows(store.run_read(INDEX_QUERY)),
        "constraints": digest_rows(store.run_read(CONSTRAINT_QUERY)),
        "components": digest_rows(store.run_read(COMPONENT_QUERY)),
    }
    combined = hashlib.sha256(
        json.dumps(sections, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return {
        "format_version": 1,
        "sections": sections,
        "combined_sha256": combined,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    settings = RetrievalSettings.from_env()
    with Neo4jStore.connect(settings, readonly=True) as store:
        fingerprint = create_fingerprint(store)
    rendered = json.dumps(fingerprint, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
