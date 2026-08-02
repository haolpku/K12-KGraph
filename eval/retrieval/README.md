# Retrieval Evaluation Assets

This directory contains deterministic evaluation assets for the Neo4j primary-school math retrieval system.

The file `retrieval_goldens.jsonl` is a synthetic, reviewable seed set generated from curriculum-style templates. It is **not** a subject-matter-expert-approved benchmark. Treat it as executable coverage for retrieval behavior until teachers or curriculum experts replace or approve the expected IDs.

## Files

- `generate_goldens.py` builds the JSONL golden set.
- `generate_eval_set.py` builds a seed set using real IDs from an enriched graph.
- `validate_goldens.py` validates schema, IDs, and minimum dataset size.
- `evaluate.py` runs live Neo4j retrieval methods and writes comparable result files.
- `metrics.py` computes Recall@5, MRR, nDCG@10, average/P50/P95 latency,
  and failure rate for result files.
- `retrieval_goldens.jsonl` contains at least 200 labeled queries.
- `primary_math_eval_240.jsonl` contains 240 graph-derived queries spanning
  grades 1–6, both semesters, all 12 PEP books, and six retrieval intents.
- `research_e2e_cases.jsonl` contains 60 synthetic HTTP contract cases for
  multi-intent routing, deterministic no-result filters, and response safety.
- `run_research_e2e.py` runs those cases against the live V1 API and writes
  `research_e2e_results.json`.

## Result File Format

Each retrieval run is a JSONL file with one row per query:

```json
{"query_id": "q001", "retrieved_ids": ["concept_fraction_basic"], "latency_ms": 18.2, "error": null}
```

Compare methods with:

```bash
python eval/retrieval/metrics.py \
  --gold eval/retrieval/retrieval_goldens.jsonl \
  --results cypher=cypher.jsonl fulltext=fulltext.jsonl vector=vector.jsonl hybrid=hybrid.jsonl
```

Run against a configured Neo4j instance:

```bash
PYTHONPATH=src python eval/retrieval/evaluate.py \
  --gold eval/retrieval/retrieval_goldens.jsonl \
  --methods cypher fulltext vector hybrid \
  --output-dir eval/retrieval/results
```

The checked-in primary set was generated with:

```bash
PYTHONPATH=src python eval/retrieval/generate_eval_set.py \
  --graph data/retrieval/primary_math_graph.json \
  --output eval/retrieval/primary_math_eval_240.jsonl \
  --limit 240 \
  --subject 数学 --stage 小学 --edition 人教版
```

Run the operational router and all component methods:

```bash
PYTHONPATH=src python eval/retrieval/evaluate.py \
  --gold eval/retrieval/primary_math_eval_240.jsonl \
  --methods auto cypher fulltext vector hybrid \
  --output-dir eval/retrieval/results/primary_math
```

Use `--intents concept_detail location similar_exercises` when comparing
full-text, vector, and hybrid entity recall without mixing in relationship
questions that are intentionally routed to Cypher. The 240 expected-ID rows
are graph-derived, not independent human annotations; a subject-matter expert
must review them before they are used for teaching acceptance.

Run the HTTP contract E2E suite after the isolated stack is healthy:

```bash
PYTHONPATH=src python eval/retrieval/run_research_e2e.py \
  --url http://127.0.0.1:18000/v1/retrieval/search
```

The runner performs a `/health` preflight, requires evidence for positive
retrieval cases, rejects backend-unavailable warnings, checks expected intent
order and reason codes, and recursively checks forbidden response fields.
