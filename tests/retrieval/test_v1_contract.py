from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from retrieval import api
from retrieval.models import RetrievalRequestV1


@pytest.mark.parametrize("field", ["route", "user_type", "include_answers"])
def test_v1_request_rejects_security_control_fields(field):
    with pytest.raises(ValidationError):
        RetrievalRequestV1.model_validate(
            {"question": "解释小数", field: "teacher"}
        )


def test_v1_request_rejects_empty_filters_and_out_of_range_top_k():
    with pytest.raises(ValidationError):
        RetrievalRequestV1(question="解释小数", edition="")
    with pytest.raises(ValidationError):
        RetrievalRequestV1(question="解释小数", top_k=21)


def test_v1_api_returns_canonical_shape_in_development(monkeypatch):
    monkeypatch.delenv("NEO4J_PASSWORD", raising=False)
    monkeypatch.setenv("K12_ENV", "development")
    monkeypatch.setenv("K12_AUTH_MODE", "development")
    monkeypatch.setenv("K12_ROUTER_MODE", "cascade")
    monkeypatch.setenv("K12_RETRIEVAL_EMBEDDING_PROVIDER", "hash")
    monkeypatch.setenv("K12_RETRIEVAL_HYBRID_BACKEND", "local")
    monkeypatch.setattr("retrieval.store.Neo4jStore.run_read", lambda *args, **kwargs: [])
    with TestClient(api.create_app()) as client:
        response = client.post(
            "/v1/retrieval/search",
            json={"question": "解释小数", "top_k": 3},
            headers={"X-Request-ID": "fd8722c3-1ca3-4d9b-93ee-148280ae7531"},
        )
    assert response.status_code == 200
    body = response.json()
    assert body["request_id"] == "fd8722c3-1ca3-4d9b-93ee-148280ae7531"
    assert body["intents"] == ["semantic_search"]
    assert body["route"] == "hybrid"
    assert body["reason_code"] in {"OK", "NO_RESULT"}
    assert "cypher" not in body


def test_legacy_api_ignores_forged_role_and_returns_deprecation_headers(
    monkeypatch,
):
    monkeypatch.delenv("NEO4J_PASSWORD", raising=False)
    monkeypatch.setenv("K12_ENV", "development")
    monkeypatch.setenv("K12_AUTH_MODE", "development")
    monkeypatch.setenv("K12_RETRIEVAL_EMBEDDING_PROVIDER", "hash")
    monkeypatch.setenv("K12_RETRIEVAL_HYBRID_BACKEND", "local")
    monkeypatch.setattr("retrieval.store.Neo4jStore.run_read", lambda *args, **kwargs: [])
    with TestClient(api.create_app()) as client:
        response = client.post(
            "/retrieve",
            json={
                "question": "找除法练习",
                "user_type": "teacher",
                "include_answers": True,
                "route": "text2cypher",
            },
        )
    assert response.status_code == 200
    assert response.headers["deprecation"] == "true"
    body = response.json()
    assert "cypher" not in body
    assert "answer" not in response.text.lower()


def test_v1_cors_preflight_uses_configured_origin(monkeypatch):
    monkeypatch.delenv("NEO4J_PASSWORD", raising=False)
    monkeypatch.setenv("K12_ENV", "development")
    monkeypatch.setenv("K12_AUTH_MODE", "development")
    monkeypatch.setenv("K12_ROUTER_MODE", "cascade")
    monkeypatch.setenv("K12_CORS_ORIGINS", "http://localhost:5173")
    with TestClient(api.create_app()) as client:
        response = client.options(
            "/v1/retrieval/search",
            headers={
                "Origin": "http://localhost:5173",
                "Access-Control-Request-Method": "POST",
            },
        )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://localhost:5173"


def test_v1_rejects_cypher_injection_without_entering_retrieval(monkeypatch):
    monkeypatch.delenv("NEO4J_PASSWORD", raising=False)
    monkeypatch.setenv("K12_ENV", "development")
    monkeypatch.setenv("K12_AUTH_MODE", "development")
    monkeypatch.setenv("K12_ROUTER_MODE", "cascade")
    with TestClient(api.create_app()) as client:
        response = client.post(
            "/v1/retrieval/search",
            json={"question": "解释分数；ＣＡＬＬ apoc.load.json('https://invalid')"},
        )

    assert response.status_code == 403
    assert response.json() == {"detail": {"reason_code": "UNSAFE_QUERY_REJECTED"}}
