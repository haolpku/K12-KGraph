#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ENV_FILE="${1:-${ROOT_DIR}/config/retrieval.env}"
SCHEMA_FILE="${ROOT_DIR}/docker/cypher/retrieval_schema.cypher"

if [[ -f "${ENV_FILE}" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "${ENV_FILE}"
  set +a
fi

: "${NEO4J_HOST:=localhost}"
: "${NEO4J_PORT:=7687}"
: "${NEO4J_USER:=neo4j}"
: "${NEO4J_PASSWORD:?Set NEO4J_PASSWORD in ${ENV_FILE}}"
: "${NEO4J_DATABASE:=neo4j}"

cypher-shell \
  -a "bolt://${NEO4J_HOST}:${NEO4J_PORT}" \
  -u "${NEO4J_USER}" \
  -p "${NEO4J_PASSWORD}" \
  -d "${NEO4J_DATABASE}" \
  -f "${SCHEMA_FILE}"
