import json
from pathlib import Path

from eval.retrieval.generate_goldens import build_rows
from eval.retrieval.validate_goldens import validate_rows

GOLDENS_PATH = Path(__file__).resolve().parents[2] / "eval" / "retrieval" / "retrieval_goldens.jsonl"


def test_generated_golden_set_contains_at_least_200_queries():
    assert len(build_rows()) >= 200


def test_checked_in_golden_set_is_schema_valid():
    rows = [json.loads(line) for line in GOLDENS_PATH.read_text(encoding="utf-8").splitlines()]
    validate_rows(rows, min_rows=200)


def test_checked_in_golden_set_marks_not_subject_matter_expert_reviewed():
    rows = [json.loads(line) for line in GOLDENS_PATH.read_text(encoding="utf-8").splitlines()]
    assert {row["notes"] for row in rows} == {"synthetic_seed_not_sme_reviewed"}
