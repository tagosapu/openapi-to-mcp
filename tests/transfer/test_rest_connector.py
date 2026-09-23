from __future__ import annotations

import asyncio
import base64
from datetime import UTC, datetime, timedelta
import hashlib
import json
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet
import httpx
import pytest

from src.transfer.mapping import MappingEngine
from src.transfer.models import ConnectorDefinition, MappingDefinition, OutboundRequestParts, TransferRequest
from tests.transfer.conftest import sample_connector_definition, sample_mapping, sample_transfer_request, settings_factory


BASE_URL = "https://93.184.216.34"
TOKEN_URL = "https://93.184.216.35/oauth/token"


class StaticCredentialResolver:
    def __init__(self, payloads: dict[str, dict[str, str]]) -> None:
        self._payloads = payloads

    async def resolve(self, credential_ref: str):
        from src.transfer.auth import SecretBundle

        return SecretBundle.model_validate({"values": self._payloads[credential_ref]})

    async def resolve_for_tenant(self, tenant_id: str, credential_ref: str):
        return await self.resolve(credential_ref)


def _request() -> TransferRequest:
    return TransferRequest.model_validate(sample_transfer_request())


def _mapping(*, operations: list[str] | None = None, target_schema_ref: str | None = None) -> MappingDefinition:
    payload = sample_mapping()
    if operations is not None:
        payload["operations"] = operations
    if target_schema_ref is not None:
        payload["target_schema_ref"] = target_schema_ref
    payload["rules"] = [
        {
            "rule_id": "invoice-id",
            "source": "/ocr/fields/invoice_number/value",
            "target": {"location": "body", "pointer": "/external_id"},
            "required": True,
            "on_missing": "error",
            "transforms": [],
        },
        {
            "rule_id": "invoice-query",
            "source": "/document/document_id",
            "target": {"location": "query", "name": "document_id"},
            "required": True,
            "on_missing": "error",
            "transforms": [],
        },
    ]
    return MappingDefinition.model_validate(payload)


def _base_spec(security_scheme: dict[str, Any], *, security_name: str = "auth", security: list[dict[str, list[str]]] | None = None) -> dict[str, Any]:
    return {
        "openapi": "3.1.0",
        "info": {"title": "Connector Spec", "version": "1.0.0"},
        "servers": [{"url": BASE_URL}],
        "paths": {
            "/invoices": {
                "post": {
                    "operationId": "createInvoice",
                    "security": security if security is not None else [{security_name: []}],
                    "parameters": [
                        {
                            "name": "X-Correlation-Id",
                            "in": "header",
                            "required": True,
                            "schema": {"type": "string"},
                        }
                    ],
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/InvoiceUpsertRequest"}
                            }
                        },
                    },
                    "responses": {
                        "201": {
                            "description": "Created",
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/InvoiceResponse"}
                                }
                            },
                        },
                        "400": {"description": "Bad request"},
                        "500": {"description": "Server error"},
                    },
                }
            },
            "/invoices/{invoiceId}": {
                "get": {
                    "operationId": "getInvoice",
                    "security": security if security is not None else [{security_name: []}],
                    "parameters": [
                        {
                            "name": "invoiceId",
                            "in": "path",
                            "required": True,
                            "schema": {"type": "string"},
                        }
                    ],
                    "responses": {
                        "200": {
                            "description": "Found",
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/InvoiceResponse"}
                                }
                            },
                        },
                        "404": {"description": "Not Found"},
                        "500": {"description": "Server error"},
                    },
                }
            },
        },
        "components": {
            "securitySchemes": {security_name: security_scheme},
            "schemas": {
                "InvoiceUpsertRequest": {
                    "type": "object",
                    "properties": {
                        "external_id": {"type": "string"},
                        "document_id": {"type": "string"},
                    },
                    "required": ["external_id"],
                },
                "InvoiceResponse": {
                    "type": "object",
                    "properties": {"id": {"type": "string"}},
                    "required": ["id"],
                },
            },
        },
    }


def _definition(
    spec: dict[str, Any],
    *,
    version: int = 1,
    operation_name: str = "create",
    policy: dict[str, Any] | None = None,
    base_url: str = BASE_URL,
) -> ConnectorDefinition:
    payload = sample_connector_definition()
    payload["version"] = version
    payload["spec"] = spec
    payload["base_url"] = base_url
    payload["operation_bindings"] = {
        operation_name: {
            "operation_id": "createInvoice",
            "idempotency_header": "Idempotency-Key",
            "lookup_operation_id": "getInvoice",
            "lookup_parameter_name": "invoiceId",
            "lookup_parameter_location": "path",
            "postcondition": {
                "reconcile_operation_id": "getInvoice",
                "result_id_pointer": "/id",
                "not_found_statuses": [404],
                "registered_statuses": [200, 201],
            },
        }
    }
    if policy is not None:
        payload["policy"] = policy
    return ConnectorDefinition.model_validate(payload)


class ChunkStream(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes], *, delay_seconds: float = 0.0) -> None:
        self._chunks = chunks
        self._delay_seconds = delay_seconds

    async def __aiter__(self):
        for chunk in self._chunks:
            if self._delay_seconds:
                await asyncio.sleep(self._delay_seconds)
            yield chunk

    async def aclose(self) -> None:
        return None


def _parts(*, operation_id: str = "createInvoice", headers: dict[str, str] | None = None) -> OutboundRequestParts:
    return OutboundRequestParts.model_validate(
        {
            "operation_id": operation_id,
            "method": "POST",
            "path": "/invoices",
            "path_params": {},
            "query_params": {"document_id": "doc-001"},
            "headers": {"X-Correlation-Id": "corr-001", **(headers or {})},
            "json_body": {"external_id": "INV-001"},
            "idempotency_key": "idem-001",
        }
    )


@pytest.mark.parametrize(
    ("scheme", "secrets", "expected_header", "expected_prefix"),
    [
        ({"type": "apiKey", "in": "header", "name": "X-Api-Key"}, {"auth": "secret-key", "config://headers/x-api-version": "2026-09-18"}, "X-Api-Key", "secret-key"),
        ({"type": "http", "scheme": "bearer"}, {"token": "bearer-token", "config://headers/x-api-version": "2026-09-18"}, "Authorization", "Bearer bearer-token"),
        ({"type": "http", "scheme": "basic"}, {"username": "alfa", "password": "bravo", "config://headers/x-api-version": "2026-09-18"}, "Authorization", "Basic "),
    ],
)
@pytest.mark.asyncio
async def test_build_request_applies_auth_and_protects_reserved_headers(
    scheme: dict[str, Any],
    secrets: dict[str, str],
    expected_header: str,
    expected_prefix: str,
) -> None:
    from src.transfer.rest_connector import RestOpenApiConnector

    connector = RestOpenApiConnector(
        _definition(_base_spec(scheme)),
        MappingEngine(),
        StaticCredentialResolver(
            {
                "vault://connectors/connector-test": secrets,
                "config://headers/x-api-version": {"value": secrets["config://headers/x-api-version"]},
            }
        ),
        httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(201))),
    )

    request = await connector.build_request(
        _parts(headers={"Authorization": "bad", "X-Correlation-Id": "corr-001"})
    )

    assert request.headers["X-Api-Version"] == "2026-09-18"
    assert request.headers[expected_header].startswith(expected_prefix)
    assert request.headers["Idempotency-Key"] == "idem-001"
    assert request.url.path == "/invoices"
    assert request.url.query == "document_id=doc-001"
    assert "bad" not in request.headers[expected_header]
    assert "secret-key" not in repr(request)


@pytest.mark.asyncio
async def test_build_request_supports_query_api_key_when_declared() -> None:
    from src.transfer.rest_connector import RestOpenApiConnector

    connector = RestOpenApiConnector(
        _definition(_base_spec({"type": "apiKey", "in": "query", "name": "api_key"})),
        MappingEngine(),
        StaticCredentialResolver(
            {
                "vault://connectors/connector-test": {"auth": "query-secret"},
                "config://headers/x-api-version": {"value": "2026-09-18"},
            }
        ),
        httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(201))),
    )

    request = await connector.build_request(_parts())

    assert "api_key=query-secret" in str(request.url)


@pytest.mark.asyncio
async def test_build_request_tries_openapi_security_options_in_order() -> None:
    from src.transfer.rest_connector import RestOpenApiConnector

    spec = _base_spec(
        {"type": "apiKey", "in": "header", "name": "X-Api-Key"},
        security_name="apiKey",
        security=[{"apiKey": []}, {"bearerAuth": []}],
    )
    spec["components"]["securitySchemes"]["bearerAuth"] = {
        "type": "http",
        "scheme": "bearer",
    }
    connector = RestOpenApiConnector(
        _definition(spec),
        MappingEngine(),
        StaticCredentialResolver(
            {
                "vault://connectors/connector-test": {
                    "bearerAuth": "bearer-token",
                    "unused": "not-an-api-key",
                },
                "config://headers/x-api-version": {"value": "2026-09-18"},
            }
        ),
        httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(201))),
    )

    request = await connector.build_request(_parts())

    assert request.headers["Authorization"] == "Bearer bearer-token"
    assert "X-Api-Key" not in request.headers


@pytest.mark.asyncio
async def test_build_request_preserves_base_url_path_prefix() -> None:
    from src.transfer.rest_connector import RestOpenApiConnector

    spec = _base_spec({"type": "http", "scheme": "bearer"})
    spec["servers"] = [{"url": f"{BASE_URL}/v1"}]
    connector = RestOpenApiConnector(
        _definition(spec, base_url=f"{BASE_URL}/v1"),
        MappingEngine(),
        StaticCredentialResolver(
            {
                "vault://connectors/connector-test": {"token": "bearer-token"},
                "config://headers/x-api-version": {"value": "2026-09-18"},
            }
        ),
        httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(201))),
    )

    request = await connector.build_request(_parts())

    assert str(request.url) == f"{BASE_URL}/v1/invoices?document_id=doc-001"


@pytest.mark.asyncio
async def test_validate_payload_uses_request_schema_and_target_schema_refs() -> None:
    from src.transfer.rest_connector import RestOpenApiConnector

    mapping = _mapping(
        operations=["create"],
        target_schema_ref="openapi:#/operations/createInvoice/request_schema",
    )
    connector = RestOpenApiConnector(
        _definition(_base_spec({"type": "http", "scheme": "bearer"})),
        MappingEngine(),
        StaticCredentialResolver(
            {
                "vault://connectors/connector-test": {"token": "bearer-token"},
                "config://headers/x-api-version": {"value": "2026-09-18"},
            }
        ),
        httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(201))),
    )

    operation = await connector.resolve_operation("create")
    result = await connector.validate_payload(_request(), mapping, operation)

    assert result.valid is True
    assert result.issues == []


@pytest.mark.asyncio
async def test_send_refreshes_oauth_token_once_on_expired_401() -> None:
    from src.transfer.rest_connector import RestOpenApiConnector

    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if request.url.path == "/oauth/token":
            token_value = "expired-token" if calls.count(TOKEN_URL) == 1 else "fresh-token"
            return httpx.Response(200, json={"access_token": token_value, "expires_in": 3600})
        if request.headers.get("Authorization") == "Bearer expired-token":
            return httpx.Response(
                401,
                headers={"WWW-Authenticate": 'Bearer error="invalid_token", error_description="expired"'},
                json={"detail": "expired"},
            )
        return httpx.Response(201, json={"id": "inv-001"}, headers={"X-Request-Id": "req-1"})

    spec = _base_spec(
        {
            "type": "oauth2",
            "flows": {
                "clientCredentials": {"tokenUrl": TOKEN_URL, "scopes": {}}
            },
        }
    )
    connector = RestOpenApiConnector(
        _definition(spec),
        MappingEngine(),
        StaticCredentialResolver(
            {
                "vault://connectors/connector-test": {
                    "client_id": "client-id",
                    "client_secret": "client-secret",
                },
                "config://headers/x-api-version": {"value": "2026-09-18"},
            }
        ),
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )

    request = await connector.build_request(_parts())
    outcome = await connector.send(request)

    assert outcome.delivery_state == "received"
    assert outcome.status_code == 201
    assert calls.count(TOKEN_URL) == 2
    assert connector.parse_response(outcome).target_resource_id == "inv-001"


@pytest.mark.asyncio
async def test_build_request_rejects_oauth_token_ssrf() -> None:
    from src.transfer.rest_connector import RestOpenApiConnector

    spec = _base_spec(
        {
            "type": "oauth2",
            "flows": {
                "clientCredentials": {
                    "tokenUrl": "http://169.254.169.254/oauth/token",
                    "scopes": {},
                }
            },
        }
    )
    connector = RestOpenApiConnector(
        _definition(spec),
        MappingEngine(),
        StaticCredentialResolver(
            {
                "vault://connectors/connector-test": {
                    "client_id": "client-id",
                    "client_secret": "client-secret",
                },
                "config://headers/x-api-version": {"value": "2026-09-18"},
            }
        ),
        httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200))),
    )

    with pytest.raises(ValueError, match="SSRF_ADDRESS_BLOCKED"):
        await connector.build_request(_parts())


@pytest.mark.asyncio
async def test_send_rejects_redirects_and_response_size_limit() -> None:
    from src.transfer.rest_connector import RestOpenApiConnector

    connector = RestOpenApiConnector(
        _definition(
            _base_spec({"type": "http", "scheme": "bearer"}),
            policy={
                "connect_timeout_seconds": 5,
                "read_timeout_seconds": 30,
                "total_timeout_seconds": 60,
                "max_response_bytes": 8,
                "max_redirects": 0,
            },
        ),
        MappingEngine(),
        StaticCredentialResolver(
            {
                "vault://connectors/connector-test": {"token": "bearer-token"},
                "config://headers/x-api-version": {"value": "2026-09-18"},
            }
        ),
        httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(302, headers={"Location": "https://api.example.com/elsewhere"})
                if request.url.params.get("document_id") == "redirect"
                else httpx.Response(200, content=b"0123456789")
            )
        ),
    )

    redirect_parts = _parts()
    redirect_parts.query_params["document_id"] = "redirect"
    redirect_outcome = await connector.send(await connector.build_request(redirect_parts))

    assert redirect_outcome.status_code == 302
    assert connector.classify_error(redirect_outcome).code == "HTTP_REDIRECT_BLOCKED"

    large_outcome = await connector.send(await connector.build_request(_parts()))
    assert connector.classify_error(large_outcome).code == "RESPONSE_TOO_LARGE"


@pytest.mark.asyncio
async def test_send_enforces_streaming_response_size_limit_before_full_buffering() -> None:
    from src.transfer.rest_connector import RestOpenApiConnector

    connector = RestOpenApiConnector(
        _definition(
            _base_spec({"type": "http", "scheme": "bearer"}),
            policy={
                "connect_timeout_seconds": 5,
                "read_timeout_seconds": 30,
                "total_timeout_seconds": 60,
                "max_response_bytes": 8,
                "max_redirects": 0,
            },
        ),
        MappingEngine(),
        StaticCredentialResolver(
            {
                "vault://connectors/connector-test": {"token": "bearer-token"},
                "config://headers/x-api-version": {"value": "2026-09-18"},
            }
        ),
        httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    headers={"content-type": "application/json"},
                    stream=ChunkStream([b'{"id":', b'"inv-001"}']),
                )
            )
        ),
    )

    outcome = await connector.send(await connector.build_request(_parts()))

    assert outcome.delivery_state == "received"
    assert connector.classify_error(outcome).code == "RESPONSE_TOO_LARGE"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(201, headers={"content-type": "application/json"}, content=b"{"),
        httpx.Response(201, json={"id": 123}),
    ],
)
async def test_send_marks_unparseable_or_schema_invalid_success_body_as_unknown(
    response: httpx.Response,
) -> None:
    from src.transfer.rest_connector import RestOpenApiConnector

    connector = RestOpenApiConnector(
        _definition(_base_spec({"type": "http", "scheme": "bearer"})),
        MappingEngine(),
        StaticCredentialResolver(
            {
                "vault://connectors/connector-test": {"token": "bearer-token"},
                "config://headers/x-api-version": {"value": "2026-09-18"},
            }
        ),
        httpx.AsyncClient(transport=httpx.MockTransport(lambda request: response)),
    )

    outcome = await connector.send(await connector.build_request(_parts()))

    assert outcome.delivery_state == "unknown"
    assert connector.classify_error(outcome).code == "RESPONSE_FORMAT_UNKNOWN"


@pytest.mark.asyncio
async def test_send_applies_total_deadline_to_response_body_read() -> None:
    from src.transfer.rest_connector import RestOpenApiConnector

    connector = RestOpenApiConnector(
        _definition(
            _base_spec({"type": "http", "scheme": "bearer"}),
            policy={
                "connect_timeout_seconds": 5,
                "read_timeout_seconds": 5,
                "total_timeout_seconds": 0.01,
                "max_response_bytes": 1024,
                "max_redirects": 0,
            },
        ),
        MappingEngine(),
        StaticCredentialResolver(
            {
                "vault://connectors/connector-test": {"token": "bearer-token"},
                "config://headers/x-api-version": {"value": "2026-09-18"},
            }
        ),
        httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    headers={"content-type": "application/json"},
                    stream=ChunkStream([b'{"id":"', b'inv-001"}'], delay_seconds=0.02),
                )
            )
        ),
    )

    outcome = await connector.send(await connector.build_request(_parts()))

    assert outcome.delivery_state == "unknown"
    assert connector.classify_error(outcome).code == "DELIVERY_UNKNOWN"


@pytest.mark.asyncio
async def test_send_and_classify_timeout_unknown_429_and_5xx() -> None:
    from src.transfer.rest_connector import RestOpenApiConnector

    connector = RestOpenApiConnector(
        _definition(_base_spec({"type": "http", "scheme": "bearer"})),
        MappingEngine(),
        StaticCredentialResolver(
            {
                "vault://connectors/connector-test": {"token": "bearer-token"},
                "config://headers/x-api-version": {"value": "2026-09-18"},
            }
        ),
        httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: (_ for _ in ()).throw(httpx.ReadTimeout("timed out", request=request))
            )
        ),
    )

    timeout_outcome = await connector.send(await connector.build_request(_parts()))
    assert timeout_outcome.delivery_state == "unknown"
    assert connector.classify_error(timeout_outcome).code == "DELIVERY_UNKNOWN"

    rate_limited = connector.classify_error(
        connector._build_outcome(
            httpx.Response(429, headers={"Retry-After": "7"}, json={"detail": "slow down"}),
            body_bytes=httpx.Response(429, headers={"Retry-After": "7"}, json={"detail": "slow down"}).content,
        )
    )
    server_error = connector.classify_error(
        connector._build_outcome(
            httpx.Response(503, json={"detail": "down"}),
            body_bytes=httpx.Response(503, json={"detail": "down"}).content,
        )
    )

    assert rate_limited.retryable is True
    assert rate_limited.retry_after_seconds == 7
    for status_code in (408, 425):
        retryable = connector.classify_error(
            connector._build_outcome(
                httpx.Response(status_code, headers={"Retry-After": "3"}),
                body_bytes=b"",
            )
        )
        assert retryable.retryable is True
        assert retryable.retry_after_seconds == 3
    assert server_error.code == "HTTP_5XX"


@pytest.mark.asyncio
async def test_send_rechecks_ssrf_and_detects_dns_rebinding(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.transfer.rest_connector import RestOpenApiConnector

    addresses = [["93.184.216.34"], ["169.254.169.254"]]

    monkeypatch.setattr(
        "src.transfer.rest_connector.resolve_host_addresses",
        lambda host: addresses.pop(0),
    )

    connector = RestOpenApiConnector(
        _definition(_base_spec({"type": "http", "scheme": "bearer"})),
        MappingEngine(),
        StaticCredentialResolver(
            {
                "vault://connectors/connector-test": {"token": "bearer-token"},
                "config://headers/x-api-version": {"value": "2026-09-18"},
            }
        ),
        httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(201))),
    )

    outcome = await connector.send(await connector.build_request(_parts()))

    assert outcome.delivery_state == "not_sent"
    assert connector.classify_error(outcome).code == "SSRF_ADDRESS_BLOCKED"


@pytest.mark.asyncio
async def test_parse_response_rejects_invalid_result_id() -> None:
    from src.transfer.rest_connector import RestOpenApiConnector

    connector = RestOpenApiConnector(
        _definition(_base_spec({"type": "http", "scheme": "bearer"})),
        MappingEngine(),
        StaticCredentialResolver(
            {
                "vault://connectors/connector-test": {"token": "bearer-token"},
                "config://headers/x-api-version": {"value": "2026-09-18"},
            }
        ),
        httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(201))),
    )
    connector._last_operation_id = "createInvoice"
    response = httpx.Response(201, json={"id": ""})
    outcome = connector._build_outcome(response, body_bytes=response.content)

    with pytest.raises(ValueError, match="RESPONSE_ID_INVALID"):
        connector.parse_response(outcome)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "expected_state"),
    [
        (httpx.Response(200, json={"id": "inv-001"}), "registered"),
        (httpx.Response(404), "not_registered"),
        (httpx.Response(503), "unknown"),
    ],
)
async def test_reconcile_returns_registered_not_registered_or_unknown(
    response: httpx.Response,
    expected_state: str,
) -> None:
    from src.transfer.connector import ReconciliationContext
    from src.transfer.rest_connector import RestOpenApiConnector

    connector = RestOpenApiConnector(
        _definition(_base_spec({"type": "http", "scheme": "bearer"})),
        MappingEngine(),
        StaticCredentialResolver(
            {
                "vault://connectors/connector-test": {"token": "bearer-token"},
                "config://headers/x-api-version": {"value": "2026-09-18"},
            }
        ),
        httpx.AsyncClient(transport=httpx.MockTransport(lambda request: response)),
    )

    result = await connector.reconcile(
        ReconciliationContext(
            transfer_id="tr-1",
            idempotency_key="idem-1",
            deduplication_value='"INV-001"',
            operation_id="createInvoice",
            mode="unknown",
            target_resource_id="inv-001",
        )
    )

    assert result.state == expected_state


@pytest.mark.asyncio
async def test_reconcile_coerces_integer_lookup_path_value() -> None:
    from src.transfer.connector import ReconciliationContext
    from src.transfer.rest_connector import RestOpenApiConnector

    observed_paths: list[str] = []
    spec = _base_spec({"type": "http", "scheme": "bearer"})
    spec["paths"] = {
        "/invoices": spec["paths"]["/invoices"],
        "/lookup/{invoiceId}": {
            "get": {
                "operationId": "getInvoice",
                "security": [{"auth": []}],
                "parameters": [
                    {
                        "name": "invoiceId",
                        "in": "path",
                        "required": True,
                        "schema": {"type": "integer"},
                    }
                ],
                "responses": {
                    "200": {
                        "description": "Found",
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/InvoiceResponse"}
                            }
                        },
                    },
                    "404": {"description": "Not found"},
                    "500": {"description": "Server error"},
                },
            }
        },
    }
    connector = RestOpenApiConnector(
        _definition(spec),
        MappingEngine(),
        StaticCredentialResolver(
            {
                "vault://connectors/connector-test": {"token": "bearer-token"},
                "config://headers/x-api-version": {"value": "2026-09-18"},
            }
        ),
        httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: observed_paths.append(request.url.path) or httpx.Response(404)
            )
        ),
    )

    result = await connector.reconcile(
        ReconciliationContext(
            transfer_id="tr-1",
            idempotency_key="idem-1",
            deduplication_value="123",
            operation_id="createInvoice",
            mode="unknown",
            target_resource_id=None,
        )
    )

    assert result.state == "not_registered"
    assert observed_paths == ["/lookup/123"]


@pytest.mark.asyncio
async def test_reconcile_coerces_boolean_lookup_query_value() -> None:
    from src.transfer.connector import ReconciliationContext
    from src.transfer.rest_connector import RestOpenApiConnector

    observed_queries: list[str] = []
    spec = _base_spec({"type": "http", "scheme": "bearer"})
    spec["paths"] = {
        "/invoices": spec["paths"]["/invoices"],
        "/lookup": {
            "get": {
                "operationId": "getInvoice",
                "security": [{"auth": []}],
                "parameters": [
                    {
                        "name": "enabled",
                        "in": "query",
                        "required": True,
                        "schema": {"type": "boolean"},
                    }
                ],
                "responses": {
                    "200": {
                        "description": "Found",
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/InvoiceResponse"}
                            }
                        },
                    },
                    "404": {"description": "Not found"},
                    "500": {"description": "Server error"},
                },
            }
        },
    }
    payload = _definition(spec).model_dump(mode="json")
    payload["operation_bindings"]["create"]["lookup_parameter_name"] = "enabled"
    payload["operation_bindings"]["create"]["lookup_parameter_location"] = "query"
    connector = RestOpenApiConnector(
        ConnectorDefinition.model_validate(payload),
        MappingEngine(),
        StaticCredentialResolver(
            {
                "vault://connectors/connector-test": {"token": "bearer-token"},
                "config://headers/x-api-version": {"value": "2026-09-18"},
            }
        ),
        httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: observed_queries.append(request.url.query.decode()) or httpx.Response(404)
            )
        ),
    )

    result = await connector.reconcile(
        ReconciliationContext(
            transfer_id="tr-1",
            idempotency_key="idem-1",
            deduplication_value="true",
            operation_id="createInvoice",
            mode="unknown",
            target_resource_id=None,
        )
    )

    assert result.state == "not_registered"
    assert observed_queries == ["enabled=true"]


@pytest.mark.asyncio
async def test_reconcile_rejects_invalid_lookup_value_without_dispatch() -> None:
    from src.transfer.connector import ReconciliationContext
    from src.transfer.rest_connector import RestOpenApiConnector

    dispatched = False
    spec = _base_spec({"type": "http", "scheme": "bearer"})
    spec["paths"]["/invoices/{invoiceId}"]["get"]["parameters"][0]["schema"] = {"type": "integer"}
    async def never_called(request: httpx.Request) -> httpx.Response:
        nonlocal dispatched
        dispatched = True
        return httpx.Response(200)

    connector = RestOpenApiConnector(
        _definition(spec),
        MappingEngine(),
        StaticCredentialResolver(
            {
                "vault://connectors/connector-test": {"token": "bearer-token"},
                "config://headers/x-api-version": {"value": "2026-09-18"},
            }
        ),
        httpx.AsyncClient(transport=httpx.MockTransport(never_called)),
    )

    result = await connector.reconcile(
        ReconciliationContext(
            transfer_id="tr-1",
            idempotency_key="idem-1",
            deduplication_value='"not-an-int"',
            operation_id="createInvoice",
            mode="unknown",
            target_resource_id=None,
        )
    )

    assert result.state == "unknown"
    assert dispatched is False


@pytest.mark.asyncio
async def test_registry_rejects_missing_reconciliation_configuration_and_invalid_result_pointer(
    tmp_path: Path,
) -> None:
    from src.transfer.rest_connector import ConnectorRegistry
    from src.transfer.store import SqliteTransferStore

    bad = sample_connector_definition()
    bad["base_url"] = BASE_URL
    bad["spec"] = _base_spec({"type": "http", "scheme": "bearer"})
    bad["operation_bindings"] = {"create": {"operation_id": "createInvoice"}}

    bad_pointer = sample_connector_definition()
    bad_pointer["base_url"] = BASE_URL
    bad_pointer["spec"] = _base_spec({"type": "http", "scheme": "bearer"})
    bad_pointer["operation_bindings"] = {
        "create": {
            "operation_id": "createInvoice",
            "lookup_operation_id": "getInvoice",
            "lookup_parameter_name": "invoiceId",
            "lookup_parameter_location": "path",
            "postcondition": {
                "reconcile_operation_id": "getInvoice",
                "result_id_pointer": "/missing",
                "not_found_statuses": [404],
                "registered_statuses": [200, 201],
            },
        }
    }

    store = SqliteTransferStore(
        tmp_path / "registry.sqlite3", allow_legacy_plaintext=True
    )
    await store.initialize()
    try:
        registry = ConnectorRegistry(
            store,
            StaticCredentialResolver(
                {
                    "vault://connectors/connector-test": {"token": "bearer-token"},
                    "config://headers/x-api-version": {"value": "2026-09-18"},
                    "key://tests/transfer": {"value": Fernet.generate_key().decode("ascii")},
                }
            ),
            settings_factory(allowed_hosts=["93.184.216.34"]),
        )

        with pytest.raises(ValueError, match="RECONCILIATION_NOT_CONFIGURED"):
            await registry.register("tenant-a", ConnectorDefinition.model_validate(bad))
        with pytest.raises(ValueError, match="RESULT_ID_POINTER_INVALID"):
            await registry.register("tenant-a", ConnectorDefinition.model_validate(bad_pointer))
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_registry_is_tenant_scoped_and_version_pinned(tmp_path: Path) -> None:
    from src.transfer.errors import NotFoundError
    from src.transfer.rest_connector import ConnectorRegistry
    from src.transfer.store import SqliteTransferStore

    store = SqliteTransferStore(
        tmp_path / "registry-version.sqlite3", allow_legacy_plaintext=True
    )
    await store.initialize()
    try:
        registry = ConnectorRegistry(
            store,
            StaticCredentialResolver(
                {
                    "vault://connectors/connector-test": {"token": "bearer-token"},
                    "config://headers/x-api-version": {"value": "2026-09-18"},
                    "key://tests/transfer": {"value": Fernet.generate_key().decode("ascii")},
                }
            ),
            settings_factory(allowed_hosts=["93.184.216.34"]),
        )
        await registry.register(
            "tenant-a",
            _definition(_base_spec({"type": "http", "scheme": "bearer"}), version=1),
        )
        second_spec = _base_spec({"type": "http", "scheme": "bearer"})
        second_spec["paths"]["/invoices"]["post"]["responses"]["202"] = {"description": "Accepted"}
        await registry.register("tenant-a", _definition(second_spec, version=2))

        current = await registry.resolve_operation("tenant-a", "connector-test", 2, "create")
        pinned = await registry.resolve_operation("tenant-a", "connector-test", 1, "create")

        assert current.operation_id == "createInvoice"
        assert pinned.operation_id == "createInvoice"
        with pytest.raises(NotFoundError):
            await registry.get("tenant-b", "connector-test")
    finally:
        await store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutate",
    [
        lambda spec, payload: spec["components"]["schemas"]["InvoiceUpsertRequest"]["required"].append("document_id"),
        lambda spec, payload: spec["components"]["securitySchemes"].__setitem__("auth", {"type": "http", "scheme": "basic"}),
        lambda spec, payload: payload.__setitem__("display_name", "Connector Test Updated"),
    ],
)
async def test_registry_rejects_same_version_material_changes_but_allows_identical_reregistration(
    tmp_path: Path,
    mutate,
) -> None:
    from src.transfer.rest_connector import ConnectorRegistry
    from src.transfer.store import SqliteTransferStore

    store = SqliteTransferStore(
        tmp_path / "registry-immutability.sqlite3", allow_legacy_plaintext=True
    )
    await store.initialize()
    try:
        registry = ConnectorRegistry(
            store,
            StaticCredentialResolver(
                {
                    "vault://connectors/connector-test": {"token": "bearer-token"},
                    "config://headers/x-api-version": {"value": "2026-09-18"},
                    "config://headers/x-region": {"value": "jp-east"},
                    "key://tests/transfer": {"value": Fernet.generate_key().decode("ascii")},
                }
            ),
            settings_factory(allowed_hosts=["93.184.216.34"]),
        )
        original = _definition(_base_spec({"type": "http", "scheme": "bearer"}), version=1)

        await registry.register("tenant-a", original)
        await registry.register("tenant-a", original)

        connection = store._require_connection()
        cursor = await connection.execute(
            "SELECT spec_json, spec_hash, COUNT(*) AS count FROM connectors WHERE tenant_id = ? AND connector_id = ? AND version = ?",
            ("tenant-a", "connector-test", 1),
        )
        row = await cursor.fetchone()
        await cursor.close()

        saved_spec = json.loads(row["spec_json"])
        canonical_hash = hashlib.sha256(
            json.dumps(original.spec, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        ).hexdigest()

        assert row["count"] == 1
        assert saved_spec["x-openapi-to-mcp-registration-hosts"] == ["93.184.216.34"]
        assert row["spec_hash"] == canonical_hash

        changed_payload = original.model_dump(mode="json")
        changed_spec = changed_payload["spec"]
        mutate(changed_spec, changed_payload)

        with pytest.raises(ValueError, match="CONNECTOR_VERSION_IMMUTABLE"):
            await registry.register("tenant-a", ConnectorDefinition.model_validate(changed_payload))
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_registry_rejects_cross_tenant_credential_reference(tmp_path: Path) -> None:
    from src.transfer.auth import EnvironmentCredentialResolver
    from src.transfer.rest_connector import ConnectorRegistry
    from src.transfer.store import SqliteTransferStore

    settings = settings_factory(
        allowed_hosts=["93.184.216.34"],
        credentials_json=json.dumps(
            {
                "tenants": {
                    "tenant-a": {
                        "vault://connectors/connector-test": {"token": "tenant-a-token"},
                        "config://headers/x-api-version": {"value": "2026-09-18"},
                    }
                }
            }
        ),
    )
    store = SqliteTransferStore(tmp_path / "registry-tenant-credentials.sqlite3", allow_legacy_plaintext=True)
    await store.initialize()
    try:
        registry = ConnectorRegistry(store, EnvironmentCredentialResolver(settings), settings)
        definition = _definition(_base_spec({"type": "http", "scheme": "bearer"}))

        await registry.register("tenant-a", definition)
        with pytest.raises(RuntimeError, match="credential ref not found for tenant"):
            await registry.register("tenant-b", definition)
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_environment_credentials_are_tenant_scoped() -> None:
    from src.transfer.auth import EnvironmentCredentialResolver

    settings = settings_factory(
        credentials_json=json.dumps(
            {
                "tenants": {
                    "tenant-a": {
                        "vault://connectors/connector-test": {"token": "tenant-a-token"},
                    }
                },
                "global": {"key://transfer/data": {"value": "global-key"}},
            }
        )
    )
    resolver = EnvironmentCredentialResolver(settings)

    bundle = await resolver.resolve_for_tenant(
        "tenant-a",
        "vault://connectors/connector-test",
    )
    assert bundle.values["token"].get_secret_value() == "tenant-a-token"
    with pytest.raises(RuntimeError, match="credential ref not found for tenant"):
        await resolver.resolve_for_tenant("tenant-b", "vault://connectors/connector-test")
    global_bundle = await resolver.resolve("key://transfer/data")
    assert global_bundle.values["value"].get_secret_value() == "global-key"


@pytest.mark.asyncio
async def test_create_payload_protector_requires_valid_fernet_key() -> None:
    from src.transfer.auth import SecretBundle
    from src.transfer.store import create_payload_protector

    class Resolver:
        async def resolve(self, credential_ref: str) -> SecretBundle:
            payload = {"value": Fernet.generate_key().decode("ascii")} if credential_ref == "key://valid" else {"value": "bad-key"}
            return SecretBundle.model_validate({"values": payload})

    settings = settings_factory(data_encryption_key_ref="key://valid")
    protector = await create_payload_protector(settings, Resolver())
    ciphertext = protector.encrypt(b"payload")
    assert protector.decrypt(ciphertext) == b"payload"

    bad_settings = settings_factory(data_encryption_key_ref="key://invalid")
    with pytest.raises(RuntimeError, match="invalid Fernet key"):
        await create_payload_protector(bad_settings, Resolver())


def test_secret_bundle_and_request_repr_redact_secrets() -> None:
    from src.transfer.auth import SecretBundle
    from src.transfer.connector import OutboundRequest

    secret = "super-secret"
    bundle = SecretBundle.model_validate({"values": {"token": secret}})
    request = OutboundRequest(
        method="POST",
        url="https://api.example.com/invoices",
        headers={"Authorization": f"Bearer {secret}"},
        json_body=None,
    )

    assert secret not in repr(bundle)
    assert secret not in repr(request)