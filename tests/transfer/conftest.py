from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]


def load_json(path: str) -> dict[str, Any]:
    return json.loads((REPO_ROOT / path).read_text(encoding="utf-8"))


def load_yaml(path: str) -> dict[str, Any]:
    return yaml.safe_load((REPO_ROOT / path).read_text(encoding="utf-8"))


@pytest.fixture
def json_loader() -> Callable[[str], dict[str, Any]]:
    return load_json


@pytest.fixture
def yaml_loader() -> Callable[[str], dict[str, Any]]:
    return load_yaml


@pytest.fixture
def sqlite_settings(tmp_path: Path) -> dict[str, str]:
    database_path = tmp_path / "transfer-test.sqlite3"
    return {
        "database_path": str(database_path),
        "database_url": f"sqlite+aiosqlite:///{database_path}",
    }


def sample_transfer_request(line_items: int | None = None) -> dict[str, Any]:
    line_item_count = 1 if line_items is None else line_items
    payload: dict[str, Any] = {
        "schema_version": "ocr-transfer/v1",
        "document": {
            "document_id": "doc-001",
            "document_type": "invoice",
            "source_system": "ocr-suite",
            "occurred_at": "2026-09-18T00:00:00Z",
            "content": {
                "media_type": "application/pdf",
                "filename": "invoice.pdf",
                "sha256": "sha256:" + "a" * 64,
                "storage_ref": "object://documents/doc-001",
            },
        },
        "ocr": {
            "languages": ["en"],
            "text_ref": "object://ocr-text/doc-001",
            "fields": {
                "invoice_number": {
                    "value": "INV-001",
                    "value_type": "string",
                    "confidence": 0.98,
                    "status": "extracted",
                },
                "total_amount": {
                    "value": 123.45,
                    "value_type": "currency",
                    "unit": "USD",
                    "confidence": 0.95,
                    "status": "extracted",
                },
            },
        },
        "delivery": {
            "connector_id": "connector-test",
            "mapping_id": "mapping-test",
            "operation": "upsert",
            "deduplication_key_path": "/ocr/fields/invoice_number/value",
        },
        "metadata": {
            "tenant_id": "tenant-a",
            "correlation_id": "corr-001",
            "labels": {"source": "unit-test"},
        },
    }
    if line_item_count > 0:
        payload["ocr"]["line_items"] = [
            {
                "fields": {
                    "description": {
                        "value": f"Item {index + 1}",
                        "value_type": "string",
                        "confidence": 0.9,
                        "status": "extracted",
                    }
                }
            }
            for index in range(line_item_count)
        ]
    return payload


def sample_mapping() -> dict[str, Any]:
    return {
        "mapping_id": "mapping-test",
        "version": 3,
        "status": "published",
        "connector_id": "connector-test",
        "document_types": ["invoice"],
        "operations": ["upsert"],
        "deduplication_key_path": "/ocr/fields/invoice_number/value",
        "target_schema_ref": "openapi:#/components/schemas/InvoiceUpsertRequest",
        "rules": [
            {
                "rule_id": "invoice-number",
                "source": "/ocr/fields/invoice_number/value",
                "target": {
                    "location": "body",
                    "pointer": "/external_id",
                },
                "required": True,
                "on_missing": "error",
                "transforms": [],
            }
        ],
    }


def sample_mapping_with_target_header(name: str) -> dict[str, Any]:
    mapping = sample_mapping()
    mapping["rules"] = [
        {
            "rule_id": "header-rule",
            "source": "/ocr/fields/invoice_number/value",
            "target": {
                "location": "header",
                "name": name,
            },
            "required": True,
            "on_missing": "error",
            "transforms": [],
        }
    ]
    return mapping


def sample_operation_selection(name: str) -> dict[str, Any]:
    operations = {
        "create": {"method": "POST", "path": "/invoices"},
        "update": {"method": "PATCH", "path": "/invoices/{invoiceId}"},
        "upsert": {"method": "PUT", "path": "/invoices/{invoiceId}"},
    }
    operation = operations[name]
    return {
        "name": name,
        "operation_id": f"{name}Invoice",
        "method": operation["method"],
        "path": operation["path"],
    }


def sample_connector_definition() -> dict[str, Any]:
    return {
        "connector_id": "connector-test",
        "version": 7,
        "type": "rest-openapi",
        "display_name": "Connector Test",
        "base_url": "https://api.example.com",
        "spec_ref": "object://openapi/connector-test/7",
        "spec": {
            "openapi": "3.1.0",
            "info": {"title": "Connector Test", "version": "1.0.0"},
            "paths": {
                "/invoices/{invoiceId}": {
                    "put": {
                        "operationId": "upsertInvoice",
                    }
                }
            },
        },
        "credential_ref": "vault://connectors/connector-test",
        "additional_headers": [
            {"name": "X-Api-Version", "value_ref": "config://headers/x-api-version"}
        ],
        "operation_bindings": {
            "upsert": {
                "operation_id": "upsertInvoice",
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
        },
        "policy": {
            "connect_timeout_seconds": 5,
            "read_timeout_seconds": 30,
            "total_timeout_seconds": 60,
            "max_response_bytes": 10 * 1024 * 1024,
            "max_redirects": 0,
        },
    }


async def seed_transfer_for_tenant(store: Any, tenant_id: str) -> str:
    from src.transfer.models import TransferRequest

    request_payload = sample_transfer_request()
    request_payload["metadata"]["tenant_id"] = tenant_id
    request_payload["metadata"]["correlation_id"] = f"corr-{tenant_id}"
    request_payload["document"]["document_id"] = f"doc-{tenant_id}"
    request_payload["document"]["content"]["storage_ref"] = (
        f"object://documents/doc-{tenant_id}"
    )
    request_payload["ocr"]["text_ref"] = f"object://ocr-text/doc-{tenant_id}"
    request = TransferRequest.model_validate(request_payload)
    created = await store.create_or_get_transfer(
        tenant_id=tenant_id,
        idempotency_key=f"idem-{tenant_id}",
        request=request,
        correlation_id=request.metadata.correlation_id,
        connector_version=7,
        mapping_version=3,
    )
    return created.record.transfer_id


def settings_factory(**overrides: Any) -> Any:
    from src.transfer.settings import TransferSettings

    values = {
        "database_path": ":memory:",
        "jwt_issuer": "https://issuer.example.com",
        "jwt_audience": "ocr-transfer",
        "jwks_url": "https://issuer.example.com/.well-known/jwks.json",
        "allowed_hosts": ["localhost", "testserver"],
        "worker_poll_seconds": 1.0,
        "worker_enabled": True,
        "max_attempts": 5,
        "max_payload_bytes": 1024 * 1024,
        "idempotency_retention_hours": 24,
        "payload_retention_days": 30,
        "audit_retention_days": 90,
        "requests_per_minute": 120,
        "burst": 20,
        "credentials_json": "{\"connector-test\": \"vault://connectors/connector-test\"}",
        "data_encryption_key_ref": "key://tests/transfer",
    }
    values.update(overrides)
    return TransferSettings.model_validate(values)


@pytest.fixture
def test_settings(tmp_path: Path) -> Any:
    return settings_factory(database_path=str(tmp_path / "transfer.sqlite3"))


@pytest.fixture
def fake_clock() -> Callable[[], datetime]:
    now = datetime(2026, 9, 18, 0, 0, tzinfo=UTC)

    def _now() -> datetime:
        return now

    return _now