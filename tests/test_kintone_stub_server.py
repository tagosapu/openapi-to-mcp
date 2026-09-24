import json

import pytest
from fastapi.testclient import TestClient

from examples.kintone_stub_server import app

client = TestClient(app)
HEADERS = {"X-Cybozu-API-Token": "mock-token"}


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
        json={
            "app": 1,
            "records": [
                {
                    "ocr_id": {"type": "SINGLE_LINE_TEXT", "value": "ocr-002"},
                    "status": {"type": "DROP_DOWN", "value": "registered"},
                }
            ],
        },
        headers=HEADERS,
    )
    assert response.status_code == 200
    record_id = response.json()["ids"][0]

    response = client.put(
        "/k/v1/record.json",
        json={
            "app": 1,
            "id": record_id,
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


def test_transfer_validation_rejects_invalid_record_without_partial_write() -> None:
    response = client.post(
        "/k/v1/records.json",
        json={
            "app": 1,
            "records": [
                {
                    "document_id": {
                        "type": "SINGLE_LINE_TEXT",
                        "value": "",
                    },
                    "text": {
                        "type": "MULTI_LINE_TEXT",
                        "value": "invalid document",
                    },
                    "status": {"type": "DROP_DOWN", "value": "registered"},
                }
            ],
        },
        headers=HEADERS,
    )

    assert response.status_code == 400
    assert response.json() == {
        "code": "MOCK_RE02",
        "id": "mock-error",
        "message": "document_id is required",
    }

    records = client.get(
        "/k/v1/records.json",
        params={"app": 1},
        headers=HEADERS,
    ).json()["records"]
    assert len(records) == 1
