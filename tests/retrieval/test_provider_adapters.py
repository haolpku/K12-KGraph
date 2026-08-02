from __future__ import annotations

import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from retrieval.embeddings import DashScopeEmbedding, FastEmbedder
from retrieval.intent_classifier import DeepSeekIntentClassifier, IntentDecisionV1


class ProviderError(Exception):
    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class NetworkError(Exception):
    pass


class FakeEmbeddingsEndpoint:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class FakeEmbeddingClient:
    def __init__(self, responses):
        self.embeddings = FakeEmbeddingsEndpoint(responses)


class FakeChatCompletions:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class FakeChatClient:
    def __init__(self, responses):
        self.chat = SimpleNamespace(completions=FakeChatCompletions(responses))


def embedding_response(vectors):
    return SimpleNamespace(data=[SimpleNamespace(embedding=vector) for vector in vectors])


def chat_response(content: str):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])


def test_fastembedder_public_class_is_still_available():
    embedder = FastEmbedder("BAAI/bge-small-zh-v1.5", 512)
    assert embedder.model_name == "BAAI/bge-small-zh-v1.5"
    assert embedder.dimension == 512


def test_fastembedder_limits_onnx_threads_and_parallelism(monkeypatch):
    calls = {}

    class FakeTextEmbedding:
        def __init__(self, **kwargs):
            calls["init"] = kwargs

        def embed(self, texts, **kwargs):
            calls["embed"] = {"texts": list(texts), **kwargs}
            return [[0.1, 0.2]]

    monkeypatch.setitem(
        sys.modules,
        "fastembed",
        SimpleNamespace(TextEmbedding=FakeTextEmbedding),
    )

    embedder = FastEmbedder("test-model", 2)

    assert embedder.embed(["分数"]) == [[0.1, 0.2]]
    assert calls["init"]["threads"] == 1
    assert calls["embed"]["parallel"] == 1


def test_fastembedder_serializes_inference_to_avoid_cpu_oversubscription():
    state_lock = threading.Lock()
    active = 0
    max_active = 0

    class FakeModel:
        def embed(self, _texts, **_kwargs):
            nonlocal active, max_active
            with state_lock:
                active += 1
                max_active = max(max_active, active)
            time.sleep(0.02)
            with state_lock:
                active -= 1
            return [[0.1, 0.2]]

    embedder = FastEmbedder("test-model", 2)
    embedder._model = FakeModel()

    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(lambda index: embedder.embed([str(index)]), range(4)))

    assert max_active == 1


def test_dashscope_embedding_chunks_batches_to_ten_and_validates_dimensions():
    client = FakeEmbeddingClient(
        [
            embedding_response([[1, 2, 3]] * 10),
            embedding_response([[4, 5, 6], [7, 8, 9]]),
        ]
    )
    embedder = DashScopeEmbedding("text-embedding-v4", 3, client=client, timeout=7)

    vectors = embedder.embed([f"text-{index}" for index in range(12)])

    assert len(vectors) == 12
    assert [len(call["input"]) for call in client.embeddings.calls] == [10, 2]
    assert all(call["model"] == "text-embedding-v4" for call in client.embeddings.calls)
    assert all(call["timeout"] == 7 for call in client.embeddings.calls)
    assert all(call["dimensions"] == 3 for call in client.embeddings.calls)
    assert all(call["encoding_format"] == "float" for call in client.embeddings.calls)
    assert all(len(vector) == 3 for vector in vectors)


def test_dashscope_embedding_retries_429_once_without_leaking_secret_material():
    leaked_token = "sk-" + "secret-value"
    client = FakeEmbeddingClient(
        [
            ProviderError(f"429 for Authorization Bearer {leaked_token}", status_code=429),
            embedding_response([[0.1, 0.2]]),
        ]
    )
    embedder = DashScopeEmbedding("text-embedding-v4", 2, client=client)

    assert embedder.embed(["分数"]) == [[0.1, 0.2]]
    assert len(client.embeddings.calls) == 2

    failing = FakeEmbeddingClient([ProviderError(f"500 {leaked_token}", status_code=500)])
    with pytest.raises(RuntimeError) as excinfo:
        DashScopeEmbedding("text-embedding-v4", 2, client=failing).embed(["分数"])
    assert leaked_token not in str(excinfo.value)
    assert len(failing.embeddings.calls) == 1


def test_dashscope_embedding_retries_network_error_once_and_rejects_bad_dimension():
    retry_client = FakeEmbeddingClient([NetworkError("temporary network outage"), embedding_response([[1.0, 0.0]])])
    assert DashScopeEmbedding("text-embedding-v4", 2, client=retry_client).embed(["分数"]) == [[1.0, 0.0]]
    assert len(retry_client.embeddings.calls) == 2

    bad_dimension_client = FakeEmbeddingClient([embedding_response([[1.0]])])
    with pytest.raises(ValueError, match="embedding dimension mismatch"):
        DashScopeEmbedding("text-embedding-v4", 2, client=bad_dimension_client).embed(["分数"])


def test_deepseek_intent_classifier_returns_strict_decision_and_sets_generation_controls():
    content = (
        '{"version":"v1","intents":["prerequisites"],'
        '"entities":[{"text":"分数","type":"Concept"}],"filters":{},'
        '"needs_clarification":false}'
    )
    client = FakeChatClient([chat_response(content)])
    classifier = DeepSeekIntentClassifier(model_name="deepseek-chat", client=client, timeout=11)

    decision = classifier.classify("学习分数前要会什么", {"grade": "四年级"})

    assert decision == IntentDecisionV1(
        version="v1",
        intents=("prerequisites",),
        entities=({"text": "分数", "type": "Concept"},),
        filters={},
        needs_clarification=False,
    )
    call = client.chat.completions.calls[0]
    assert call["temperature"] == 0
    assert call["timeout"] == 11
    assert call["response_format"] == {"type": "json_object"}
    assert call["model"] == "deepseek-chat"


def test_deepseek_intent_classifier_rejects_non_strict_json_and_retries_429_once():
    invalid_client = FakeChatClient(
        [
            chat_response(
                '{"version":"v1","intent":"prerequisites","route":"cypher",'
                '"entity_text":"分数","labels":["Concept"],"confidence":"high","extra":true}'
            )
        ]
    )
    with pytest.raises(ValueError, match="invalid IntentDecisionV1 JSON"):
        DeepSeekIntentClassifier(client=invalid_client).classify("学习分数前要会什么")

    retry_client = FakeChatClient(
        [
            ProviderError("rate limited", status_code=429),
            chat_response(
                '{"version":"v1","intents":["semantic_search"],'
                '"entities":[{"text":"小数","type":"Concept"}],"filters":{},'
                '"needs_clarification":false}'
            ),
        ]
    )

    decision = DeepSeekIntentClassifier(client=retry_client).classify("解释小数")

    assert decision.intents == ("semantic_search",)
    assert len(retry_client.chat.completions.calls) == 2
