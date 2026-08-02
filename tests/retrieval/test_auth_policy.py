from __future__ import annotations

import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from retrieval.auth import AuthenticationError, Authenticator
from retrieval.models import AuthenticationContext
from retrieval.policy import (
    AuthorizationError,
    can_use_teacher_analysis,
    filter_properties,
    require_permission,
)
from retrieval.settings import RetrievalSettings


def test_development_auth_is_always_fixed_student():
    context = Authenticator(RetrievalSettings()).authenticate(
        "Bearer caller-controlled-token"
    )
    assert context.role == "student"
    assert context.permissions == frozenset({"retrieval:read"})


def test_rs256_fixture_jwt_maps_trusted_claims_and_rejects_hs256():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_key = private_key.public_key()
    now = int(time.time())
    settings = RetrievalSettings(
        auth_mode="jwks",
        jwt_issuer="https://issuer.test",
        jwt_audience="k12-retrieval",
        jwks_url="https://issuer.test/.well-known/jwks.json",
    )
    claims = {
        "sub": "teacher-1",
        "role": "teacher",
        "permissions": [
            "retrieval:read",
            "retrieval:answers",
            "retrieval:text2cypher",
        ],
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_audience,
        "iat": now,
        "nbf": now - 1,
        "exp": now + 60,
    }
    token = jwt.encode(claims, private_key, algorithm="RS256")
    context = Authenticator(
        settings, key_resolver=lambda _token: public_key
    ).authenticate(f"Bearer {token}")
    assert context.role == "teacher"
    assert can_use_teacher_analysis(context)

    bad_token = jwt.encode(claims, "shared-secret", algorithm="HS256")
    with pytest.raises(AuthenticationError):
        Authenticator(
            settings, key_resolver=lambda _token: "shared-secret"
        ).authenticate(f"Bearer {bad_token}")


def test_policy_requires_explicit_permissions_and_unknown_fields_fail_closed():
    student = AuthenticationContext(
        principal_id="student-1",
        role="student",
        permissions=frozenset({"retrieval:read"}),
    )
    require_permission(student, "retrieval:read")
    with pytest.raises(AuthorizationError):
        require_permission(student, "retrieval:answers")
    assert filter_properties(
        "Exercise",
        {
            "stem": "12个苹果平均分",
            "answer": "4",
            "analysis": "12÷3",
            "embedding": [0.1],
            "new_secret_field": "hidden",
        },
        student,
    ) == {"stem": "12个苹果平均分"}
