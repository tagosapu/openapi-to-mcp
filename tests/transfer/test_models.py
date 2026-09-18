from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.transfer.models import OcrField, TransferRequest
from tests.transfer.conftest import sample_transfer_request


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