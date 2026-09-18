from __future__ import annotations

import json
from pathlib import Path

from jsonschema import Draft202012Validator
import yaml


REPO_ROOT = Path(__file__).resolve().parents[2]


def load_json(path: str) -> dict[str, object]:
    return json.loads((REPO_ROOT / path).read_text(encoding="utf-8"))


def load_yaml(path: str) -> dict[str, object]:
    return yaml.safe_load((REPO_ROOT / path).read_text(encoding="utf-8"))


def test_transfer_schema_requires_connector_and_mapping() -> None:
    invalid = {
        "schema_version": "ocr-transfer/v1",
        "document": {"document_id": "doc-1", "document_type": "invoice"},
        "ocr": {"fields": {}},
        "delivery": {"operation": "upsert"},
    }

    errors = list(
        Draft202012Validator(load_json("schemas/ocr-transfer-v1.json")).iter_errors(
            invalid
        )
    )

    assert any(error.validator == "required" for error in errors)


def test_mapping_schema_restricts_target_locations_and_schema_refs() -> None:
    schema = load_json("schemas/mapping-v1.json")
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

    assert any(error.validator == "enum" for error in validator.iter_errors(invalid_location))
    assert any(error.validator == "pattern" for error in validator.iter_errors(invalid_ref))


def test_openapi_contract_exposes_transfer_and_admin_paths() -> None:
    paths = load_yaml("docs/api/ocr-transfer-openapi.yaml")["paths"]

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


def test_openapi_contract_uses_problem_details_headers_and_operation_scopes() -> None:
    document = load_yaml("docs/api/ocr-transfer-openapi.yaml")
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


def test_openapi_connector_definition_accepts_spec_or_spec_ref_without_secrets() -> None:
    schema = load_yaml("docs/api/ocr-transfer-openapi.yaml")["components"]["schemas"][
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
    assert list(validator.iter_errors(base_payload))