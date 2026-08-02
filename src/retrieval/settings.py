"""Runtime settings for the Neo4j retrieval service."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple

from utils.config import repo_root
from utils.envfile import load_env_file


@dataclass(frozen=True)
class RetrievalSettings:
    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str = ""
    neo4j_database: Optional[str] = None
    readonly_user: Optional[str] = None
    readonly_password: Optional[str] = None
    environment: str = "development"
    auth_mode: str = "development"
    jwt_algorithm: str = "RS256"
    jwt_issuer: Optional[str] = None
    jwt_audience: Optional[str] = None
    jwks_url: Optional[str] = None
    audit_hmac_key: Optional[str] = None
    cors_origins: Tuple[str, ...] = ()
    router_mode: str = "cascade"
    router_model: str = "deepseek-v4-flash"
    router_base_url: str = "https://api.deepseek.com"
    router_api_key: Optional[str] = None
    router_timeout_seconds: float = 4.0
    embedding_provider: str = "fastembed"
    embedding_base_url: Optional[str] = None
    embedding_api_key: Optional[str] = None
    embedding_model: str = "BAAI/bge-small-zh-v1.5"
    embedding_dimension: int = 512
    hybrid_backend: str = "graphrag"
    default_top_k: int = 5
    max_top_k: int = 20
    max_path_depth: int = 3
    query_timeout_seconds: float = 5.0
    neo4j_pool_warmup_concurrency: int = 8
    data_dir: Path = repo_root() / "data"

    @classmethod
    def from_env(cls, env_path: Optional[Path] = None) -> "RetrievalSettings":
        load_env_file(env_path or repo_root() / "config" / ".env")
        return cls(
            neo4j_uri=os.environ.get("NEO4J_URI", cls.neo4j_uri),
            neo4j_user=os.environ.get("NEO4J_USER", cls.neo4j_user),
            neo4j_password=os.environ.get("NEO4J_PASSWORD", ""),
            neo4j_database=os.environ.get("NEO4J_DATABASE") or None,
            readonly_user=os.environ.get("NEO4J_READONLY_USER") or None,
            readonly_password=os.environ.get("NEO4J_READONLY_PASSWORD") or None,
            environment=os.environ.get("K12_ENV", cls.environment).strip().lower(),
            auth_mode=os.environ.get("K12_AUTH_MODE", cls.auth_mode).strip().lower(),
            jwt_algorithm=os.environ.get(
                "K12_JWT_ALGORITHM", cls.jwt_algorithm
            ).strip(),
            jwt_issuer=os.environ.get("K12_JWT_ISSUER") or None,
            jwt_audience=os.environ.get("K12_JWT_AUDIENCE") or None,
            jwks_url=os.environ.get("K12_JWKS_URL") or None,
            audit_hmac_key=os.environ.get("K12_AUDIT_HMAC_KEY") or None,
            cors_origins=tuple(
                origin.strip()
                for origin in os.environ.get("K12_CORS_ORIGINS", "").split(",")
                if origin.strip()
            ),
            router_mode=os.environ.get(
                "K12_ROUTER_MODE", cls.router_mode
            ).strip().lower(),
            router_model=os.environ.get(
                "K12_ROUTER_MODEL",
                os.environ.get("DEEPSEEK_LLM_MODEL", cls.router_model),
            ),
            router_base_url=os.environ.get(
                "K12_ROUTER_BASE_URL",
                os.environ.get("DEEPSEEK_BASE_URL", cls.router_base_url),
            ),
            router_api_key=(
                os.environ.get("K12_ROUTER_API_KEY")
                or os.environ.get("DEEPSEEK_API_KEY")
                or None
            ),
            router_timeout_seconds=float(
                os.environ.get(
                    "K12_ROUTER_TIMEOUT_SECONDS", cls.router_timeout_seconds
                )
            ),
            embedding_provider=os.environ.get(
                "K12_RETRIEVAL_EMBEDDING_PROVIDER", cls.embedding_provider
            ).strip().lower(),
            embedding_base_url=(
                os.environ.get("K12_RETRIEVAL_EMBEDDING_BASE_URL")
                or os.environ.get("EMBEDDING_BASE_URL")
                or None
            ),
            embedding_api_key=(
                os.environ.get("K12_RETRIEVAL_EMBEDDING_API_KEY")
                or os.environ.get("EMBEDDING_API_KEY")
                or None
            ),
            embedding_model=os.environ.get(
                "K12_RETRIEVAL_EMBEDDING_MODEL",
                os.environ.get("EMBEDDING_MODEL", cls.embedding_model),
            ),
            embedding_dimension=int(
                os.environ.get(
                    "K12_RETRIEVAL_EMBEDDING_DIMENSION",
                    os.environ.get(
                        "EMBEDDING_DIMENSIONS",
                        os.environ.get("EMBEDDING_DIMENSION", cls.embedding_dimension),
                    ),
                )
            ),
            hybrid_backend=os.environ.get(
                "K12_RETRIEVAL_HYBRID_BACKEND",
                cls.hybrid_backend,
            ).strip().lower(),
            default_top_k=int(os.environ.get("K12_RETRIEVAL_TOP_K", cls.default_top_k)),
            max_top_k=int(os.environ.get("K12_RETRIEVAL_MAX_TOP_K", cls.max_top_k)),
            max_path_depth=int(os.environ.get("K12_RETRIEVAL_MAX_PATH_DEPTH", cls.max_path_depth)),
            query_timeout_seconds=float(os.environ.get("K12_RETRIEVAL_TIMEOUT_SECONDS", cls.query_timeout_seconds)),
            neo4j_pool_warmup_concurrency=int(
                os.environ.get(
                    "K12_NEO4J_POOL_WARMUP_CONCURRENCY",
                    cls.neo4j_pool_warmup_concurrency,
                )
            ),
            data_dir=Path(os.environ.get("K12_RETRIEVAL_DATA_DIR", str(cls.data_dir))).resolve(),
        )

    def validate_runtime(self) -> None:
        if self.environment not in {"development", "test", "production"}:
            raise ValueError("K12_ENV must be development, test, or production")
        if self.auth_mode not in {"development", "jwks"}:
            raise ValueError("K12_AUTH_MODE must be development or jwks")
        if self.router_mode != "cascade":
            raise ValueError("K12_ROUTER_MODE currently supports only cascade")
        if self.embedding_provider not in {"fastembed", "openai", "hash"}:
            raise ValueError(
                "K12_RETRIEVAL_EMBEDDING_PROVIDER must be fastembed, openai, or hash"
            )
        if self.jwt_algorithm != "RS256":
            raise ValueError("K12_JWT_ALGORITHM must be RS256")
        if self.auth_mode == "jwks" and not all(
            (self.jwt_issuer, self.jwt_audience, self.jwks_url)
        ):
            raise ValueError(
                "JWKS authentication requires issuer, audience, and JWKS URL"
            )
        if self.environment == "production":
            if self.auth_mode != "jwks":
                raise ValueError("production requires K12_AUTH_MODE=jwks")
            if not self.readonly_user or not self.readonly_password:
                raise ValueError("production requires a dedicated Neo4j read-only user")
            if not self.audit_hmac_key:
                raise ValueError("production requires K12_AUDIT_HMAC_KEY")
            if "*" in self.cors_origins:
                raise ValueError("production CORS origins cannot use wildcard")
        if self.embedding_provider == "openai" and (
            not self.embedding_api_key or not self.embedding_base_url
        ):
            raise ValueError(
                "OpenAI-compatible embedding requires API key and base URL"
            )
        if not 1 <= self.neo4j_pool_warmup_concurrency <= 32:
            raise ValueError(
                "K12_NEO4J_POOL_WARMUP_CONCURRENCY must be between 1 and 32"
            )

    def clamp_top_k(self, value: Optional[int]) -> int:
        if value is None:
            return self.default_top_k
        return max(1, min(int(value), self.max_top_k))

    def clamp_depth(self, value: Optional[int]) -> int:
        if value is None:
            return self.max_path_depth
        return max(1, min(int(value), self.max_path_depth))
