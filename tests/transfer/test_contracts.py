from __future__ import annotations

from jsonschema import Draft202012Validator


def test_transfer_schema_requires_connector_and_mapping(json_loader) -> None:
    invalid = {
        "schema_version": "ocr-transfer/v1",
        "document": {"document_id": "doc-1", "document_type": "invoice"},
        "ocr": {"fields": {}},
        "delivery": {"operation": "upsert"},
    }

    errors = list(
        Draft202012Validator(json_loader("schemas/ocr-transfer-v1.json")).iter_errors(
            invalid
        )
    )

    assert any(error.validator == "required" for error in errors)


def test_transfer_schema_allows_fields_only_or_line_items_only(json_loader) -> None:
    schema = json_loader("schemas/ocr-transfer-v1.json")
    validator = Draft202012Validator(schema)
    fields_only = {
        "schema_version": "ocr-transfer/v1",
        "document": {
            "document_id": "doc-fields-only",
            "document_type": "invoice",
            "source_system": "ocr-service-a",
        },
        "ocr": {
            "fields": {
                "invoice_number": {
                    "value": "INV-1",
                    "value_type": "string",
                    "status": "extracted",
                }
            }
        },
        "delivery": {
            "connector_id": "finance-api-prod",
            "mapping_id": "invoice-v1",
            "operation": "upsert",
            "deduplication_key_path": "/ocr/fields/invoice_number/value",
        },
        "metadata": {
            "tenant_id": "tenant-001",
            "correlation_id": "corr-001",
        },
    }
    line_items_only = {
        **fields_only,
        "document": {
            **fields_only["document"],
            "document_id": "doc-line-items-only",
        },
        "ocr": {
            "line_items": [
                {
                    "fields": {
                        "description": {
                            "value": "Item A",
                            "value_type": "string",
                            "status": "extracted",
                        }
                    }
                }
            ]
        },
    }

    assert list(validator.iter_errors(fields_only)) == []
    assert list(validator.iter_errors(line_items_only)) == []


def test_transfer_schema_rejects_non_opaque_storage_and_text_refs(json_loader) -> None:
    schema = json_loader("schemas/ocr-transfer-v1.json")
    validator = Draft202012Validator(schema)
    valid = {
        "schema_version": "ocr-transfer/v1",
        "document": {
            "document_id": "doc-refs",
            "document_type": "invoice",
            "source_system": "ocr-service-a",
            "content": {
                "storage_ref": "object://documents/doc-refs",
            },
        },
        "ocr": {
            "text_ref": "object://ocr-text/doc-refs",
            "fields": {
                "invoice_number": {
                    "value": "INV-2",
                    "value_type": "string",
                    "status": "extracted",
                }
            },
        },
        "delivery": {
            "connector_id": "finance-api-prod",
            "mapping_id": "invoice-v1",
            "operation": "create",
            "deduplication_key_path": "/ocr/fields/invoice_number/value",
        },
        "metadata": {
            "tenant_id": "tenant-001",
            "correlation_id": "corr-002",
        },
    }

    assert list(validator.iter_errors(valid)) == []
    assert any(
        error.validator == "pattern"
        for error in validator.iter_errors(
            {
                **valid,
                "document": {
                    **valid["document"],
                    "content": {"storage_ref": "file://tmp/invoice.pdf"},
                },
            }
        )
    )
    assert any(
        error.validator == "pattern"
        for error in validator.iter_errors(
            {
                **valid,
                "ocr": {
                    **valid["ocr"],
                    "text_ref": "ftp://ocr-text/doc-refs",
                },
            }
        )
    )


def test_mapping_schema_restricts_target_locations_schema_refs_and_operations(json_loader) -> None:
    schema = json_loader("schemas/mapping-v1.json")
    validator = Draft202012Validator(schema)
    valid = {
        "mapping_id": "invoice-v1",
        "version": 3,
        "connector_id": "finance-api-prod",
        "document_types": ["invoice"],
        "operations": ["upsert"],
        "deduplication_key_path": "/ocr/fields/invoice_number/value",
        "target_schema_ref": "openapi:#/components/schemas/InvoiceUpsertRequest",
        "rules": [
            {
                "source": "/ocr/fields/invoice_number/value",
                "target": {
                    "location": "body",
                    "pointer": "/external_id",
                },
                "required": True,
                "on_missing": "error",
            }
        ],
    }

    assert list(validator.iter_errors(valid)) == []

    invalid_location = {
        **valid,
        "rules": [
            {
                "source": "/ocr/fields/invoice_number/value",
                "target": {
                    "location": "cookie",
                    "name": "external_id",
                },
                "required": True,
                "on_missing": "error",
            }
        ],
    }
    invalid_ref = {**valid, "target_schema_ref": "finance-invoice-request:2026-09-01"}
    invalid_operation = {**valid, "operations": ["batch_upsert"]}

    assert any(error.validator == "enum" for error in validator.iter_errors(invalid_location))
    assert any(error.validator == "pattern" for error in validator.iter_errors(invalid_ref))
    assert any(error.validator == "enum" for error in validator.iter_errors(invalid_operation))


def test_openapi_contract_exposes_transfer_and_admin_paths(yaml_loader) -> None:
    paths = yaml_loader("docs/api/ocr-transfer-openapi.yaml")["paths"]

    expected_paths = {
        "/v1/transfers",
        "/v1/transfers/{transfer_id}",
        "/v1/transfers/{transfer_id}/retry",
        "/v1/transfers/{transfer_id}/cancel",
        "/v1/transfers/{transfer_id}/review",
        "/v1/transfers/{transfer_id}/reconcile",
        "/v1/mappings/{mapping_id}/preview",
        "/v1/connectors",
        "/v1/connectors/{connector_id}/validate",
        "/v1/mappings",
        "/v1/health/live",
        "/v1/health/ready",
    }

    assert expected_paths.issubset(paths)
    assert paths["/v1/transfers"]["post"]["responses"]["202"]


def test_openapi_contract_uses_problem_details_headers_and_operation_scopes(yaml_loader) -> None:
    document = yaml_loader("docs/api/ocr-transfer-openapi.yaml")
    post_transfer = document["paths"]["/v1/transfers"]["post"]
    connector_create = document["paths"]["/v1/connectors"]["post"]
    mapping_create = document["paths"]["/v1/mappings"]["post"]

    assert "Idempotency-Key" in post_transfer["parameters"][0]["name"]
    assert "X-Correlation-ID" in {param["name"] for param in post_transfer["parameters"]}
    assert post_transfer["responses"]["409"]["content"]["application/problem+json"]
    assert post_transfer["responses"]["202"]["headers"]["X-Idempotent-Replay"]
    assert post_transfer["security"] == [{"OAuth2": ["transfer:write"]}]
    assert document["paths"]["/v1/transfers/{transfer_id}/retry"]["post"]["security"] == [
        {"OAuth2": ["transfer:retry"]}
    ]
    assert document["paths"]["/v1/transfers/{transfer_id}/cancel"]["post"]["security"] == [
        {"OAuth2": ["transfer:cancel"]}
    ]
    assert document["paths"]["/v1/transfers/{transfer_id}/review"]["post"]["security"] == [
        {"OAuth2": ["transfer:review"]}
    ]
    assert document["paths"]["/v1/transfers/{transfer_id}/reconcile"]["post"]["security"] == [
        {"OAuth2": ["transfer:reconcile"]}
    ]
    assert connector_create["security"] == [{"OAuth2": ["connector:admin"]}]
    assert mapping_create["security"] == [{"OAuth2": ["mapping:write"]}]


def test_openapi_public_operation_enums_only_allow_mvp_operations(yaml_loader, json_loader) -> None:
    document = yaml_loader("docs/api/ocr-transfer-openapi.yaml")
    transfer_schema = json_loader("schemas/ocr-transfer-v1.json")

    expected_operations = ["create", "update", "upsert"]
    transfer_operation = document["components"]["schemas"]["TransferOperation"]

    assert transfer_schema["$defs"]["delivery"]["properties"]["operation"]["enum"] == expected_operations
    assert transfer_operation["enum"] == expected_operations
    assert document["components"]["schemas"]["MappingPreviewRequest"]["properties"]["operation"]["$ref"] == "#/components/schemas/TransferOperation"
    assert document["components"]["schemas"]["MappingListItem"]["properties"]["operations"]["items"]["$ref"] == "#/components/schemas/TransferOperation"


def test_openapi_connector_definition_accepts_spec_or_spec_ref_without_inline_secrets(yaml_loader) -> None:
    schema = yaml_loader("docs/api/ocr-transfer-openapi.yaml")["components"]["schemas"][
        "ConnectorDefinition"
    ]
    validator = Draft202012Validator(schema)

    base_payload = {
        "connector_id": "finance-api-prod",
        "type": "rest-openapi",
        "display_name": "Finance API",
        "base_url": "https://api.example.com",
        "credential_ref": "secret://finance-api/prod",
        "additional_headers": [
            {
                "name": "X-Api-Version",
                "value_ref": "config://finance-api/api-version",
            }
        ],
        "policy": {
            "connect_timeout_seconds": 5,
            "read_timeout_seconds": 30,
            "total_timeout_seconds": 60,
            "max_response_bytes": 10485760,
            "max_redirects": 0,
        },
    }

    assert list(validator.iter_errors({**base_payload, "spec_ref": "finance-api:2026-09-01"})) == []
    assert list(
        validator.iter_errors(
            {
                **base_payload,
                "spec": {"openapi": "3.1.0", "info": {"title": "Finance", "version": "1.0.0"}, "paths": {}},
            }
        )
    ) == []
    assert list(validator.iter_errors({**base_payload, "client_secret": "should-not-be-accepted"}))
    assert any(
        error.validator == "required"
        or error.validator == "oneOf"
        for error in validator.iter_errors(base_payload)
    )
    assert any(
        error.validator == "additionalProperties"
        or error.validator == "oneOf"
        for error in validator.iter_errors(
            {
                **base_payload,
                "spec_ref": "finance-api:2026-09-01",
                "additional_headers": [
                    {
                        "name": "X-Api-Version",
                        "value": "2026-09-01",
                    }
                ],
            }
        )
    )

    for forbidden_name in ["Authorization", "Cookie", "Proxy-Authorization", "X-Api-Key"]:
        assert any(
            error.validator == "enum" or error.validator == "const"
            for error in validator.iter_errors(
                {
                    **base_payload,
                    "spec_ref": "finance-api:2026-09-01",
                    "additional_headers": [
                        {
                            "name": forbidden_name,
                            "value_ref": "config://finance-api/header-value",
                        }
                    ],
                }
            )
        )