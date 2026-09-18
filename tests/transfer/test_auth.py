from __future__ import annotations

from datetime import UTC, datetime, timedelta

import jwt
import pytest

from src.transfer.auth import JwtAuthorizer
from tests.transfer.conftest import settings_factory


class _SigningKey:
    key = "test-signing-key-for-transfer-auth-32"
    algorithm_name = "HS256"


class _FakeJwkClient:
    def __init__(self, url: str) -> None:
        self.url = url

    def get_signing_key_from_jwt(self, token: str) -> _SigningKey:
        return _SigningKey()


@pytest.mark.asyncio
async def test_jwt_authorizer_requires_exp_and_identity_claims(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("src.transfer.auth.jwt.PyJWKClient", _FakeJwkClient)
    settings = settings_factory()
    authorizer = JwtAuthorizer(settings)
    base_claims = {
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_audience,
        "sub": "subject-a",
        "tenant_id": "tenant-a",
        "scope": "transfer:read",
    }

    missing_exp = jwt.encode(base_claims, _SigningKey.key, algorithm="HS256")
    with pytest.raises(jwt.MissingRequiredClaimError):
        await authorizer.authorize(f"Bearer {missing_exp}")

    missing_subject = jwt.encode(
        {key: value for key, value in base_claims.items() if key != "sub"} | {
            "exp": datetime.now(UTC) + timedelta(minutes=5),
        },
        _SigningKey.key,
        algorithm="HS256",
    )
    with pytest.raises(jwt.MissingRequiredClaimError):
        await authorizer.authorize(f"Bearer {missing_subject}")

    valid = jwt.encode(
        {**base_claims, "exp": datetime.now(UTC) + timedelta(minutes=5)},
        _SigningKey.key,
        algorithm="HS256",
    )
    principal = await authorizer.authorize(f"Bearer {valid}")

    assert principal.subject == "subject-a"
    assert principal.tenant_id == "tenant-a"
