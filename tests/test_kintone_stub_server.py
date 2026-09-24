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
