from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.transfer.models import ConnectorDefinition, MappingDefinition, OcrField, OcrSource, TransferRequest
from tests.transfer.conftest import sample_connector_definition, sample_mapping, sample_transfer_request


def test_confidence_and_field_status_are_bounded() -> None:
    with pytest.raises(ValidationError):
        OcrField.model_validate(
            {
                "value": "x",
                "value_type": "string",
                "confidence": 1.1,
                "status": "extracted",
            }
        )


def test_transfer_request_uses_deduplication_key_path() -> None:
    request = TransferRequest.model_validate(sample_transfer_request())

    assert request.delivery.deduplication_key_path == "/ocr/fields/invoice_number/value"


def test_ocr_source_rejects_out_of_bounds_geometry() -> None:
    with pytest.raises(ValidationError):
        OcrSource.model_validate(
            {
                "page": 1,
                "bbox": [0.0, 0.1, 1.2, 0.9],
            }
        )

    with pytest.raises(ValidationError):
        OcrSource.model_validate(
            {
                "page": 1,
                "polygon": [[0.0, 0.0], [0.5, -0.1], [1.0, 1.0]],
            }
        )


def test_mapping_definition_uses_canonical_status_and_rule_fields() -> None:
    payload = sample_mapping()
    payload["status"] = "deprecated"
    payload["rules"][0].update(
        {
            "default": "fallback",
            "enum_map": {"INV-001": "invoice-001"},
            "condition": {
                "source": "/ocr/fields/invoice_number/value",
                "operator": "exists",
            },
        }
    )

    mapping = MappingDefinition.model_validate(payload)

    assert mapping.status == "deprecated"
    assert mapping.rules[0].rule_id == "invoice-number"

    invalid_status = sample_mapping()
    invalid_status["status"] = "retired"
    with pytest.raises(ValidationError):
        MappingDefinition.model_validate(invalid_status)

    invalid_on_missing = sample_mapping()
    invalid_on_missing["rules"][0]["on_missing"] = "skip"
    with pytest.raises(ValidationError):
        MappingDefinition.model_validate(invalid_on_missing)


def test_connector_definition_rejects_secret_headers_and_http_refs() -> None:
    valid = ConnectorDefinition.model_validate(sample_connector_definition())

    assert valid.display_name == "Connector Test"

    invalid_header_name = sample_connector_definition()
    invalid_header_name["additional_headers"][0]["name"] = "Authorization"
    with pytest.raises(ValidationError):
        ConnectorDefinition.model_validate(invalid_header_name)

    invalid_header_ref = sample_connector_definition()
    invalid_header_ref["additional_headers"][0]["value_ref"] = "https://example.com/header"
    with pytest.raises(ValidationError):
        ConnectorDefinition.model_validate(invalid_header_ref)

    invalid_credential_ref = sample_connector_definition()
    invalid_credential_ref["credential_ref"] = "https://example.com/secret"
    with pytest.raises(ValidationError):
        ConnectorDefinition.model_validate(invalid_credential_ref)

    missing_display_name = sample_connector_definition()
    missing_display_name.pop("display_name")
    with pytest.raises(ValidationError):
        ConnectorDefinition.model_validate(missing_display_name)