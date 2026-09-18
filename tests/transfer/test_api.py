from __future__ import annotations

import json
from typing import Any

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from src.transfer.app import create_app, current_principal
from src.transfer.auth import Principal
from src.transfer.limits import RateLimiter
from src.transfer.models import TransferStatus
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
    }
    return connector


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