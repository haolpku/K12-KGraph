"""Embedding providers for retrieval indexes."""

from __future__ import annotations

import hashlib
import math
from collections.abc import Sequence
from threading import Lock
from typing import Any, Iterable, List, Protocol


class Embedder(Protocol):
    dimension: int

    def embed(self, texts: Iterable[str]) -> List[List[float]]:
        ...


class HashEmbedder:
    """Deterministic local fallback used for tests and offline demos.

    It is not a semantic model. It keeps indexing and evaluation code runnable when
    fastembed model files or network access are unavailable.
    """

    def __init__(self, dimension: int = 512) -> None:
        self.dimension = dimension

    def embed(self, texts: Iterable[str]) -> List[List[float]]:
        return [self._one(text) for text in texts]

    def _one(self, text: str) -> List[float]:
        vec = [0.0] * self.dimension
        for token in str(text or "").split():
            digest = hashlib.sha256(token.encode("utf-8")).digest()
            idx = int.from_bytes(digest[:4], "big") % self.dimension
            sign = 1.0 if digest[4] % 2 == 0 else -1.0
            vec[idx] += sign
        norm = math.sqrt(sum(x * x for x in vec)) or 1.0
        return [x / norm for x in vec]


class FastEmbedder:
    def __init__(
        self,
        model_name: str,
        dimension: int,
        *,
        threads: int = 1,
        parallel: int = 1,
    ) -> None:
        self.model_name = model_name
        self.dimension = dimension
        self.threads = threads
        self.parallel = parallel
        self._model = None
        self._model_lock = Lock()
        self._inference_lock = Lock()

    def embed(self, texts: Iterable[str]) -> List[List[float]]:
        if self._model is None:
            with self._model_lock:
                if self._model is None:
                    try:
                        from fastembed import TextEmbedding
                    except ImportError as exc:
                        raise RuntimeError("Install fastembed to generate semantic vectors") from exc
                    self._model = TextEmbedding(
                        model_name=self.model_name,
                        threads=self.threads,
                    )
        with self._inference_lock:
            vectors = [
                list(map(float, vector))
                for vector in self._model.embed(
                    list(texts),
                    parallel=self.parallel,
                )
            ]
        if vectors and len(vectors[0]) != self.dimension:
            raise ValueError(f"embedding dimension mismatch: expected {self.dimension}, got {len(vectors[0])}")
        return vectors


class DashScopeEmbedding:
    """OpenAI-compatible DashScope embedding adapter.

    The adapter accepts an injected OpenAI SDK compatible client for tests or custom
    runtime wiring. It keeps outbound embedding batches at or below DashScope's
    documented small-batch limit and validates every returned vector dimension.
    """

    max_batch_size = 10

    def __init__(
        self,
        model_name: str,
        dimension: int,
        *,
        api_key: str | None = None,
        base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1",
        timeout: float = 30.0,
        client: Any | None = None,
    ) -> None:
        self.model_name = model_name
        self.dimension = dimension
        self.timeout = timeout
        self._client = client
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")

    def embed(self, texts: Iterable[str]) -> List[List[float]]:
        materialized = [str(text or "") for text in texts]
        vectors: list[list[float]] = []
        for batch in _chunks(materialized, self.max_batch_size):
            request_input = list(batch)
            response = _retry_once_if_retryable(
                lambda request_input=request_input: self._client_for_request().embeddings.create(
                    model=self.model_name,
                    input=request_input,
                    dimensions=self.dimension,
                    encoding_format="float",
                    timeout=self.timeout,
                )
            )
            vectors.extend(self._vectors_from_response(response))
        return vectors

    def _client_for_request(self) -> Any:
        if self._client is not None:
            return self._client
        if not self._api_key or not self._api_key.strip():
            raise ValueError("missing DashScope api_key")
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise RuntimeError("Install openai to use DashScope embeddings") from exc
        self._client = OpenAI(api_key=self._api_key, base_url=self._base_url, timeout=self.timeout)
        return self._client

    def _vectors_from_response(self, response: Any) -> list[list[float]]:
        data = _response_value(response, "data")
        if not isinstance(data, Sequence):
            raise ValueError("embedding response missing data list")
        vectors: list[list[float]] = []
        for item in data:
            embedding = _response_value(item, "embedding")
            if not isinstance(embedding, Sequence) or isinstance(embedding, (str, bytes)):
                raise ValueError("embedding response item missing vector")
            vector = [float(value) for value in embedding]
            if len(vector) != self.dimension:
                raise ValueError(f"embedding dimension mismatch: expected {self.dimension}, got {len(vector)}")
            vectors.append(vector)
        return vectors


def make_embedder(model_name: str, dimension: int, *, offline_hash: bool = False) -> Embedder:
    if offline_hash:
        return HashEmbedder(dimension)
    return FastEmbedder(model_name, dimension)


def _chunks(items: list[str], size: int) -> Iterable[list[str]]:
    for index in range(0, len(items), size):
        yield items[index : index + size]


def _response_value(item: Any, key: str) -> Any:
    if isinstance(item, dict):
        return item.get(key)
    return getattr(item, key, None)


def _retry_once_if_retryable(operation: Any) -> Any:
    try:
        return operation()
    except Exception as exc:  # noqa: BLE001 - external SDKs expose several retryable exception classes.
        if not _is_retryable_embedding_error(exc):
            raise RuntimeError("embedding request failed") from None
    try:
        return operation()
    except Exception:  # noqa: BLE001 - suppress provider details that may include credentials.
        raise RuntimeError("embedding request failed after retry") from None


def _is_retryable_embedding_error(exc: Exception) -> bool:
    status_code = getattr(exc, "status_code", None)
    response = getattr(exc, "response", None)
    if status_code is None and response is not None:
        status_code = getattr(response, "status_code", None)
    if status_code == 429:
        return True
    name = exc.__class__.__name__.lower()
    return any(token in name for token in ("timeout", "connection", "network", "connect", "readtimeout"))
