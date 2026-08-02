"""Low-cardinality metrics and privacy-preserving retrieval audit helpers."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Optional

LOGGER = logging.getLogger("retrieval.audit")

try:
    from prometheus_client import Counter, Histogram

    REQUESTS = Counter(
        "k12_retrieval_requests_total",
        "Retrieval requests",
        ("route", "reason", "status"),
    )
    LATENCY = Histogram(
        "k12_retrieval_request_seconds",
        "Retrieval request latency",
        ("route",),
    )
except ImportError:  # pragma: no cover - optional until deployment dependency
    REQUESTS = None
    LATENCY = None


def configure_audit_logging() -> None:
    """Ensure privacy-safe audit records are emitted by serving processes."""

    LOGGER.setLevel(logging.INFO)
    LOGGER.disabled = False
    if not any(getattr(handler, "_k12_audit_handler", False) for handler in LOGGER.handlers):
        handler = logging.StreamHandler()
        handler.setLevel(logging.INFO)
        handler.setFormatter(logging.Formatter("%(message)s"))
        handler._k12_audit_handler = True  # type: ignore[attr-defined]
        LOGGER.addHandler(handler)
    LOGGER.propagate = False


def request_id(value: Optional[str]) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except (TypeError, ValueError):
        return str(uuid.uuid4())


def principal_hash(principal_id: str, key: Optional[str]) -> str:
    if not key:
        return "development"
    return hmac.new(
        key.encode("utf-8"),
        principal_id.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def query_fingerprint(question: str) -> str:
    normalized = " ".join(str(question).strip().split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


@dataclass
class RequestObservation:
    request_id: str
    principal_id_hash: str
    role: str
    query_hash: str
    started_at: float = 0.0

    def __enter__(self) -> "RequestObservation":
        self.started_at = time.perf_counter()
        return self

    def finish(
        self,
        *,
        route: str,
        reason: str,
        status: int,
        extra: Optional[Dict[str, Any]] = None,
    ) -> None:
        elapsed = max(0.0, time.perf_counter() - self.started_at)
        safe_extra = {
            key: value
            for key, value in (extra or {}).items()
            if key
            in {
                "intents",
                "entity_count",
                "top_k",
                "fallback_reason",
                "warning_code",
                "error_code",
            }
        }
        LOGGER.info(
            json.dumps(
                {
                    "request_id": self.request_id,
                    "principal_id_hash": self.principal_id_hash,
                    "role": self.role,
                    "query_hash": self.query_hash,
                    "route": route,
                    "reason_code": reason,
                    "status": status,
                    "latency_ms": round(elapsed * 1000, 3),
                    **safe_extra,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        if REQUESTS is not None:
            REQUESTS.labels(route=route, reason=reason, status=str(status)).inc()
        if LATENCY is not None:
            LATENCY.labels(route=route).observe(elapsed)

    def __exit__(self, *exc: object) -> None:
        return None
