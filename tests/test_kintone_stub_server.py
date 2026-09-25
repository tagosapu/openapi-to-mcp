import json

import pytest
from fastapi.testclient import TestClient

from examples.kintone_stub_server import KINTONE_APP_FIELDS, app

client = TestClient(app)
HEADERS = {"X-Cybozu-API-Token": "mock-token"}


def _valid_record(invoice_number: str = "INV-TEST-002") -> dict[str, object]:
    return {
        "invoice_number": {
            "type": "SINGLE_LINE_TEXT",
            "value": invoice_number,
        },
        "invoice_date": {"type": "DATE", "value": "2026-09-20"},
        "vendor_name": {"type": "SINGLE_LINE_TEXT", "value": "Test Vendor"},
        "subtotal": {"type": "NUMBER", "value": "10000"},
        "tax_amount": {"type": "NUMBER", "value": "1000"},
        "total_amount": {"type": "NUMBER", "value": "11000"},
        "currency": {"type": "DROP_DOWN", "value": "JPY"},
        "status": {"type": "DROP_DOWN", "value": "registered"},
        "ocr_confidence": {"type": "NUMBER", "value": "0.95"},
        "source_file": {"type": "SINGLE_LINE_TEXT", "value": "test.pdf"},
        "ocr_text_ref": {
            "type": "SINGLE_LINE_TEXT",
            "value": "object://ocr-text/test-002",
        },
        "line_items": {
            "type": "SUBTABLE",
            "value": [
                {
                    "value": {
                        "description": {
                            "type": "SINGLE_LINE_TEXT",
                            "value": "Test item",
                        },
                        "quantity": {"type": "NUMBER", "value": "1"},
                        "unit_price": {"type": "NUMBER", "value": "10000"},
                        "amount": {"type": "NUMBER", "value": "10000"},
                    }
                }
            ],
        },
    }


def setup_function() -> None:
    response = client.post("/__mock/reset")
    assert response.status_code == 200


def test_requires_kintone_api_token() -> None:
    response = client.get("/k/v1/records.json", params={"app": 1})

    assert response.status_code == 401
    assert response.json()["code"] == "CB_AU01"


def test_record_crud_and_request_log() -> None:
    response = client.get(
        "/k/v1/records.json",
        params={"app": 1, "query": 'status = "registered"', "totalCount": True},
        headers=HEADERS,
    )
    assert response.status_code == 200
    assert response.json()["totalCount"] == "1"

    response = client.post(
        "/k/v1/records.json",
        json={"app": 1, "records": [_valid_record()]},
        headers=HEADERS,
    )
    assert response.status_code == 200
    record_id = response.json()["ids"][0]

    response = client.put(
        "/k/v1/record.json",
        json={
            "app": 1,
            "updateKey": {"field": "invoice_number", "value": "INV-TEST-002"},
            "record": {"status": {"value": "processed"}},
        },
        headers=HEADERS,
    )
    assert response.status_code == 200

    response = client.get(
        "/k/v1/record.json",
        params={"app": 1, "id": record_id},
        headers=HEADERS,
    )
    assert response.status_code == 200
    assert response.json()["record"]["status"]["value"] == "processed"

    response = client.request(
        "DELETE",
        "/k/v1/records.json",
        json={"app": 1, "ids": [record_id]},
        headers=HEADERS,
    )
    assert response.status_code == 200

    request_log = client.get("/__mock/requests").json()["requests"]
    assert any(
        item["method"] == "POST" and item["path"] == "/k/v1/records.json"
        for item in request_log
    )
    assert all("has_api_token" in item for item in request_log)


@pytest.mark.parametrize("status_code", [401, 404, 409, 429, 500])
def test_mock_can_reproduce_http_error(status_code: int) -> None:
    response = client.get(f"/__mock/errors/{status_code}")

    assert response.status_code == status_code
    assert response.json() == {
        "code": f"MOCK_{status_code}",
        "id": "mock-error",
        "message": f"simulated status {status_code}",
    }
    if status_code == 429:
        assert response.headers["Retry-After"] == "1"


def test_mock_request_history_redacts_invalid_token() -> None:
    client.get(
        "/k/v1/records.json",
        params={"app": 1},
        headers={"X-Cybozu-API-Token": "wrong-token"},
    )

    history = json.dumps(client.get("/__mock/requests").json())
    assert "wrong-token" not in history


def test_get_over_post_matches_get_records() -> None:
    expected = client.get(
        "/k/v1/records.json",
        params={"app": 1, "query": 'status = "registered"', "totalCount": True},
        headers=HEADERS,
    ).json()
    response = client.post(
        "/k/v1/records.json",
        json={"app": 1, "query": 'status = "registered"', "totalCount": True},
        headers={**HEADERS, "X-HTTP-Method-Override": "GET"},
    )

    assert response.status_code == 200
    assert response.json() == expected


def test_app_form_fields_match_target_and_unknown_codes_are_ignored() -> None:
    response = client.get(
        "/k/v1/app/form/fields.json",
        params={"app": 1},
        headers=HEADERS,
    )

    assert response.status_code == 200
    properties = response.json()["properties"]
    assert properties["invoice_number"]["type"] == "SINGLE_LINE_TEXT"
    assert properties["invoice_number"]["unique"] is True
    assert properties["line_items"]["type"] == "SUBTABLE"
    assert "document_id" not in properties
    assert set(properties) == set(KINTONE_APP_FIELDS)

    record = _valid_record("INV-UNKNOWN-FIELD")
    record["document_id"] = {
        "type": "SINGLE_LINE_TEXT",
        "value": "ocr-only-metadata",
    }
    created = client.post(
        "/k/v1/records.json",
        json={"app": 1, "records": [record]},
        headers=HEADERS,
    )
    assert created.status_code == 200

    stored = client.get(
        "/k/v1/record.json",
        params={"app": 1, "id": created.json()["ids"][0]},
        headers=HEADERS,
    )
    assert stored.status_code == 200
    assert "document_id" not in stored.json()["record"]


def test_number_field_values_must_follow_kintone_string_shape() -> None:
    record = _valid_record("INV-NUMBER-SHAPE")
    record["total_amount"] = {"type": "NUMBER", "value": 11000}

    response = client.post(
        "/k/v1/records.json",
        json={"app": 1, "records": [record]},
        headers=HEADERS,
    )

    assert response.status_code == 400
    assert response.json() == {
        "code": "MOCK_RE03",
        "id": "mock-error",
        "message": "total_amount must be a string number",
    }


def test_transfer_validation_rejects_invalid_record_without_partial_write() -> None:
    invalid_record = _valid_record("INV-MISSING-NUMBER")
    invalid_record["invoice_number"] = {
        "type": "SINGLE_LINE_TEXT",
        "value": "",
    }
    response = client.post(
        "/k/v1/records.json",
        json={"app": 1, "records": [invalid_record]},
        headers=HEADERS,
    )

    assert response.status_code == 400
    assert response.json() == {
        "code": "MOCK_RE02",
        "id": "mock-error",
        "message": "invoice_number is required",
    }

    records = client.get(
        "/k/v1/records.json",
        params={"app": 1},
        headers=HEADERS,
    ).json()["records"]
    assert len(records) == 1
