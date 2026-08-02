#!/usr/bin/env python3
"""FastAPI entry point for the Neo4j retrieval service."""

from __future__ import annotations

import logging
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from utils.bootstrap import ensure_src_on_path

ensure_src_on_path(__file__)

from retrieval.auth import AuthenticationError, Authenticator  # noqa: E402
from retrieval.intent_classifier import DeepSeekIntentClassifier  # noqa: E402
from retrieval.models import (  # noqa: E402
    AuthenticationContext,
    RetrievalRequestV1,
    RetrievalResponseV1,
)
from retrieval.observability import (  # noqa: E402
    RequestObservation,
    configure_audit_logging,
    principal_hash,
    query_fingerprint,
    request_id,
)
from retrieval.policy import AuthorizationError  # noqa: E402
from retrieval.security import (  # noqa: E402
    UnsafeQuestionError,
    validate_question_input,
)
from retrieval.service import (  # noqa: E402
    RetrievalBackendUnavailable,
    RetrievalService,
)
from retrieval.settings import RetrievalSettings  # noqa: E402
from retrieval.store import Neo4jStore  # noqa: E402
from retrieval.teacher_analysis import DeepSeekTeacherPlanGenerator  # noqa: E402

LOGGER = logging.getLogger(__name__)


def create_app() -> Any:
    configure_audit_logging()
    settings = RetrievalSettings.from_env()
    settings.validate_runtime()
    store = Neo4jStore(settings, readonly=True)
    classifier = (
        DeepSeekIntentClassifier(
            model_name=settings.router_model,
            api_key=settings.router_api_key,
            base_url=settings.router_base_url,
            timeout=settings.router_timeout_seconds,
        )
        if settings.router_api_key
        else None
    )
    teacher_plan_generator = (
        DeepSeekTeacherPlanGenerator(
            model_name=settings.router_model,
            api_key=settings.router_api_key,
            base_url=settings.router_base_url,
            timeout=settings.router_timeout_seconds,
        )
        if settings.router_api_key
        else None
    )
    service = RetrievalService(
        store,
        settings,
        intent_classifier=classifier,
        teacher_plan_generator=teacher_plan_generator,
    )
    authenticator = Authenticator(settings)

    @asynccontextmanager
    async def lifespan(_app: Any):
        graph_ready = True
        try:
            store.warm_read_pool(settings.neo4j_pool_warmup_concurrency)
        except Exception:
            graph_ready = False
            LOGGER.warning("Neo4j READ connection pool warmup failed")
        if graph_ready:
            service.warm_embedding()
        yield
        store.close()

    app = FastAPI(title="K12-KGraph Neo4j Retrieval", version="1.0.0", lifespan=lifespan)
    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(settings.cors_origins),
            allow_credentials=True,
            allow_methods=["GET", "POST", "OPTIONS"],
            allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
            expose_headers=["Deprecation", "Sunset", "Link"],
        )

    def authentication_context(
        authorization: Optional[str] = Header(default=None),
    ) -> AuthenticationContext:
        try:
            return authenticator.authenticate(authorization)
        except AuthenticationError as exc:
            raise HTTPException(
                status_code=401,
                detail={"reason_code": "AUTHENTICATION_REQUIRED"},
                headers={"WWW-Authenticate": "Bearer"},
            ) from exc

    auth_dependency = Depends(authentication_context)

    @app.get("/health")
    def health() -> Dict[str, Any]:
        try:
            component = store.verify_connectivity()
            return {"ok": True, "neo4j": component}
        except Exception:
            return JSONResponse(status_code=503, content={"ok": False, "error": "neo4j unavailable"})

    @app.post("/retrieve")
    def retrieve(
        payload: Dict[str, Any],
        response: Response,
        context: AuthenticationContext = auth_dependency,
    ) -> Dict[str, Any]:
        response.headers.update(_legacy_headers())
        return _legacy_response(
            _run_v1(service, _legacy_request(payload), context, None)
        )

    @app.post("/retrieval/search")
    def search(
        payload: Dict[str, Any],
        response: Response,
        context: AuthenticationContext = auth_dependency,
    ) -> Dict[str, Any]:
        response.headers.update(_legacy_headers())
        return _legacy_response(
            _run_v1(service, _legacy_request(payload), context, None)
        )

    @app.post(
        "/v1/retrieval/search",
        response_model=RetrievalResponseV1,
        responses={
            401: {"description": "Authentication required or invalid"},
            403: {"description": "Authenticated principal lacks permission"},
            503: {"description": "Graph or upstream backend unavailable"},
        },
    )
    def search_v1(
        payload: RetrievalRequestV1,
        request: Request,
        context: AuthenticationContext = auth_dependency,
        x_request_id: Optional[str] = Header(default=None),
    ) -> RetrievalResponseV1:
        rid = request_id(x_request_id)
        observation = RequestObservation(
            request_id=rid,
            principal_id_hash=principal_hash(
                context.principal_id, settings.audit_hmac_key
            ),
            role=context.role,
            query_hash=query_fingerprint(payload.question),
        )
        with observation:
            try:
                validate_question_input(payload.question)
                result = service.search_v1(payload, context, request_id=rid)
            except UnsafeQuestionError as exc:
                observation.finish(
                    route="none",
                    reason="UNSAFE_QUERY_REJECTED",
                    status=403,
                )
                raise HTTPException(
                    status_code=403,
                    detail={"reason_code": "UNSAFE_QUERY_REJECTED"},
                ) from exc
            except AuthorizationError as exc:
                observation.finish(
                    route="none",
                    reason="FORBIDDEN_TOOL",
                    status=403,
                )
                raise HTTPException(
                    status_code=403,
                    detail={"reason_code": "FORBIDDEN_TOOL"},
                ) from exc
            except Exception as exc:
                observation.finish(
                    route="none",
                    reason="GRAPH_BACKEND_UNAVAILABLE",
                    status=503,
                )
                raise HTTPException(
                    status_code=503,
                    detail={"reason_code": "GRAPH_BACKEND_UNAVAILABLE"},
                ) from exc
            observation.finish(
                route=result.route,
                reason=result.reason_code,
                status=200,
                extra={
                    "intents": result.intents,
                    "entity_count": len(result.entities),
                    "top_k": payload.top_k,
                },
            )
            return result

    @app.get("/metrics")
    def metrics() -> Any:
        try:
            from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
        except ImportError:
            return PlainTextResponse("metrics unavailable\n", status_code=503)
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    return app


app = create_app()


def _legacy_request(payload: Dict[str, Any]) -> RetrievalRequestV1:
    allowed = {
        "question": payload.get("question", payload.get("query")),
        "grade": payload.get("grade"),
        "semester": _legacy_semester(payload.get("semester")),
        "edition": payload.get("edition"),
        "book_id": payload.get("book_id"),
        "section_id": payload.get("section_id"),
        "exercise_type": payload.get("exercise_type", payload.get("type")),
        "difficulty": payload.get("difficulty"),
        "top_k": payload.get("top_k", 5),
    }
    return RetrievalRequestV1.model_validate(
        {key: value for key, value in allowed.items() if value is not None}
    )


def _run_v1(
    service: RetrievalService,
    request: RetrievalRequestV1,
    context: AuthenticationContext,
    rid: Optional[str],
) -> RetrievalResponseV1:
    try:
        validate_question_input(request.question)
        return service.search_v1(request, context, request_id=rid)
    except UnsafeQuestionError as exc:
        raise HTTPException(
            status_code=403,
            detail={"reason_code": "UNSAFE_QUERY_REJECTED"},
        ) from exc
    except AuthorizationError as exc:
        raise HTTPException(
            status_code=403,
            detail={"reason_code": "FORBIDDEN_TOOL"},
        ) from exc
    except RetrievalBackendUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail={"reason_code": "GRAPH_BACKEND_UNAVAILABLE"},
        ) from exc


def _legacy_response(response: RetrievalResponseV1) -> Dict[str, Any]:
    payload = response.model_dump(mode="json")
    payload["intent"] = response.intents[0]
    payload["query"] = ""
    payload.pop("intents", None)
    payload.pop("request_id", None)
    payload.pop("reason_code", None)
    payload.pop("needs_clarification", None)
    payload.pop("scores", None)
    return payload


def _legacy_headers() -> Dict[str, str]:
    return {
        "Deprecation": "true",
        "Sunset": "2026-10-27",
        "Link": '</v1/retrieval/search>; rel="successor-version"',
    }


def _legacy_semester(value: Any) -> Any:
    if value == "上":
        return "上册"
    if value == "下":
        return "下册"
    return value
