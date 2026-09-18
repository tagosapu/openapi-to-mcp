from __future__ import annotations

import json
from typing import Any

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from starlette.requests import Request

from src.transfer.app import create_app, current_principal
from src.transfer.auth import Principal
from src.transfer.limits import RateLimiter
from src.transfer.models import TransferStatus
from src.transfer.routes import ApiError, _read_json_body
from tests.transfer.conftest import (
    sample_connector_definition,
    sample_mapping,
    sample_transfer_request,
    settings_factory,
)


@pytest.fixture
def test_settings(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    encryption_key = Fernet.generate_key().decode("ascii")
    monkeypatch.setattr(
        "src.transfer.rest_connector.resolve_host_addresses",
        lambda host: ["93.184.216.34"],
    )
    return settings_factory(
        database_path=str(tmp_path / "transfer-api.sqlite3"),
        worker_enabled=False,
        allowed_hosts=["api.example.com", "localhost", "testserver"],
        credentials_json=json.dumps(
            {
                "connector-test": "vault://connectors/connector-test",
                "key://tests/transfer": encryption_key,
            }
        ),
    )


@pytest.fixture
def app(test_settings: Any) -> Any:
    return create_app(test_settings)


@pytest.fixture
def client(app: Any) -> TestClient:
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def principal() -> Principal:
    return Principal(
        tenant_id="tenant-a",
        subject="subject-a",
        scopes=frozenset(
            {
                "transfer:write",
                "transfer:read",
                "transfer:retry",
                "transfer:cancel",
                "transfer:review",
                "transfer:reconcile",
                "mapping:read",
                "mapping:write",
                "connector:read",
                "connector:admin",
            }
        ),
    )


@pytest.fixture
def authenticated_client(client: TestClient, app: Any, principal: Principal) -> TestClient:
    app.dependency_overrides[current_principal] = lambda: principal
    return client


def _seed_dependencies(authenticated_client: TestClient) -> None:
    connector_response = authenticated_client.post(
        "/v1/connectors",
        json=_valid_connector_definition(),
    )
    assert connector_response.status_code == 201
    mapping_response = authenticated_client.post(
        "/v1/mappings",
        json=sample_mapping(),
    )
    assert mapping_response.status_code == 201


def _valid_connector_definition() -> dict[str, Any]:
    connector = sample_connector_definition()
    operation_bindings = connector.pop("operation_bindings")
    connector.pop("version")
    connector.pop("spec_ref")
    connector["spec"] = {
        "openapi": "3.1.0",
        "info": {"title": "Connector Test", "version": "1.0.0"},
        "servers": [{"url": "https://api.example.com"}],
        "paths": {
            "/invoices/{invoiceId}": {
                "put": {
                    "operationId": "upsertInvoice",
                    "security": [{"bearerAuth": []}],
                    "parameters": [
                        {
                            "name": "invoiceId",
                            "in": "path",
                            "required": True,
                            "schema": {"type": "string"},
                        }
                    ],
                    "requestBody": {
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/InvoiceUpsertRequest"}
                            }
                        }
                    },
                    "responses": {
                        "200": {
                            "description": "Updated",
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/InvoiceResponse"}
                                }
                            },
                        },
                        "400": {"description": "Bad request"},
                        "500": {"description": "Server error"},
                    },
                },
                "get": {
                    "operationId": "getInvoice",
                    "security": [{"bearerAuth": []}],
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
                        "404": {"description": "Not found"},
                        "500": {"description": "Server error"},
                    },
                },
            }
        },
        "components": {
            "securitySchemes": {"bearerAuth": {"type": "http", "scheme": "bearer"}},
            "schemas": {
                "InvoiceUpsertRequest": {
                    "type": "object",
                    "properties": {"external_id": {"type": "string"}},
                    "required": ["external_id"],
                },
                "InvoiceResponse": {
                    "type": "object",
                    "properties": {"id": {"type": "string"}},
                    "required": ["id"],
                },
            },
        },
        "x-openapi-to-mcp-operation-bindings": operation_bindings,
    }
    return connector


def test_create_connector_accepts_canonical_public_shape(
    authenticated_client: TestClient,
) -> None:
    response = authenticated_client.post(
        "/v1/connectors",
        json=_valid_connector_definition(),
    )

    assert response.status_code == 201
    assert response.json()["display_name"] == "Connector Test"
    assert response.json()["capabilities"] == ["upsert"]


def test_create_connector_rejects_internal_fields_and_unresolved_spec_ref(
    authenticated_client: TestClient,
) -> None:
    internal_fields = _valid_connector_definition()
    internal_fields["version"] = 7
    rejected = authenticated_client.post("/v1/connectors", json=internal_fields)

    unresolved = _valid_connector_definition()
    unresolved.pop("spec")
    unresolved["spec_ref"] = "finance-api:2026-09-01"
    unresolved_response = authenticated_client.post("/v1/connectors", json=unresolved)

    assert rejected.status_code == 422
    assert rejected.json()["code"] == "REQUEST_INVALID"
    assert unresolved_response.status_code == 422
    assert unresolved_response.json()["code"] == "SPEC_SNAPSHOT_UNRESOLVED"


def test_transfer_write_requires_scope(client: TestClient, app: Any) -> None:
    app.dependency_overrides[current_principal] = lambda: Principal(
        tenant_id="tenant-a",
        subject="subject-a",
        scopes=frozenset({"transfer:read"}),
    )

    response = client.post(
        "/v1/transfers",
        headers={"Idempotency-Key": "idem-001"},
        json=sample_transfer_request(),
    )

    assert response.status_code == 403
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["code"] == "FORBIDDEN"


def test_transfer_status_cannot_cross_tenant(
    authenticated_client: TestClient,
) -> None:
    _seed_dependencies(authenticated_client)
    payload = sample_transfer_request()
    created = authenticated_client.post(
        "/v1/transfers",
        headers={"Idempotency-Key": "idem-tenant-a"},
        json=payload,
    )
    assert created.status_code == 202
    transfer_id = created.json()["transfer_id"]

    authenticated_client.app.dependency_overrides[current_principal] = lambda: Principal(
        tenant_id="tenant-b",
        subject="subject-b",
        scopes=frozenset({"transfer:read"}),
    )

    response = authenticated_client.get(f"/v1/transfers/{transfer_id}")

    assert response.status_code == 404


def test_create_transfer_returns_accepted_and_reuses_pinned_versions(
    authenticated_client: TestClient,
) -> None:
    _seed_dependencies(authenticated_client)

    first = authenticated_client.post(
        "/v1/transfers",
        headers={"Idempotency-Key": "idem-replay"},
        json=sample_transfer_request(),
    )
    replay = authenticated_client.post(
        "/v1/transfers",
        headers={"Idempotency-Key": "idem-replay"},
        json=sample_transfer_request(),
    )

    assert first.status_code == 202
    assert first.json()["status"] == TransferStatus.ACCEPTED.value
    assert first.json()["mapping_version"] == 3
    assert replay.status_code == 202
    assert replay.headers["x-idempotent-replay"] == "true"
    assert replay.json()["transfer_id"] == first.json()["transfer_id"]


def test_create_transfer_rejects_idempotency_conflict(
    authenticated_client: TestClient,
) -> None:
    _seed_dependencies(authenticated_client)

    first = authenticated_client.post(
        "/v1/transfers",
        headers={"Idempotency-Key": "idem-conflict"},
        json=sample_transfer_request(),
    )
    changed = sample_transfer_request()
    changed["document"]["document_id"] = "doc-changed"
    response = authenticated_client.post(
        "/v1/transfers",
        headers={"Idempotency-Key": "idem-conflict"},
        json=changed,
    )

    assert first.status_code == 202
    assert response.status_code == 409
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["code"] == "IDEMPOTENCY_CONFLICT"


def test_mapping_preview_uses_registered_operation_without_sending(
    authenticated_client: TestClient,
) -> None:
    _seed_dependencies(authenticated_client)
    payload = sample_transfer_request()

    response = authenticated_client.post(
        "/v1/mappings/mapping-test/preview",
        json={
            "connector_id": "connector-test",
            "operation": "upsert",
            "document": payload["document"],
            "ocr": payload["ocr"],
            "metadata": payload["metadata"],
        },
    )

    assert response.status_code == 200
    assert response.json()["method"] == "PUT"
    assert response.json()["path"] == "/invoices/{invoiceId}"
    assert response.json()["body"] == {"external_id": "INV-001"}
    assert response.json()["validation"]["passed"] is True


def test_cancel_and_status_use_worker_and_tenant_scoped_store(
    authenticated_client: TestClient,
) -> None:
    _seed_dependencies(authenticated_client)
    created = authenticated_client.post(
        "/v1/transfers",
        headers={"Idempotency-Key": "idem-cancel"},
        json=sample_transfer_request(),
    )
    transfer_id = created.json()["transfer_id"]

    cancelled = authenticated_client.post(f"/v1/transfers/{transfer_id}/cancel")
    status = authenticated_client.get(f"/v1/transfers/{transfer_id}")

    assert cancelled.status_code == 202
    assert cancelled.json()["status"] == TransferStatus.CANCELLED.value
    assert status.status_code == 200
    assert status.json()["status"] == TransferStatus.CANCELLED.value
    assert "request" not in status.json()


def test_reconcile_route_forwards_operator_evidence(
    authenticated_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_dependencies(authenticated_client)
    created = authenticated_client.post(
        "/v1/transfers",
        headers={"Idempotency-Key": "idem-reconcile-route"},
        json=sample_transfer_request(),
    )
    assert created.status_code == 202
    transfer_id = created.json()["transfer_id"]
    captured: dict[str, Any] = {}

    async def fake_reconcile(
        tenant_id: str,
        requested_transfer_id: str,
        *,
        evidence: Any,
    ) -> Any:
        captured["tenant_id"] = tenant_id
        captured["transfer_id"] = requested_transfer_id
        captured["evidence"] = evidence
        return await authenticated_client.app.state.store.get_transfer(tenant_id, requested_transfer_id)

    monkeypatch.setattr(authenticated_client.app.state.worker, "reconcile_transfer", fake_reconcile)
    response = authenticated_client.post(
        f"/v1/transfers/{transfer_id}/reconcile",
        json={
            "resolution": "confirmed_present",
            "target_resource_id": "target-operator",
            "notes": "verified by operator",
        },
    )

    assert response.status_code == 200
    assert captured["tenant_id"] == "tenant-a"
    assert captured["transfer_id"] == transfer_id
    assert captured["evidence"].resolution == "confirmed_present"
    assert captured["evidence"].target_resource_id == "target-operator"
    assert captured["evidence"].notes == "verified by operator"


def test_transfer_list_uses_cursor_pagination_and_created_filters(
    authenticated_client: TestClient,
) -> None:
    _seed_dependencies(authenticated_client)
    for index in range(3):
        payload = sample_transfer_request()
        payload["document"]["document_id"] = f"doc-page-{index}"
        payload["document"]["content"]["storage_ref"] = f"object://documents/doc-page-{index}"
        payload["ocr"]["text_ref"] = f"object://ocr-text/doc-page-{index}"
        payload["metadata"]["correlation_id"] = f"corr-page-{index}"
        response = authenticated_client.post(
            "/v1/transfers",
            headers={"Idempotency-Key": f"idem-page-{index}"},
            json=payload,
        )
        assert response.status_code == 202

    first = authenticated_client.get("/v1/transfers?limit=2")
    assert first.status_code == 200
    assert len(first.json()["items"]) == 2
    assert first.json()["next_cursor"]

    second = authenticated_client.get(
        "/v1/transfers",
        params={"limit": 2, "cursor": first.json()["next_cursor"]},
    )
    assert second.status_code == 200
    assert len(second.json()["items"]) == 1
    assert second.json()["next_cursor"] is None
    assert {
        item["transfer_id"] for item in first.json()["items"]
    }.isdisjoint({item["transfer_id"] for item in second.json()["items"]})

    after = authenticated_client.get(
        "/v1/transfers?created_after=2020-01-01T00:00:00Z&limit=10"
    )
    before = authenticated_client.get(
        "/v1/transfers?created_before=2099-01-01T00:00:00Z&limit=10"
    )
    malformed = authenticated_client.get("/v1/transfers?cursor=not-a-cursor")

    assert len(after.json()["items"]) == 3
    assert len(before.json()["items"]) == 3
    assert malformed.status_code == 422
    assert malformed.json()["code"] == "CURSOR_INVALID"


def test_health_endpoints_do_not_require_authentication(client: TestClient) -> None:
    live = client.get("/v1/health/live")
    ready = client.get("/v1/health/ready")

    assert live.status_code == 200
    assert live.json()["status"] == "ok"
    assert ready.status_code == 200
    assert ready.json()["dependencies"] == {
        "sqlite": "ok",
        "credentials": "ok",
        "worker": "ok",
    }


def test_request_limits_and_media_type_return_problem_details(
    authenticated_client: TestClient,
) -> None:
    unsupported = authenticated_client.post(
        "/v1/transfers",
        headers={"Idempotency-Key": "idem-media", "Content-Type": "text/plain"},
        content="{}",
    )
    authenticated_client.app.state.settings.max_payload_bytes = 16
    oversized = authenticated_client.post(
        "/v1/transfers",
        headers={"Idempotency-Key": "idem-size"},
        json=sample_transfer_request(),
    )

    assert unsupported.status_code == 415
    assert unsupported.headers["content-type"].startswith("application/problem+json")
    assert oversized.status_code == 413
    assert oversized.json()["code"] == "PAYLOAD_TOO_LARGE"
    assert "invoice_number" not in oversized.text


@pytest.mark.asyncio
async def test_read_json_body_enforces_limit_without_content_length(app: Any) -> None:
    app.state.settings.max_payload_bytes = 16
    sent = False
    body = b'{"value":"this is too large"}'

    async def receive() -> dict[str, Any]:
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    request = Request(
        {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/v1/transfers",
            "raw_path": b"/v1/transfers",
            "query_string": b"",
            "headers": [(b"content-type", b"application/json")],
            "client": ("testclient", 1234),
            "server": ("testserver", 80),
            "app": app,
        },
        receive,
    )

    with pytest.raises(ApiError) as error:
        await _read_json_body(request, required=True)

    assert error.value.status == 413
    assert error.value.code == "PAYLOAD_TOO_LARGE"


class _FakeClock:
    def __init__(self) -> None:
        self.value = 0.0

    def monotonic(self) -> float:
        return self.value


def test_rate_limiter_is_scoped_to_tenant_and_client() -> None:
    clock = _FakeClock()
    limiter = RateLimiter(requests_per_minute=60, burst=1, clock=clock)

    assert limiter.check("tenant-a", "client-a").allowed is True
    denied = limiter.check("tenant-a", "client-a")
    assert denied.allowed is False
    assert denied.retry_after_seconds == 1
    assert limiter.check("tenant-b", "client-a").allowed is True
    clock.value = 1.0
    assert limiter.check("tenant-a", "client-a").allowed is True


def test_transfer_rate_limit_uses_authenticated_subject_and_returns_retry_after(
    authenticated_client: TestClient,
) -> None:
    _seed_dependencies(authenticated_client)
    authenticated_client.app.state.rate_limiter = RateLimiter(
        requests_per_minute=60,
        burst=1,
        clock=_FakeClock(),
    )

    first = authenticated_client.post(
        "/v1/transfers",
        headers={"Idempotency-Key": "idem-rate-1", "X-Client-ID": "client-a"},
        json=sample_transfer_request(),
    )
    second = authenticated_client.post(
        "/v1/transfers",
        headers={"Idempotency-Key": "idem-rate-2", "X-Client-ID": "client-b"},
        json=sample_transfer_request(),
    )

    assert first.status_code == 202
    assert second.status_code == 429
    assert second.headers["retry-after"] == "1"


def test_connector_write_validation_modes_are_rejected(
    authenticated_client: TestClient,
) -> None:
    _seed_dependencies(authenticated_client)

    write_validation = authenticated_client.post(
        "/v1/connectors/connector-test/validate",
        json={"mode": "dry-run-write"},
    )
    structural = authenticated_client.post(
        "/v1/connectors/connector-test/validate",
        json={"mode": "structural"},
    )

    assert write_validation.status_code == 422
    assert write_validation.json()["code"] == "WRITE_VALIDATION_UNSUPPORTED"
    assert structural.status_code == 202
    assert structural.json()["status"] == "accepted"
