from __future__ import annotations

import hashlib
import json
from copy import deepcopy

import pytest

from src.transfer import (
    MappingDefinition,
    MappingValidationError,
    OperationSelection,
    ReviewCorrection,
    TransferRequest,
)
from src.transfer.mapping import MappingEngine
from tests.transfer.conftest import (
    sample_mapping,
    sample_mapping_with_target_header,
    sample_transfer_request,
)


def _request(payload: dict | None = None) -> TransferRequest:
    source = sample_transfer_request(line_items=2) if payload is None else payload
    return TransferRequest.model_validate(source)


def _operation(
    name: str = "upsert",
    *,
    operation_id: str = "upsertInvoice",
    method: str = "PUT",
    path: str = "/invoices/{external_id}",
) -> OperationSelection:
    return OperationSelection.model_validate(
        {
            "name": name,
            "operation_id": operation_id,
            "method": method,
            "path": path,
        }
    )


def _mapping(rules: list[dict], *, operations: list[str] | None = None, deduplication_key_path: str | None = None) -> MappingDefinition:
    payload = sample_mapping()
    payload["rules"] = rules
    if operations is not None:
        payload["operations"] = operations
    if deduplication_key_path is not None:
        payload["deduplication_key_path"] = deduplication_key_path
    return MappingDefinition.model_validate(payload)


def test_mapping_builds_path_query_header_and_body() -> None:
    payload = sample_transfer_request(line_items=2)
    payload["ocr"]["fields"]["invoice_number"]["value"] = "INV-0001"
    payload["ocr"]["fields"]["dry_run"] = {
        "value": False,
        "value_type": "boolean",
        "confidence": 0.99,
        "status": "extracted",
    }
    payload["ocr"]["fields"]["total_amount"]["value"] = 12000
    mapping = _mapping(
        [
            {
                "rule_id": "path-external-id",
                "source": "/ocr/fields/invoice_number/value",
                "target": {"location": "path", "name": "external_id"},
                "required": True,
                "on_missing": "error",
                "transforms": [],
            },
            {
                "rule_id": "query-dry-run",
                "source": "/ocr/fields/dry_run/value",
                "target": {"location": "query", "name": "dry_run"},
                "required": True,
                "on_missing": "error",
                "transforms": ["to_string", "lower"],
            },
            {
                "rule_id": "header-document-type",
                "source": "/document/document_type",
                "target": {"location": "header", "name": "X-Document-Type"},
                "required": True,
                "on_missing": "error",
                "transforms": [],
            },
            {
                "rule_id": "body-amount",
                "source": "/ocr/fields/total_amount",
                "target": {"location": "body", "pointer": "/amount"},
                "required": True,
                "on_missing": "error",
                "transforms": ["currency_amount", "to_integer"],
            },
        ]
    )

    preview = MappingEngine().preview(_request(payload), mapping, _operation())

    assert preview.path_params == {"external_id": "INV-0001"}
    assert preview.query_params == {"dry_run": "false"}
    assert preview.headers == {"X-Document-Type": "invoice"}
    assert preview.json_body["amount"] == 12000
    assert "Authorization" not in preview.headers
    assert "Idempotency-Key" not in preview.headers


def test_mapping_rejects_auth_and_control_headers() -> None:
    mapping = MappingDefinition.model_validate(sample_mapping_with_target_header("Authorization"))

    with pytest.raises(MappingValidationError, match="forbidden header"):
        MappingEngine().preview(_request(), mapping, _operation())


@pytest.mark.parametrize("name", ["Host", "Content-Length", "Cookie", "Proxy-Authorization"])
def test_mapping_rejects_all_forbidden_headers(name: str) -> None:
    mapping = MappingDefinition.model_validate(sample_mapping_with_target_header(name))

    with pytest.raises(MappingValidationError, match="forbidden header"):
        MappingEngine().preview(_request(), mapping, _operation())


def test_mapping_applies_default_enum_condition_concat_and_line_items() -> None:
    payload = sample_transfer_request(line_items=2)
    payload["ocr"]["fields"]["invoice_number"]["value"] = " INV-0001 "
    payload["ocr"]["fields"]["approval"] = {
        "value": "yes",
        "value_type": "string",
        "confidence": 0.96,
        "status": "extracted",
    }
    payload["ocr"]["fields"]["summary_parts"] = {
        "value": ["Invoice ", "INV-0001", " for ", "invoice"],
        "value_type": "array",
        "confidence": 0.96,
        "status": "extracted",
    }
    mapping = _mapping(
        [
            {
                "rule_id": "external-id",
                "source": "/ocr/fields/invoice_number/value",
                "target": {"location": "body", "pointer": "/external_id"},
                "required": True,
                "on_missing": "error",
                "transforms": ["trim"],
            },
            {
                "rule_id": "approval-code",
                "source": "/ocr/fields/approval/value",
                "target": {"location": "body", "pointer": "/approval_code"},
                "required": True,
                "on_missing": "error",
                "transforms": ["to_integer"],
                "enum_map": {"yes": "1", "no": "0"},
            },
            {
                "rule_id": "default-note",
                "source": "/ocr/fields/note/value",
                "target": {"location": "body", "pointer": "/note"},
                "required": False,
                "on_missing": "omit",
                "transforms": [],
                "default": "n/a",
            },
            {
                "rule_id": "summary",
                "source": "/ocr/fields/summary_parts/value",
                "target": {"location": "body", "pointer": "/summary"},
                "required": True,
                "on_missing": "error",
                "transforms": ["concat"],
            },
            {
                "rule_id": "line-items",
                "source": "/ocr/line_items",
                "target": {"location": "body", "pointer": "/line_items"},
                "required": True,
                "on_missing": "error",
                "transforms": [],
            },
            {
                "rule_id": "skip-when-condition-false",
                "source": "/ocr/fields/invoice_number/value",
                "target": {"location": "body", "pointer": "/skipped"},
                "required": False,
                "on_missing": "omit",
                "transforms": [],
                "condition": {
                    "source": "/document/document_type",
                    "operator": "equals",
                    "value": "receipt",
                },
            },
        ]
    )
    normalized_line_items = _request(payload).model_dump(mode="json")["ocr"]["line_items"]

    preview = MappingEngine().preview(_request(payload), mapping, _operation())

    assert preview.json_body == {
        "approval_code": 1,
        "external_id": "INV-0001",
        "line_items": normalized_line_items,
        "note": "n/a",
        "summary": "Invoice INV-0001 for invoice",
    }


def test_mapping_rejects_concat_on_non_list_input() -> None:
    mapping = _mapping(
        [
            {
                "rule_id": "summary",
                "source": "/ocr/fields/invoice_number/value",
                "target": {"location": "body", "pointer": "/summary"},
                "required": True,
                "on_missing": "error",
                "transforms": ["concat"],
            }
        ]
    )

    with pytest.raises(MappingValidationError, match="concat"):
        MappingEngine().preview(_request(), mapping, _operation())


def test_mapping_handles_allowed_type_conversions() -> None:
    payload = sample_transfer_request(line_items=0)
    payload["ocr"]["fields"]["invoice_number"]["value"] = " inv-001 "
    payload["ocr"]["fields"]["int_text"] = {
        "value": "0042",
        "value_type": "string",
        "confidence": 0.95,
        "status": "extracted",
    }
    payload["ocr"]["fields"]["number_text"] = {
        "value": "1234.50",
        "value_type": "string",
        "confidence": 0.95,
        "status": "extracted",
    }
    payload["ocr"]["fields"]["issue_date"] = {
        "value": "2026-09-18",
        "value_type": "string",
        "confidence": 0.95,
        "status": "extracted",
    }
    payload["ocr"]["fields"]["issued_at"] = {
        "value": "2026-09-18T01:02:03Z",
        "value_type": "string",
        "confidence": 0.95,
        "status": "extracted",
    }
    mapping = _mapping(
        [
            {
                "rule_id": "lower-code",
                "source": "/ocr/fields/invoice_number/value",
                "target": {"location": "body", "pointer": "/lower_code"},
                "required": True,
                "on_missing": "error",
                "transforms": ["trim", "lower"],
            },
            {
                "rule_id": "upper-type",
                "source": "/document/document_type",
                "target": {"location": "body", "pointer": "/upper_type"},
                "required": True,
                "on_missing": "error",
                "transforms": ["upper"],
            },
            {
                "rule_id": "quantity",
                "source": "/ocr/fields/int_text/value",
                "target": {"location": "body", "pointer": "/quantity"},
                "required": True,
                "on_missing": "error",
                "transforms": ["to_integer"],
            },
            {
                "rule_id": "amount-number",
                "source": "/ocr/fields/number_text/value",
                "target": {"location": "body", "pointer": "/amount_number"},
                "required": True,
                "on_missing": "error",
                "transforms": ["to_number"],
            },
            {
                "rule_id": "issue-date",
                "source": "/ocr/fields/issue_date/value",
                "target": {"location": "body", "pointer": "/issue_date"},
                "required": True,
                "on_missing": "error",
                "transforms": ["to_date"],
            },
            {
                "rule_id": "issued-at",
                "source": "/ocr/fields/issued_at/value",
                "target": {"location": "body", "pointer": "/issued_at"},
                "required": True,
                "on_missing": "error",
                "transforms": ["to_datetime"],
            },
            {
                "rule_id": "amount-text",
                "source": "/ocr/fields/total_amount",
                "target": {"location": "body", "pointer": "/amount_text"},
                "required": True,
                "on_missing": "error",
                "transforms": ["currency_amount", "to_string"],
            },
        ]
    )

    preview = MappingEngine().preview(_request(payload), mapping, _operation())

    assert preview.json_body == {
        "amount_number": 1234.5,
        "amount_text": "123.45",
        "issue_date": "2026-09-18",
        "issued_at": "2026-09-18T01:02:03+00:00",
        "lower_code": "inv-001",
        "quantity": 42,
        "upper_type": "INVOICE",
    }


def test_mapping_adds_low_confidence_review_issues() -> None:
    payload = sample_transfer_request(line_items=0)
    payload["ocr"]["fields"]["invoice_number"]["confidence"] = 0.89
    payload["ocr"]["fields"]["memo"] = {
        "value": "check",
        "value_type": "string",
        "confidence": 0.69,
        "status": "ambiguous",
    }
    mapping = _mapping(
        [
            {
                "rule_id": "external-id",
                "source": "/ocr/fields/invoice_number/value",
                "target": {"location": "body", "pointer": "/external_id"},
                "required": True,
                "on_missing": "error",
                "transforms": [],
            },
            {
                "rule_id": "memo",
                "source": "/ocr/fields/memo/value",
                "target": {"location": "body", "pointer": "/memo"},
                "required": False,
                "on_missing": "omit",
                "transforms": [],
            },
        ]
    )

    preview = MappingEngine().preview(_request(payload), mapping, _operation())

    assert preview.requires_review is True
    assert preview.issues[0].model_dump() == {
        "source_path": "/ocr/fields/invoice_number",
        "target_path": "/body/external_id",
        "code": "LOW_CONFIDENCE",
        "message": "required field confidence is below threshold",
        "retryable": False,
    }
    assert any(issue.target_path == "/body/memo" for issue in preview.issues)


def test_apply_corrections_overlays_without_mutating_original() -> None:
    request = _request()
    original = request.model_dump(mode="json")
    correction = ReviewCorrection.model_validate(
        {
            "correction_ref": "object://reviews/correction-1",
            "values": {
                "/ocr/fields/invoice_number/value": "INV-0099",
                "/ocr/fields/invoice_number/status": "manually_corrected",
            },
            "actor": "reviewer@example.com",
            "reason": "verified against source",
        }
    )

    corrected = MappingEngine().apply_corrections(request, correction)

    assert request.model_dump(mode="json") == original
    assert corrected.ocr.fields["invoice_number"].value == "INV-0099"
    assert corrected.ocr.fields["invoice_number"].status == "manually_corrected"


def test_apply_corrections_supports_root_pointer_without_mutating_original() -> None:
    request = _request()
    original = request.model_dump(mode="json")
    replacement = deepcopy(original)
    replacement["document"]["document_type"] = "receipt"
    replacement["ocr"]["fields"]["invoice_number"]["value"] = "INV-ROOT"
    correction = ReviewCorrection.model_validate(
        {
            "correction_ref": "object://reviews/correction-root",
            "values": {"": replacement},
            "actor": "reviewer@example.com",
            "reason": "replace payload with approved snapshot",
        }
    )

    corrected = MappingEngine().apply_corrections(request, correction)

    assert request.model_dump(mode="json") == original
    assert corrected.document.document_type == "receipt"
    assert corrected.ocr.fields["invoice_number"].value == "INV-ROOT"


def test_mapping_rejects_unknown_pointers() -> None:
    mapping = _mapping(
        [
            {
                "rule_id": "unknown-source",
                "source": "/ocr/fields/unknown/value",
                "target": {"location": "body", "pointer": "/external_id"},
                "required": True,
                "on_missing": "error",
                "transforms": [],
            }
        ]
    )

    with pytest.raises(MappingValidationError, match="source pointer"):
        MappingEngine().preview(_request(), mapping, _operation())


def test_mapping_rejects_duplicate_targets() -> None:
    mapping = _mapping(
        [
            {
                "rule_id": "external-id-1",
                "source": "/ocr/fields/invoice_number/value",
                "target": {"location": "body", "pointer": "/external_id"},
                "required": True,
                "on_missing": "error",
                "transforms": [],
            },
            {
                "rule_id": "external-id-2",
                "source": "/document/document_id",
                "target": {"location": "body", "pointer": "/external_id"},
                "required": True,
                "on_missing": "error",
                "transforms": [],
            },
        ]
    )

    with pytest.raises(MappingValidationError, match="duplicate target"):
        MappingEngine().preview(_request(), mapping, _operation())


def test_mapping_rejects_operation_mismatch() -> None:
    mapping = _mapping(
        [
            {
                "rule_id": "external-id",
                "source": "/ocr/fields/invoice_number/value",
                "target": {"location": "body", "pointer": "/external_id"},
                "required": True,
                "on_missing": "error",
                "transforms": [],
            }
        ],
        operations=["create"],
    )

    with pytest.raises(MappingValidationError, match="operation"):
        MappingEngine().preview(_request(), mapping, _operation())


def test_mapping_rejects_empty_required_values() -> None:
    payload = sample_transfer_request(line_items=0)
    payload["ocr"]["fields"]["invoice_number"]["value"] = ""
    mapping = _mapping(
        [
            {
                "rule_id": "external-id",
                "source": "/ocr/fields/invoice_number/value",
                "target": {"location": "body", "pointer": "/external_id"},
                "required": True,
                "on_missing": "error",
                "transforms": [],
            }
        ]
    )

    with pytest.raises(MappingValidationError, match="required"):
        MappingEngine().preview(_request(payload), mapping, _operation())


@pytest.mark.parametrize(
    ("value", "value_type"),
    [
        (None, "string"),
        ("", "string"),
        ([], "array"),
        ({}, "object"),
        (True, "boolean"),
        (["INV-001"], "array"),
        ({"value": "INV-001"}, "object"),
    ],
)
def test_apply_rejects_invalid_deduplication_values(value: object, value_type: str) -> None:
    payload = sample_transfer_request(line_items=0)
    payload["ocr"]["fields"]["dedup"] = {
        "value": value,
        "value_type": value_type,
        "confidence": 0.99,
        "status": "extracted",
    }
    payload["delivery"]["deduplication_key_path"] = "/ocr/fields/dedup/value"
    mapping = _mapping(
        [
            {
                "rule_id": "external-id",
                "source": "/ocr/fields/invoice_number/value",
                "target": {"location": "body", "pointer": "/external_id"},
                "required": True,
                "on_missing": "error",
                "transforms": [],
            }
        ],
        deduplication_key_path="/ocr/fields/dedup/value",
    )

    with pytest.raises(MappingValidationError, match="DEDUPLICATION_KEY_INVALID"):
        MappingEngine().apply(_request(payload), mapping, _operation())


def test_apply_rejects_deduplication_key_path_mismatch() -> None:
    mapping = _mapping(
        [
            {
                "rule_id": "external-id",
                "source": "/ocr/fields/invoice_number/value",
                "target": {"location": "body", "pointer": "/external_id"},
                "required": True,
                "on_missing": "error",
                "transforms": [],
            }
        ],
        deduplication_key_path="/document/document_id",
    )

    with pytest.raises(MappingValidationError, match="deduplication_key_path"):
        MappingEngine().apply(_request(), mapping, _operation())


def test_apply_builds_canonical_idempotency_key() -> None:
    payload = sample_transfer_request(line_items=0)
    payload["ocr"]["fields"]["dedup_number"] = {
        "value": 42,
        "value_type": "integer",
        "confidence": 0.99,
        "status": "extracted",
    }
    payload["delivery"]["deduplication_key_path"] = "/ocr/fields/dedup_number/value"
    mapping = _mapping(
        [
            {
                "rule_id": "external-id",
                "source": "/ocr/fields/invoice_number/value",
                "target": {"location": "body", "pointer": "/external_id"},
                "required": True,
                "on_missing": "error",
                "transforms": [],
            }
        ],
        deduplication_key_path="/ocr/fields/dedup_number/value",
    )

    outbound = MappingEngine().apply(_request(payload), mapping, _operation())
    canonical = json.dumps(
        {"document_id": "doc-001", "deduplication_value": 42},
        sort_keys=True,
        separators=(",", ":"),
    )

    assert outbound.operation_id == "upsertInvoice"
    assert outbound.method == "PUT"
    assert outbound.path == "/invoices/{external_id}"
    assert outbound.headers == {}
    assert outbound.json_body == {"external_id": "INV-001"}
    assert outbound.idempotency_key == hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@pytest.mark.parametrize(
    ("transform", "field_name", "field_value", "pattern"),
    [
        ("to_integer", "bad_integer", "not-a-number", "to_integer"),
        ("to_number", "bad_number", "not-a-number", "to_number"),
    ],
)
def test_mapping_wraps_invalid_numeric_conversions(
    transform: str,
    field_name: str,
    field_value: str,
    pattern: str,
) -> None:
    payload = sample_transfer_request(line_items=0)
    payload["ocr"]["fields"][field_name] = {
        "value": field_value,
        "value_type": "string",
        "confidence": 0.95,
        "status": "extracted",
    }
    mapping = _mapping(
        [
            {
                "rule_id": f"rule-{field_name}",
                "source": f"/ocr/fields/{field_name}/value",
                "target": {"location": "body", "pointer": f"/{field_name}"},
                "required": True,
                "on_missing": "error",
                "transforms": [transform],
            }
        ]
    )

    with pytest.raises(MappingValidationError, match=pattern):
        MappingEngine().preview(_request(payload), mapping, _operation())
