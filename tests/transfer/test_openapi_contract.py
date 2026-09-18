from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from src.transfer.openapi_contract import ContractPreflight


FIXTURES = Path(__file__).with_name("fixtures")


def load_fixture(name: str) -> dict[str, object]:
    return yaml.safe_load((FIXTURES / name).read_text(encoding="utf-8"))


def test_preflight_rejects_relative_base_url() -> None:
    spec = load_fixture("relative-server.yaml")

    result = ContractPreflight().run(spec)

    assert result.valid is False
    assert "BASE_URL_NOT_ABSOLUTE" in {issue.code for issue in result.issues}


def test_preflight_requires_operation_level_security_and_resolves_refs() -> None:
    result = ContractPreflight().run(load_fixture("operation-security.yaml"))

    assert result.valid is False
    assert result.operations["createInvoice"].request_schema == {
        "type": "object",
        "properties": {"external_id": {"type": "string"}},
        "required": ["external_id"],
    }
    assert result.operations["createInvoice"].success_schema == {
        "type": "object",
        "properties": {"id": {"type": "string"}},
        "required": ["id"],
    }
    assert result.operations["createInvoice"].required_headers == ["X-Correlation-Id"]
    assert "SECURITY_UNDEFINED" in {issue.code for issue in result.issues}


def test_preflight_rejects_external_refs_and_duplicate_generated_operation_ids() -> None:
    spec = {
        "openapi": "3.1.0",
        "info": {"title": "Spec", "version": "1.0.0"},
        "servers": [{"url": "https://api.example.com"}],
        "paths": {
            "/orders-id": {
                "post": {
                    "security": [{"bearerAuth": []}],
                    "requestBody": {
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "https://example.com/schemas/order.json"}
                            }
                        }
                    },
                    "responses": {"201": {"description": "Created"}},
                }
            },
            "/orders_id": {
                "post": {
                    "security": [{"bearerAuth": []}],
                    "responses": {"201": {"description": "Created"}},
                }
            },
        },
        "components": {
            "securitySchemes": {
                "bearerAuth": {"type": "http", "scheme": "bearer"}
            }
        },
    }

    result = ContractPreflight().run(spec)

    codes = {issue.code for issue in result.issues}
    assert "EXTERNAL_REF_UNSUPPORTED" in codes
    assert "DUPLICATE_OPERATION_ID" in codes


def test_preflight_resolve_target_schema_supports_component_and_operation_request_refs() -> None:
    spec = {
        "openapi": "3.1.0",
        "info": {"title": "Spec", "version": "1.0.0"},
        "servers": [{"url": "https://api.example.com"}],
        "paths": {
            "/invoices": {
                "post": {
                    "operationId": "createInvoice",
                    "security": [{"bearerAuth": []}],
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/InvoiceCreateRequest"}
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
            }
        },
        "components": {
            "securitySchemes": {
                "bearerAuth": {"type": "http", "scheme": "bearer"}
            },
            "schemas": {
                "InvoiceCreateRequest": {
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

    result = ContractPreflight().run(spec)

    assert result.valid is True
    assert result.resolve_target_schema(
        "openapi:#/components/schemas/InvoiceCreateRequest",
        "createInvoice",
    )["required"] == ["external_id"]
    assert result.resolve_target_schema(
        "openapi:#/operations/createInvoice/request_schema",
        "createInvoice",
    )["properties"]["external_id"] == {"type": "string"}


@pytest.mark.parametrize(
    ("security", "expected_code"),
    [
        (None, "OPERATION_SECURITY_REQUIRED"),
        ([], "OPERATION_SECURITY_EMPTY"),
    ],
)
def test_preflight_rejects_missing_effective_security_success_schema_and_error_responses(
    security: list[dict[str, object]] | None,
    expected_code: str,
) -> None:
    spec: dict[str, object] = {
        "openapi": "3.1.0",
        "info": {"title": "Spec", "version": "1.0.0"},
        "servers": [{"url": "https://api.example.com"}],
        "paths": {
            "/invoices": {
                "post": {
                    "operationId": "createInvoice",
                    "responses": {
                        "201": {"description": "Created"},
                    },
                }
            }
        },
        "components": {
            "securitySchemes": {
                "bearerAuth": {"type": "http", "scheme": "bearer"}
            }
        },
    }
    operation = spec["paths"]["/invoices"]["post"]
    if security is not None:
        operation["security"] = security

    result = ContractPreflight().run(spec)

    codes = {issue.code for issue in result.issues}
    assert result.valid is False
    assert expected_code in codes
    assert "SUCCESS_RESPONSE_SCHEMA_MISSING" in codes
    assert "ERROR_4XX_RESPONSE_MISSING" in codes
    assert "ERROR_5XX_RESPONSE_MISSING" in codes