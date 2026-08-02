"""Trusted authentication context for the retrieval HTTP interface."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Optional

from retrieval.models import AuthenticationContext
from retrieval.settings import RetrievalSettings


class AuthenticationError(ValueError):
    """Raised when credentials are absent or invalid."""


KeyResolver = Callable[[str], Any]


@dataclass
class Authenticator:
    settings: RetrievalSettings
    key_resolver: Optional[KeyResolver] = None

    def authenticate(self, authorization: Optional[str]) -> AuthenticationContext:
        if self.settings.auth_mode == "development":
            return AuthenticationContext(
                principal_id="development-student",
                role="student",
                permissions=frozenset({"retrieval:read"}),
            )
        if not authorization or not authorization.startswith("Bearer "):
            raise AuthenticationError("Bearer authentication is required")
        token = authorization.removeprefix("Bearer ").strip()
        if not token:
            raise AuthenticationError("Bearer authentication is required")
        return self._decode_jwt(token)

    def _decode_jwt(self, token: str) -> AuthenticationContext:
        try:
            import jwt
            from jwt import PyJWKClient
        except ImportError as exc:  # pragma: no cover - dependency installation
            raise AuthenticationError("JWT support is unavailable") from exc

        try:
            if self.key_resolver is not None:
                signing_key = self.key_resolver(token)
            else:
                if not self.settings.jwks_url:
                    raise AuthenticationError("JWKS URL is not configured")
                signing_key = PyJWKClient(
                    self.settings.jwks_url,
                    cache_keys=True,
                    lifespan=300,
                    timeout=2,
                ).get_signing_key_from_jwt(token).key
            claims = jwt.decode(
                token,
                signing_key,
                algorithms=["RS256"],
                issuer=self.settings.jwt_issuer,
                audience=self.settings.jwt_audience,
                leeway=30,
                options={
                    "require": ["sub", "role", "permissions", "exp", "nbf"],
                    "verify_signature": True,
                    "verify_exp": True,
                    "verify_nbf": True,
                    "verify_iss": True,
                    "verify_aud": True,
                },
            )
            return AuthenticationContext.model_validate(
                {
                    "principal_id": claims["sub"],
                    "role": claims["role"],
                    "permissions": claims["permissions"],
                    "tenant_id": claims.get("tenant_id"),
                    "authenticated": True,
                }
            )
        except AuthenticationError:
            raise
        except Exception as exc:
            raise AuthenticationError("invalid authentication token") from exc
