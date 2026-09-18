from __future__ import annotations

import base64
import json
from collections import Counter
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from cryptography.fernet import Fernet
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from src.transfer.app import create_app, current_principal
from src.transfer.auth import Principal
from src.transfer.models import TransferStatus
from src.transfer.rest_connector import RestOpenApiConnector
from src.transfer.worker import RetryPolicy, TransferWorker
from tests.transfer.conftest import settings_factory

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES_DIR = REPO_ROOT / "tests" / "transfer" / "fixtures"
EXAMPLES_DIR = REPO_ROOT / "examples" / "ocr"
_REAL_ASYNC_CLIENT = httpx.AsyncClient
SECRET_VALUES = (
    "test-target-key",
    "test-alt-token",
    "test-basic-password",
    "test-oauth-secret",
    "expired-oauth-token",
    "fresh-oauth-token",
)


@dataclass
class TargetVariant:
    auth_mode: str
    api_version: str
    api_key: str = "test-target-key"
    bearer_token: str = "test-alt-token"
    username: str = "test-basic-user"
    password: str = "test-basic-password"


class TargetApi:
    def __init__(self) -> None:
        self.app = FastAPI(title="in-process target invoice API")
        self.variants: dict[str, TargetVariant] = {}
        self.invoices: dict[str, dict[str, Any]] = {}
        self.calls: Counter[str] = Counter()
        self.auth_failures = 0
        self.fail_next_429 = False
        self.validation_error_next = False
        self.drop_next: str | None = None
        self.oauth_token_calls = 0
        self._install_routes()

    def register_variant(
        self,
        prefix: str,
        *,
        auth_mode: str,
        api_version: str,
    ) -> None:
        self.variants[prefix] = TargetVariant(
            auth_mode=auth_mode,
            api_version=api_version,
        )

    def count(self, method: str, prefix: str) -> int:
        return self.calls[f"{method.upper()}:{prefix}"]

    def invoice_id(self, prefix: str, external_id: str) -> str:
        return f"{prefix}-resource-{external_id}"

    def auth_headers(self, prefix: str) -> dict[str, str]:
        variant = self.variants[prefix]
        headers = {"X-Api-Version": variant.api_version}
        if variant.auth_mode == "api_key":
            headers["X-Api-Key"] = variant.api_key
        elif variant.auth_mode == "bearer":
            headers["Authorization"] = f"Bearer {variant.bearer_token}"
        elif variant.auth_mode == "basic":
            encoded = base64.b64encode(
                f"{variant.username}:{variant.password}".encode()
            ).decode("ascii")
            headers["Authorization"] = f"Basic {encoded}"
        return headers

    def _install_routes(self) -> None:
        @self.app.post("/{prefix}/invoices")
        async def create_invoice(prefix: str, request: Request) -> JSONResponse:
            return await self._write_invoice(prefix, request, method="POST")

        @self.app.get("/{prefix}/invoices")
        async def find_invoices(prefix: str, request: Request) -> JSONResponse:
            variant = self.variants.get(prefix)
            if variant is None:
                return JSONResponse(status_code=404, content={"code": "UNKNOWN_TARGET"})
            auth_error = self._authorize(prefix, request, variant)
            if auth_error is not None:
                return auth_error
            self.calls[f"GET:{prefix}"] += 1
            requested_id = request.query_params.get("external_id")
            if not requested_id:
                return JSONResponse(
                    status_code=400,
                    content={"code": "VALIDATION_ERROR", "message": "external_id is required"},
                )
            invoice = self._find_invoice(prefix, requested_id)
            return JSONResponse(status_code=200, content={"items": [invoice] if invoice else []})

        @self.app.get("/{prefix}/invoices/{external_id}")
        async def get_invoice(prefix: str, external_id: str, request: Request) -> JSONResponse:
            variant = self.variants.get(prefix)
            if variant is None:
                return JSONResponse(status_code=404, content={"code": "UNKNOWN_TARGET"})
            auth_error = self._authorize(prefix, request, variant)
            if auth_error is not None:
                return auth_error
            self.calls[f"GET:{prefix}"] += 1
            invoice = self._find_invoice(prefix, external_id)
            if invoice is None:
                return JSONResponse(status_code=404, content={"code": "NOT_FOUND"})
            return JSONResponse(status_code=200, content=invoice)

        @self.app.put("/{prefix}/invoices/{external_id}")
        async def update_invoice(prefix: str, external_id: str, request: Request) -> JSONResponse:
            return await self._write_invoice(
                prefix,
                request,
                method="PUT",
                path_external_id=external_id,
            )

        @self.app.post("/oauth/token")
        async def issue_oauth_token(request: Request) -> JSONResponse:
            self.oauth_token_calls += 1
            expected = "Basic " + base64.b64encode(
                b"test-oauth-client:test-oauth-secret"
            ).decode("ascii")
            if request.headers.get("Authorization") != expected:
                self.auth_failures += 1
                return JSONResponse(status_code=401, content={"code": "UNAUTHORIZED"})
            token = (
                "expired-oauth-token"
                if self.oauth_token_calls == 1
                else "fresh-oauth-token"
            )
            return JSONResponse(
                status_code=200,
                content={"access_token": token, "token_type": "Bearer", "expires_in": 3600},
            )

    async def _write_invoice(
        self,
        prefix: str,
        request: Request,
        *,
        method: str,
        path_external_id: str | None = None,
    ) -> JSONResponse:
        variant = self.variants.get(prefix)
        if variant is None:
            return JSONResponse(status_code=404, content={"code": "UNKNOWN_TARGET"})
        auth_error = self._authorize(prefix, request, variant)
        if auth_error is not None:
            return auth_error
        self.calls[f"{method}:{prefix}"] += 1
        if self.fail_next_429:
            self.fail_next_429 = False
            return JSONResponse(
                status_code=429,
                headers={"Retry-After": "0"},
                content={"code": "RATE_LIMITED"},
            )
        if self.validation_error_next:
            self.validation_error_next = False
            return JSONResponse(
                status_code=400,
                content={"code": "TARGET_VALIDATION", "message": "invoice payload rejected"},
            )
        try:
            body = await request.json()
        except ValueError:
            return JSONResponse(status_code=400, content={"code": "INVALID_JSON"})
        if not isinstance(body, dict):
            return JSONResponse(status_code=400, content={"code": "VALIDATION_ERROR"})
        external_id = body.get("external_id")
        if not isinstance(external_id, str) or not external_id:
            return JSONResponse(
                status_code=400,
                content={"code": "VALIDATION_ERROR", "message": "external_id is required"},
            )
        if (
            path_external_id is not None
            and self._find_invoice(prefix, path_external_id) is None
            and path_external_id != external_id
        ):
            return JSONResponse(status_code=404, content={"code": "NOT_FOUND"})
        if path_external_id is not None and path_external_id not in {external_id, self.invoice_id(prefix, external_id)}:
            return JSONResponse(status_code=400, content={"code": "VALIDATION_ERROR"})
        if self.drop_next == "before_write":
            self.drop_next = None
            raise RuntimeError("simulated target connection drop")

        key = f"{prefix}:{external_id}"
        existing = self.invoices.get(key)
        if method == "POST" and existing is not None:
            return JSONResponse(status_code=409, content={"code": "DUPLICATE_EXTERNAL_ID"})
        invoice = {
            **body,
            "id": existing["id"] if existing is not None else self.invoice_id(prefix, external_id),
            "external_id": external_id,
        }
        self.invoices[key] = invoice
        if self.drop_next == "after_write":
            self.drop_next = None
            raise RuntimeError("simulated target connection drop")
        status_code = 200 if existing is not None else 201
        return JSONResponse(
            status_code=status_code,
            headers={"X-Request-Id": f"request-{method.lower()}-{self.count(method, prefix)}"},
            content=invoice,
        )

    def _authorize(
        self,
        prefix: str,
        request: Request,
        variant: TargetVariant,
    ) -> JSONResponse | None:
        if request.headers.get("X-Api-Version") != variant.api_version:
            self.auth_failures += 1
            return JSONResponse(
                status_code=400,
                content={"code": "API_VERSION_REQUIRED"},
            )
        authorization = request.headers.get("Authorization", "")
        if variant.auth_mode == "api_key":
            valid = request.headers.get("X-Api-Key") == variant.api_key
        elif variant.auth_mode == "bearer":
            valid = authorization == f"Bearer {variant.bearer_token}"
        elif variant.auth_mode == "basic":
            expected = "Basic " + base64.b64encode(
                f"{variant.username}:{variant.password}".encode()
            ).decode("ascii")
            valid = authorization == expected
        elif variant.auth_mode == "oauth2":
            if authorization == "Bearer expired-oauth-token":
                return JSONResponse(
                    status_code=401,
                    headers={
                        "WWW-Authenticate": 'Bearer error="invalid_token", error_description="expired"'
                    },
                    content={"code": "TOKEN_EXPIRED"},
                )
            valid = authorization == "Bearer fresh-oauth-token"
        else:
            valid = False
        if not valid:
            self.auth_failures += 1
            return JSONResponse(status_code=401, content={"code": "UNAUTHORIZED"})
        return None

    def _find_invoice(self, prefix: str, lookup: str) -> dict[str, Any] | None:
        direct = self.invoices.get(f"{prefix}:{lookup}")
        if direct is not None:
            return direct
        for key, invoice in self.invoices.items():
            if key.startswith(f"{prefix}:") and invoice.get("id") == lookup:
                return invoice
        return None


class AppClient:
    def __init__(self, app: FastAPI, client: httpx.AsyncClient, target_api: TargetApi) -> None:
        self.app = app
        self.client = client
        self.target_api = target_api

    async def register(
        self,
        spec_name: str,
        connector_id: str,
        *,
        auth_mode: str | None = None,
        prefix: str,
        api_version: str,
    ) -> tuple[str, str]:
        spec = _load_yaml(spec_name)
        if auth_mode is not None:
            spec = _auth_variant(spec, auth_mode=auth_mode, prefix=prefix)
            self.target_api.register_variant(
                prefix,
                auth_mode=auth_mode,
                api_version=api_version,
            )
        mapping = _load_json("invoice_mapping.json")
        mapping_id = f"invoice-{connector_id}"
        mapping["mapping_id"] = mapping_id
        mapping["connector_id"] = connector_id
        connector_response = await self.client.post(
            "/v1/connectors",
            json={
                "connector_id": connector_id,
                "type": "rest-openapi",
                "display_name": f"Invoice target {connector_id}",
                "base_url": spec["servers"][0]["url"],
                "spec": spec,
                "credential_ref": f"vault://connectors/{_credential_mode(auth_mode, spec_name)}",
                "additional_headers": [
                    {
                        "name": "X-Api-Version",
                        "value_ref": f"config://headers/{prefix}-api-version",
                    }
                ],
                "policy": {
                    "connect_timeout_seconds": 5,
                    "read_timeout_seconds": 30,
                    "total_timeout_seconds": 60,
                    "max_response_bytes": 1024 * 1024,
                    "max_redirects": 0,
                },
            },
        )
        assert connector_response.status_code == 201, connector_response.text
        validation_response = await self.client.post(
            f"/v1/connectors/{connector_id}/validate",
            json={},
        )
        assert validation_response.status_code == 202, validation_response.text
        mapping_response = await self.client.post("/v1/mappings", json=mapping)
        assert mapping_response.status_code == 201, mapping_response.text
        return connector_id, mapping_id

    async def post_transfer(self, payload: dict[str, Any], *, key: str) -> httpx.Response:
        return await self.client.post(
            "/v1/transfers",
            headers={"Idempotency-Key": key},
            json=payload,
        )

    async def get_transfer(self, transfer_id: str) -> dict[str, Any]:
        response = await self.client.get(f"/v1/transfers/{transfer_id}")
        assert response.status_code == 200, response.text
        return response.json()

    async def wait_for_status(
        self,
        transfer_id: str,
        expected: str | TransferStatus,
        *,
        max_steps: int = 24,
    ) -> dict[str, Any]:
        expected_value = expected.value if isinstance(expected, TransferStatus) else expected
        for _ in range(max_steps):
            current = await self.get_transfer(transfer_id)
            if current["status"] == expected_value:
                return current
            worked = await self.app.state.worker.run_once()
            if not worked:
                current = await self.get_transfer(transfer_id)
                if current["status"] == expected_value:
                    return current
        raise AssertionError(
            f"transfer {transfer_id} did not reach {expected_value}: {current['status']}"
        )

    async def reconcile_automatically(self, transfer_id: str) -> dict[str, Any]:
        record = await self.app.state.worker.reconcile_transfer("tenant-a", transfer_id)
        return record.model_dump(mode="json")

    async def reconcile_with_evidence(
        self,
        transfer_id: str,
        evidence: dict[str, Any],
    ) -> dict[str, Any]:
        response = await self.client.post(
            f"/v1/transfers/{transfer_id}/reconcile",
            json=evidence,
        )
        assert response.status_code == 200, response.text
        return response.json()

    async def target_list(self, prefix: str, external_id: str) -> httpx.Response:
        transport = httpx.ASGITransport(app=self.target_api.app)
        async with _REAL_ASYNC_CLIENT(
            transport=transport,
            base_url="http://target.test",
        ) as client:
            return await client.get(
                f"/{prefix}/invoices",
                params={"external_id": external_id},
                headers=self.target_api.auth_headers(prefix),
            )


def _load_json(name: str) -> dict[str, Any]:
    path = FIXTURES_DIR / name if name == "invoice_mapping.json" else EXAMPLES_DIR / name
    return json.loads(path.read_text(encoding="utf-8"))


def _load_yaml(name: str) -> dict[str, Any]:
    return yaml.safe_load((FIXTURES_DIR / name).read_text(encoding="utf-8"))


def _credential_mode(auth_mode: str | None, spec_name: str) -> str:
    if auth_mode is not None:
        return auth_mode
    return "bearer" if spec_name.endswith("_alt.yaml") else "api-key"


def _auth_variant(spec: dict[str, Any], *, auth_mode: str, prefix: str) -> dict[str, Any]:
    variant = deepcopy(spec)
    renamed_paths: dict[str, Any] = {}
    for path, value in variant["paths"].items():
        renamed_paths[path.replace("/target", f"/{prefix}")] = value
    variant["paths"] = renamed_paths
    for operation in _operations(variant):
        if auth_mode == "basic":
            operation["security"] = [{"basicAuth": []}]
        else:
            operation["security"] = [{"oauthAuth": []}]
    if auth_mode == "basic":
        variant["components"]["securitySchemes"] = {
            "basicAuth": {"type": "http", "scheme": "basic"}
        }
    else:
        variant["components"]["securitySchemes"] = {
            "oauthAuth": {
                "type": "oauth2",
                "flows": {
                    "clientCredentials": {
                        "tokenUrl": "http://target.test/oauth/token",
                        "scopes": {},
                    }
                },
            }
        }
    return variant


def _operations(spec: dict[str, Any]) -> list[dict[str, Any]]:
    operations: list[dict[str, Any]] = []
    for path_item in spec["paths"].values():
        for method, operation in path_item.items():
            if method in {"get", "post", "put", "patch", "delete"}:
                operations.append(operation)
    return operations


def invoice_payload(
    *,
    operation: str = "upsert",
    suffix: str = "001",
    variant: str = "primary",
    connector_id: str = "invoice-target",
    mapping_id: str = "invoice-v1",
) -> dict[str, Any]:
    name = "invoice-transfer-alt.json" if variant == "alt" else "invoice-transfer.json"
    payload = _load_json(name)
    payload["document"]["document_id"] = f"doc-{suffix}"
    payload["document"]["content"]["filename"] = f"invoice-{suffix}.pdf"
    payload["document"]["content"]["storage_ref"] = f"object://documents/doc-{suffix}"
    payload["ocr"]["text_ref"] = f"object://ocr-text/doc-{suffix}"
    payload["ocr"]["fields"]["invoice_number"]["value"] = f"INV-{suffix}"
    payload["ocr"]["fields"]["invoice_number"]["raw_value"] = f"INV-{suffix}"
    payload["metadata"]["correlation_id"] = f"corr-{suffix}"
    payload["delivery"].update(
        {
            "connector_id": connector_id,
            "mapping_id": mapping_id,
            "operation": operation,
        }
    )
    return payload


@pytest.fixture
def target_api() -> TargetApi:
    target = TargetApi()
    target.register_variant("target", auth_mode="api_key", api_version="2026-01-01")
    target.register_variant("alternate", auth_mode="bearer", api_version="2026-02-01")
    return target


@pytest.fixture
def integration_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    encryption_key = Fernet.generate_key().decode("ascii")
    credentials = {
        "vault://connectors/api-key": {"auth": "test-target-key"},
        "vault://connectors/bearer": {"token": "test-alt-token"},
        "vault://connectors/basic": {
            "username": "test-basic-user",
            "password": "test-basic-password",
        },
        "vault://connectors/oauth2": {
            "client_id": "test-oauth-client",
            "client_secret": "test-oauth-secret",
        },
        "config://headers/target-api-version": {"value": "2026-01-01"},
        "config://headers/alternate-api-version": {"value": "2026-02-01"},
        "config://headers/basic-api-version": {"value": "2026-03-01"},
        "config://headers/oauth-api-version": {"value": "2026-04-01"},
        "key://transfer/data": encryption_key,
    }
    monkeypatch.setattr(
        "src.transfer.rest_connector.resolve_host_addresses",
        lambda host: ["93.184.216.34"],
    )
    return settings_factory(
        database_path=str(tmp_path / "transfer-integration.sqlite3"),
        worker_enabled=False,
        allowed_hosts=["target.test", "testserver"],
        credentials_json=json.dumps(credentials),
        data_encryption_key_ref="key://transfer/data",
    )


@pytest.fixture
async def app_client(
    integration_settings: Any,
    target_api: TargetApi,
    monkeypatch: pytest.MonkeyPatch,
) -> Any:
    app = create_app(integration_settings)
    principal = Principal(
        tenant_id="tenant-a",
        subject="integration-test",
        scopes=frozenset(
            {
                "transfer:write",
                "transfer:read",
                "transfer:retry",
                "transfer:reconcile",
                "mapping:read",
                "mapping:write",
                "connector:read",
                "connector:admin",
            }
        ),
    )
    app.dependency_overrides[current_principal] = lambda: principal

    def target_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.ASGITransport(app=target_api.app)
        return _REAL_ASYNC_CLIENT(*args, **kwargs)

    monkeypatch.setattr(RestOpenApiConnector.__init__.__globals__["httpx"], "AsyncClient", target_client)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with _REAL_ASYNC_CLIENT(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            yield AppClient(app, client, target_api)


@pytest.mark.asyncio
async def test_api_key_upsert_create_update_and_idempotent_replay(
    app_client: AppClient,
    target_api: TargetApi,
) -> None:
    connector_id, mapping_id = await app_client.register(
        "target_openapi.yaml",
        "invoice-target",
        prefix="target",
        api_version="2026-01-01",
    )

    upsert_payload = invoice_payload(
        operation="upsert",
        suffix="upsert-001",
        connector_id=connector_id,
        mapping_id=mapping_id,
    )
    first = await app_client.post_transfer(upsert_payload, key="key-upsert-001")
    replay = await app_client.post_transfer(upsert_payload, key="key-upsert-001")
    assert first.status_code == 202
    assert replay.status_code == 202
    assert replay.headers["X-Idempotent-Replay"] == "true"
    completed = await app_client.wait_for_status(first.json()["transfer_id"], "succeeded")
    assert completed["result"]["target_resource_id"] == "target-resource-INV-upsert-001"

    create_payload = invoice_payload(
        operation="create",
        suffix="create-001",
        connector_id=connector_id,
        mapping_id=mapping_id,
    )
    created = await app_client.post_transfer(create_payload, key="key-create-001")
    created_status = await app_client.wait_for_status(
        created.json()["transfer_id"],
        "succeeded",
    )
    assert created_status["result"]["target_resource_id"]

    update_payload = invoice_payload(
        operation="update",
        suffix="create-001",
        connector_id=connector_id,
        mapping_id=mapping_id,
    )
    updated = await app_client.post_transfer(update_payload, key="key-update-001")
    updated_status = await app_client.wait_for_status(
        updated.json()["transfer_id"],
        "succeeded",
    )
    assert updated_status["result"]["target_resource_id"] == "target-resource-INV-create-001"
    assert target_api.count("POST", "target") == 1
    assert target_api.count("PUT", "target") == 2

    duplicate = await app_client.post_transfer(create_payload, key="key-create-duplicate")
    duplicate_status = await app_client.wait_for_status(
        duplicate.json()["transfer_id"],
        "failed",
    )
    assert duplicate_status["error"]["code"] == "HTTP_409"
    listed = await app_client.target_list("target", "INV-upsert-001")
    assert listed.status_code == 200
    assert listed.json()["items"][0]["id"] == "target-resource-INV-upsert-001"


@pytest.mark.asyncio
async def test_alt_fixture_uses_bearer_and_runs_crud(
    app_client: AppClient,
    target_api: TargetApi,
) -> None:
    connector_id, mapping_id = await app_client.register(
        "target_openapi_alt.yaml",
        "invoice-target-alt",
        prefix="alternate",
        api_version="2026-02-01",
    )
    for operation, suffix in (
        ("create", "alt-001"),
        ("update", "alt-001"),
        ("upsert", "alt-002"),
    ):
        payload = invoice_payload(
            operation=operation,
            suffix=suffix,
            variant="alt",
            connector_id=connector_id,
            mapping_id=mapping_id,
        )
        response = await app_client.post_transfer(payload, key=f"key-{operation}-{suffix}")
        status = await app_client.wait_for_status(response.json()["transfer_id"], "succeeded")
        assert status["result"]["target_resource_id"]
    assert target_api.auth_failures == 0
    assert target_api.count("POST", "alternate") == 1
    assert target_api.count("PUT", "alternate") == 2


@pytest.mark.parametrize(
    ("auth_mode", "prefix", "api_version", "connector_id"),
    [
        ("basic", "basic", "2026-03-01", "invoice-basic"),
        ("oauth2", "oauth", "2026-04-01", "invoice-oauth"),
    ],
)
@pytest.mark.asyncio
async def test_basic_and_oauth_credentials_use_existing_connector_auth(
    app_client: AppClient,
    target_api: TargetApi,
    auth_mode: str,
    prefix: str,
    api_version: str,
    connector_id: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    connector_id, mapping_id = await app_client.register(
        "target_openapi.yaml",
        connector_id,
        auth_mode=auth_mode,
        prefix=prefix,
        api_version=api_version,
    )
    payload = invoice_payload(
        operation="create",
        suffix=auth_mode,
        connector_id=connector_id,
        mapping_id=mapping_id,
    )
    response = await app_client.post_transfer(payload, key=f"key-{auth_mode}")
    completed = await app_client.wait_for_status(response.json()["transfer_id"], "succeeded")
    assert completed["result"]["target_resource_id"]
    assert target_api.auth_failures == 0
    if auth_mode == "oauth2":
        assert target_api.oauth_token_calls == 2
    for secret in SECRET_VALUES:
        assert secret not in caplog.text


@pytest.mark.asyncio
async def test_retrying_429_and_target_validation_error_are_observable(
    app_client: AppClient,
    target_api: TargetApi,
) -> None:
    connector_id, mapping_id = await app_client.register(
        "target_openapi.yaml",
        "invoice-retry",
        prefix="target",
        api_version="2026-01-01",
    )
    target_api.fail_next_429 = True
    retry_payload = invoice_payload(
        operation="upsert",
        suffix="retry-001",
        connector_id=connector_id,
        mapping_id=mapping_id,
    )
    retry_response = await app_client.post_transfer(retry_payload, key="key-retry-001")
    retrying = await app_client.wait_for_status(
        retry_response.json()["transfer_id"],
        "retrying",
    )
    assert retrying["error"]["code"] == "HTTP_429"
    succeeded = await app_client.wait_for_status(
        retry_response.json()["transfer_id"],
        "succeeded",
    )
    assert succeeded["result"]["target_resource_id"] == "target-resource-INV-retry-001"
    assert target_api.count("PUT", "target") == 2

    target_api.validation_error_next = True
    invalid_payload = invoice_payload(
        operation="upsert",
        suffix="validation-001",
        connector_id=connector_id,
        mapping_id=mapping_id,
    )
    invalid_response = await app_client.post_transfer(
        invalid_payload,
        key="key-validation-001",
    )
    failed = await app_client.wait_for_status(
        invalid_response.json()["transfer_id"],
        "failed",
    )
    assert failed["error"]["code"] == "HTTP_400"


@pytest.mark.asyncio
async def test_unknown_outcome_reconciles_absent_then_resends_and_present_without_resend(
    app_client: AppClient,
    target_api: TargetApi,
) -> None:
    connector_id, mapping_id = await app_client.register(
        "target_openapi.yaml",
        "invoice-reconcile",
        prefix="target",
        api_version="2026-01-01",
    )

    target_api.drop_next = "before_write"
    absent_payload = invoice_payload(
        operation="upsert",
        suffix="absent-001",
        connector_id=connector_id,
        mapping_id=mapping_id,
    )
    absent_response = await app_client.post_transfer(absent_payload, key="key-absent-001")
    absent_id = absent_response.json()["transfer_id"]
    absent = await app_client.wait_for_status(absent_id, "reconciliation_required")
    assert absent["error"]["code"] == "UNEXPECTED_ERROR"
    reconciled_absent = await app_client.reconcile_automatically(absent_id)
    assert reconciled_absent["status"] == "queued"
    assert target_api.count("GET", "target") >= 1
    resent = await app_client.wait_for_status(absent_id, "succeeded")
    assert resent["result"]["target_resource_id"]

    target_api.drop_next = "after_write"
    present_payload = invoice_payload(
        operation="upsert",
        suffix="present-001",
        connector_id=connector_id,
        mapping_id=mapping_id,
    )
    present_response = await app_client.post_transfer(present_payload, key="key-present-001")
    present_id = present_response.json()["transfer_id"]
    present = await app_client.wait_for_status(present_id, "reconciliation_required")
    target_id = target_api.invoices["target:INV-present-001"]["id"]
    confirmed = await app_client.reconcile_with_evidence(
        present_id,
        {
            "resolution": "confirmed_present",
            "target_resource_id": target_id,
            "notes": "target accepted the request before the response was lost",
        },
    )
    assert present["status"] == "reconciliation_required"
    assert confirmed["status"] == "succeeded"
    assert confirmed["result"]["target_resource_id"] == target_id
    assert target_api.count("PUT", "target") == 3


@pytest.mark.asyncio
async def test_restart_recovery_moves_delivering_to_reconciliation_required(
    app_client: AppClient,
    target_api: TargetApi,
) -> None:
    connector_id, mapping_id = await app_client.register(
        "target_openapi.yaml",
        "invoice-restart",
        prefix="target",
        api_version="2026-01-01",
    )
    payload = invoice_payload(
        operation="upsert",
        suffix="restart-001",
        connector_id=connector_id,
        mapping_id=mapping_id,
    )
    response = await app_client.post_transfer(payload, key="key-restart-001")
    transfer_id = response.json()["transfer_id"]
    assert await app_client.app.state.worker.run_once() is True
    claim = await app_client.app.state.store.claim_due_transfer(datetime.now(UTC))
    assert claim is not None
    assert claim.record.status == TransferStatus.DELIVERING

    recovered_worker = TransferWorker(
        app_client.app.state.store,
        app_client.app.state.registry,
        app_client.app.state.mapping_engine,
        RetryPolicy.from_settings(app_client.app.state.settings),
    )
    await recovered_worker.recover_inflight()
    recovered = await app_client.get_transfer(transfer_id)
    assert recovered["status"] == "reconciliation_required"
    assert target_api.count("PUT", "target") == 0
