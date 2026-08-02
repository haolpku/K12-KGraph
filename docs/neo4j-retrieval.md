# Neo4j Retrieval Deployment

This guide documents the deployable Neo4j 5.26 retrieval stack for K12-KGraph. It covers the operational pieces added under `docker/` and does not assume real credentials are committed.

Current repository state:

- Existing code builds JSON knowledge graphs and benchmark data.
- `src/retrieval/` provides data preparation, Neo4j import, index creation, deterministic Cypher, hybrid retrieval, routing, Text2Cypher safety, and FastAPI.
- `docker/docker-compose.neo4j-retrieval.yml` starts Neo4j 5.26 and `retrieval.api:app`.

## Files

| File | Purpose |
| --- | --- |
| `config/retrieval.env.example` | Safe environment template with placeholder credentials. |
| `docker/docker-compose.neo4j-retrieval.yml` | Neo4j 5.26 plus retrieval API service. |
| `docker/retrieval-api.Dockerfile` | Python API image with Neo4j, Neo4j GraphRAG, FastAPI, and uvicorn. |
| `docker/cypher/retrieval_schema.cypher` | Constraints, property indexes, full-text indexes, and vector indexes. |
| `docker/scripts/import_kg_json.py` | Imports K12-KGraph JSON into Neo4j and builds `search_text`. |
| `docker/scripts/generate_vectors.py` | Generates `embedding` vectors for `Concept`, `Skill`, and `Exercise`. |
| `docker/scripts/init_retrieval_schema.sh` | Applies the Cypher schema with `cypher-shell`. |
| `docker/scripts/healthcheck_api.py` | Container healthcheck for `GET /health`. |
| `docker/scripts/wait_for_neo4j.py` | Blocks API startup until Neo4j accepts Bolt queries. |

## Prerequisites

- Docker and Docker Compose.
- A K12-KGraph JSON file. The demo file is `demo/kg/math_7a_rjb.json`.
- Python packages inside the API image are installed by `docker/retrieval-api.Dockerfile`.

For local host execution, create the repository environment from the lockfile:

```bash
uv sync --frozen
```

`pyproject.toml` declares direct runtime and development dependencies, and `uv.lock` pins the complete environment. The generated `requirements.txt` and `requirements-dev.txt` files remain compatibility exports for Docker and legacy tooling; do not edit them directly.

## Configure

Create a private env file:

```bash
cp config/retrieval.env.example config/retrieval.env
```

Edit `config/retrieval.env` and set at least:

| Variable | Description |
| --- | --- |
| `NEO4J_PASSWORD` | Neo4j password. Use a non-example value. |
| `RETRIEVAL_API_APP` | ASGI app import path, for example `retrieval.api:app`. |
| `KG_JSON_PATH` | Graph JSON file to import. |
| `KG_SUBJECT` | Subject metadata attached during import. |
| `KG_STAGE` | Stage metadata attached during import. |
| `KG_EDITION` | Textbook edition metadata attached during import. |
| `K12_RETRIEVAL_EMBEDDING_PROVIDER` | `fastembed`、`openai`或仅测试用的`hash`。 |
| `EMBEDDING_MODEL` | Embedding模型；示例默认`BAAI/bge-small-zh-v1.5`。 |
| `EMBEDDING_DIMENSIONS` | 向量维度。现有索引固定为`512`。 |
| `DEEPSEEK_API_KEY` | 可选；用于结构化路由和教师分析计划，不生成自由Cypher。 |
| `NEO4J_READONLY_USER` | Optional Enterprise/Aura read-only principal for the API. |
| `K12_RETRIEVAL_HYBRID_BACKEND` | `graphrag` uses Neo4j GraphRAG `HybridCypherRetriever`; `local` selects the driver-based fallback. |
| `K12_CORS_ORIGINS` | 允许的前端Origin，逗号分隔。 |

Do not put real passwords, API keys, or production hostnames in `config/retrieval.env.example`.

## Start Neo4j and API

Validate the Compose file first:

```bash
docker compose --env-file config/retrieval.env -f docker/docker-compose.neo4j-retrieval.yml config
```

Start the stack:

```bash
docker compose --env-file config/retrieval.env -f docker/docker-compose.neo4j-retrieval.yml up -d --build
```

Check container status:

```bash
docker compose --env-file config/retrieval.env -f docker/docker-compose.neo4j-retrieval.yml ps
```

隔离E2E示例把Neo4j Browser绑定到`http://localhost:17474`，API绑定到
`http://localhost:18000`。使用非标准宿主机端口可避免已有Neo4j Browser
或本地服务用旧凭据自动连接测试库。

The canonical search endpoint is:

```bash
curl -fsS http://localhost:18000/v1/retrieval/search \
  -H 'Content-Type: application/json' \
  -d '{"question":"哪些题考察分数？","grade":"3","semester":"下册","edition":"人教版","book_id":"math_3b_rjb","section_id":"math_3b_rjb_ch2_s1","exercise_type":"选择题","difficulty":2,"top_k":5}'
```

`POST /retrieve` is retained as a compatibility alias.

V1请求只接受`question`、`grade`、`semester`、`edition`、`book_id`、
`section_id`、`exercise_type`、`difficulty`和`top_k`。`user_type`、
`include_answers`和`route`属于安全控制字段，V1会以HTTP 422拒绝。

## Initialize Schema

Apply constraints and indexes after Neo4j is healthy:

```bash
./docker/scripts/init_retrieval_schema.sh config/retrieval.env
```

The Cypher creates:

- Unique `id` constraints for `Concept`, `Skill`, `Exercise`, `Book`, `Chapter`, and `Section`.
- Name and scope indexes for `Concept`, `Skill`, and `Exercise`.
- Full-text indexes for `Concept`, `Skill`, and `Exercise`.
- Vector indexes for `Concept`, `Skill`, and `Exercise` on the `embedding` property.

The vector indexes use dimension `512`. If you change `EMBEDDING_MODEL` to a model with another dimension, update `docker/cypher/retrieval_schema.cypher` before applying it.

The full-text indexes use Neo4j's `cjk` analyzer. On the checked-in 200-query
demo-derived set, `cjk` kept Recall@5 at `0.9625` while improving full-text MRR
from `0.8985` to `0.9296` and nDCG@10 from `0.9169` to `0.9418`. These are
technical demo figures, not primary-mathematics acceptance results.

`CREATE ... IF NOT EXISTS` does not change an existing full-text index's
analyzer. For an existing deployment, schedule a maintenance window, drop only
the three named full-text indexes, and rerun schema initialization:

```cypher
DROP INDEX concept_fulltext IF EXISTS;
DROP INDEX skill_fulltext IF EXISTS;
DROP INDEX exercise_fulltext IF EXISTS;
```

The Python-native equivalent, useful outside Docker, is:

```bash
PYTHONPATH=src python -m retrieval.indexes --print-only
PYTHONPATH=src python -m retrieval.indexes
```

## Import KG JSON

Import the demo graph:

```bash
set -a
source config/retrieval.env
set +a

python docker/scripts/import_kg_json.py \
  --kg-json "$KG_JSON_PATH" \
  --subject "$KG_SUBJECT" \
  --stage "$KG_STAGE" \
  --edition "$KG_EDITION"
```

The importer accepts either:

- A single JSON file with top-level `nodes` and `edges` lists.
- A directory containing `nodes.json` and `edges.json`.

Import behavior:

- It merges nodes by stable `id`.
- It preserves whitelisted labels and relation types from the K12-KGraph schema.
- It expands aggregated `target_name_to_ids` edges such as `tests_concept` and `tests_skill`.
- It writes retrieval scope fields such as `subject`, `stage`, `edition`, `book_id`, `grade`, and `semester` when they can be inferred.
- It writes `search_text` separately for each node type.
- For `Exercise`, `search_text` uses `name`, `stem`, `type`, and `difficulty`; it excludes `answer` and `analysis`.

## Generate Vectors

Generate embeddings for retrievable nodes:

```bash
set -a
source config/retrieval.env
set +a

python docker/scripts/generate_vectors.py
```

For a small smoke test, limit each label:

```bash
python docker/scripts/generate_vectors.py --limit 10
```

The script updates nodes that have `search_text` and do not already have `embedding`.

The Python-native preparation and import path for the released primary
mathematics data is:

```bash
PYTHONPATH=src python -m retrieval.data_prep \
  --input ../data/K12-KGraph/K12-KGraph/subject_specific_KG/math.json \
  --output data/retrieval/primary_math_graph.json \
  --baseline-output data/retrieval/primary_math_baseline.json \
  --subject 数学 \
  --stage 小学 \
  --embed

PYTHONPATH=src python -m retrieval.import_neo4j \
  --input data/retrieval/primary_math_graph.json \
  --create-indexes
```

For an offline smoke test only, add `--offline-hash-embeddings`. Hash vectors
are not semantic and must not be used for production search.

## Retrieval Architecture

- Parameterized Cypher handles concept details, textbook locations, prerequisite/successor paths, and exercises by concept or skill.
- The default `graphrag` backend runs the official Neo4j
  `HybridCypherRetriever` per node label, applies scope filters in its
  retrieval query, and then expands graph relationships.
- For Neo4j 5.26, GraphRAG over-retrieves candidates before applying scope
  filters. If GraphRAG is unavailable or rejects a Lucene query, the service
  falls back to the local full-text/vector reciprocal-rank fusion retriever.
- 教师分析只接收严格`TeacherAnalysisPlanV1`，再由静态编译器生成参数化
  Cypher；模型自由生成的Cypher不进入V1 API运行时。
- Student responses remove answers, analyses, explanations, and internal Cypher.

Neo4j 5.26 applies grade/edition filtering after vector candidates are
retrieved. The service therefore over-fetches vector candidates before
filtering. Measure recall on the target data before increasing the multiplier
or upgrading to a release with vector index filtering.

## Retrieval Evaluation

The checked-in primary-mathematics set contains 240 deterministic,
graph-derived queries across all 12 PEP books:

```bash
python eval/retrieval/validate_goldens.py eval/retrieval/primary_math_eval_240.jsonl

PYTHONPATH=src python eval/retrieval/evaluate.py \
  --gold eval/retrieval/primary_math_eval_240.jsonl \
  --output-dir eval/retrieval/results/primary_math \
  --methods auto cypher fulltext vector hybrid

# Fair entity-retrieval comparison: 120 concept/location/similar-exercise rows
PYTHONPATH=src python eval/retrieval/evaluate.py \
  --gold eval/retrieval/primary_math_eval_240.jsonl \
  --output-dir eval/retrieval/results/primary_math_entity \
  --methods fulltext vector hybrid \
  --intents concept_detail location similar_exercises

# Older deterministic synthetic seed
python eval/retrieval/validate_goldens.py eval/retrieval/retrieval_goldens.jsonl
python eval/retrieval/metrics.py --help
```

Both sets are explicitly marked `synthetic_seed_not_sme_reviewed`. A primary
mathematics subject-matter expert must review and correct the 240-row set before
the metrics can be presented as human-validated teaching acceptance results.
The measured primary-data results are recorded in
`docs/retrieval-final-report.md`.

没有DeepSeek Key时，结构化模型回退不可用，但高置信规则和规则多意图仍可
工作。教师分析没有可信教师权限或计划生成器时保持关闭。V1不执行模型自由
Cypher。

## Health Checks

Neo4j容器健康检查使用无凭据HTTP就绪探测，避免在Bolt端口刚开放、系统库尚未
完全就绪时触发认证锁定。

The API container healthcheck calls:

```bash
python /app/docker/scripts/healthcheck_api.py
```

That script expects `GET /health` to return HTTP 2xx. If the body is JSON and has a `status` field, valid values are `ok` or `healthy`.

Host-side smoke checks:

```bash
curl -fsS http://localhost:18000/health
docker compose --env-file config/retrieval.env -f docker/docker-compose.neo4j-retrieval.yml logs --tail=80 neo4j
```

## Backup and Restore

Stop write traffic before a filesystem-level backup:

```bash
docker compose --env-file config/retrieval.env -f docker/docker-compose.neo4j-retrieval.yml stop retrieval-api
```

Create a Neo4j dump from inside the container:

```bash
docker compose --env-file config/retrieval.env -f docker/docker-compose.neo4j-retrieval.yml exec neo4j \
  neo4j-admin database dump neo4j --to-path=/var/lib/neo4j/import/backups --overwrite-destination=true
```

Copy the dump to the host:

```bash
docker compose --env-file config/retrieval.env -f docker/docker-compose.neo4j-retrieval.yml cp \
  neo4j:/var/lib/neo4j/import/backups ./backups
```

Restore into an empty database only after confirming the target volume can be replaced. Do not run destructive restore commands against a database that contains data you need to keep.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| Neo4j is unhealthy | Run `docker compose --env-file config/retrieval.env -f docker/docker-compose.neo4j-retrieval.yml logs neo4j`. Confirm `NEO4J_PASSWORD` is set and at least 8 characters. |
| API exits immediately | Confirm `RETRIEVAL_API_APP` points to an importable ASGI app and that it exposes `GET /health`. |
| Schema init cannot connect | Confirm `cypher-shell` is installed on the host or run the command inside the Neo4j container. |
| Vector index dimension error | Match `EMBEDDING_DIMENSION`, the FastEmbed model output dimension, and `vector.dimensions` in `retrieval_schema.cypher`. |
| Student search exposes answers | Inspect imported `Exercise.search_text`; it must not contain `answer` or `analysis`. |
| Full-text recall is weak for Chinese aliases | Add aliases to `properties.aliases` before import or regenerate `search_text`; then rebuild the full-text index if needed. |

## Stop

Stop containers without deleting data:

```bash
docker compose --env-file config/retrieval.env -f docker/docker-compose.neo4j-retrieval.yml down
```

Delete containers and Neo4j volumes:

```bash
docker compose --env-file config/retrieval.env -f docker/docker-compose.neo4j-retrieval.yml down -v
```

The second command deletes the local Neo4j database volume.
