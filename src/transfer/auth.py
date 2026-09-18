from __future__ import annotations

import asyncio
import json
from typing import Protocol

import httpx
import jwt
from pydantic import BaseModel, Field, SecretStr

from .settings import TransferSettings as Settings


class SecretBundle(BaseModel):
    values: dict[str, SecretStr] = Field(repr=False)


class CredentialResolver(Protocol):
    async def resolve(self, credential_ref: str) -> SecretBundle: ...


class EnvironmentCredentialResolver:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        raw_json = settings.credentials_json.get_secret_value() if settings.credentials_json is not None else "{}"
        decoded = json.loads(raw_json)
        if not isinstance(decoded, dict):
            raise RuntimeError("TRANSFER_CREDENTIALS_JSON must decode to an object")
        self._credentials = decoded

    async def resolve(self, credential_ref: str) -> SecretBundle:
        if credential_ref not in self._credentials:
            raise RuntimeError(f"credential ref not found: {credential_ref}")
        value = self._credentials[credential_ref]
        if isinstance(value, str):
            return SecretBundle(values={"value": SecretStr(value)})
        if not isinstance(value, dict):
            raise RuntimeError(f"credential ref has invalid payload: {credential_ref}")
        return SecretBundle(values={key: SecretStr(str(raw)) for key, raw in value.items()})


class Principal(BaseModel):
    tenant_id: str
    subject: str
    scopes: frozenset[str]


class JwtAuthorizer:
    def __init__(self, settings: Settings, http_client: httpx.AsyncClient | None = None) -> None:
        self._settings = settings
        self._jwk_client = jwt.PyJWKClient(settings.jwks_url)
        self._http_client = http_client

    async def authorize(self, authorization_header: str) -> Principal:
        scheme, _, token = authorization_header.partition(" ")
        if scheme.lower() != "bearer" or not token:
            raise ValueError("invalid authorization header")
        signing_key = await asyncio.to_thread(self._jwk_client.get_signing_key_from_jwt, token)
        claims = jwt.decode(
            token,
            signing_key.key,
            audience=self._settings.jwt_audience,
            issuer=self._settings.jwt_issuer,
            algorithms=[signing_key.algorithm_name],
        )
        scopes = claims.get("scope", "")
        return Principal(
            tenant_id=str(claims.get("tenant_id", "")),
            subject=str(claims.get("sub", "")),
            scopes=frozenset(part for part in scopes.split(" ") if part),
        )