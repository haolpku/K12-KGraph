import json
import logging

from retrieval import api
from retrieval.observability import (
    LOGGER,
    RequestObservation,
    principal_hash,
    query_fingerprint,
    request_id,
)


def test_api_enables_info_level_audit_logging(monkeypatch):
    monkeypatch.setenv("K12_ENV", "development")
    monkeypatch.setenv("K12_AUTH_MODE", "development")

    api.create_app()

    assert LOGGER.level == logging.INFO


def test_request_id_accepts_uuid_and_replaces_invalid_value():
    valid = "fd8722c3-1ca3-4d9b-93ee-148280ae7531"
    assert request_id(valid) == valid
    assert request_id("not-a-uuid") != "not-a-uuid"


def test_audit_log_contains_hashes_but_not_raw_sensitive_values():
    raw_principal = "student@example.test"
    raw_question = "把12个苹果平均分给3人，答案是什么？"
    records = []

    class CaptureHandler(logging.Handler):
        def emit(self, record):
            records.append(record)

    handler = CaptureHandler()
    LOGGER.addHandler(handler)
    observation = RequestObservation(
        request_id=request_id(None),
        principal_id_hash=principal_hash(raw_principal, "test-hmac-key"),
        role="student",
        query_hash=query_fingerprint(raw_question),
    )
    try:
        with observation:
            observation.finish(
                route="hybrid",
                reason="OK",
                status=200,
                extra={"intents": ["semantic_search"], "top_k": 5},
            )
    finally:
        LOGGER.removeHandler(handler)
    payload = json.loads(records[-1].message)
    serialized = json.dumps(payload, ensure_ascii=False)
    assert raw_principal not in serialized
    assert raw_question not in serialized
    assert payload["route"] == "hybrid"
    assert len(payload["principal_id_hash"]) == 64
    assert len(payload["query_hash"]) == 64
