import json
from dataclasses import asdict

import httpx
import pytest

from src.services.openapi_mock_data import MockOperationScenario
from src.services.openapi_mock_server import OpenAPIMockServer


def test_mock_server_matches_method_and_path_and_records_query() -> None:
    scenarios = [
        MockOperationScenario(
            operation_id="getUser",
            tool_name="getUser",
            method="GET",
            path="/users/{id}",
            tool_arguments={"id": 7},
            status_code=200,
            response_headers={"content-type": "application/json"},
            response_body={"id": 7, "name": "mock-user"},
            scenario_kind="success",
        ),
        MockOperationScenario(
            operation_id="createUser",
            tool_name="createUser",
            method="POST",
            path="/users/{id}",
            tool_arguments={"id": 7, "body": {"name": "posted-user"}},
            status_code=201,
            response_headers={"content-type": "application/json"},
            response_body={"created": True},
            scenario_kind="success",
        ),
    ]
    server = OpenAPIMockServer(scenarios)

    base_url = server.start()

    try:
        assert base_url.startswith("http://127.0.0.1:")

        response = httpx.get(
            f"{base_url}/users/7?expand=groups&expand=roles",
            timeout=2,
        )

        assert response.status_code == 200
        assert response.json() == {"id": 7, "name": "mock-user"}

        recorded = server.requests()

        assert len(recorded) == 1
        assert recorded[0].method == "GET"
        assert recorded[0].path == "/users/7"
        assert recorded[0].query == {"expand": ["groups", "roles"]}
        assert recorded[0].body is None
    finally:
        server.stop()


def test_mock_server_returns_documented_error_and_redacts_credentials() -> None:
    scenario = MockOperationScenario(
        operation_id="updateUser",
        tool_name="updateUser",
        method="POST",
        path="/users/{id}",
        tool_arguments={"id": 7},
        status_code=404,
        response_headers={"content-type": "application/json"},
        response_body={"code": "not_found", "message": "missing"},
        scenario_kind="http_error",
    )
    server = OpenAPIMockServer([scenario])

    base_url = server.start()

    try:
        response = httpx.post(
            f"{base_url}/users/7?verbose=true",
            headers={
                "Authorization": "Bearer secret-token",
                "X-Cybozu-Api-Token": "cybozu-secret",
                "X-Trace-Id": "trace-123",
            },
            json={
                "name": "visible",
                "password": "super-secret",
                "profile": {
                    "api_key": "top-secret",
                    "nested": [
                        {"token": "nested-secret"},
                        {"authorization": "another-secret"},
                    ],
                },
            },
            timeout=2,
        )

        assert response.status_code == 404
        assert response.json() == {"code": "not_found", "message": "missing"}

        recorded = server.requests()

        assert len(recorded) == 1
        assert recorded[0].path == "/users/7"
        assert recorded[0].query == {"verbose": ["true"]}
        assert _header_value(recorded[0].headers, "authorization") == "[REDACTED]"
        assert _header_value(recorded[0].headers, "x-cybozu-api-token") == "[REDACTED]"
        assert _header_value(recorded[0].headers, "x-trace-id") == "trace-123"
        assert recorded[0].body == {
            "name": "visible",
            "password": "[REDACTED]",
            "profile": {
                "api_key": "[REDACTED]",
                "nested": [
                    {"token": "[REDACTED]"},
                    {"authorization": "[REDACTED]"},
                ],
            },
        }

        serialized = json.dumps([asdict(item) for item in recorded], sort_keys=True)

        assert "secret-token" not in serialized
        assert "cybozu-secret" not in serialized
        assert "super-secret" not in serialized
        assert "top-secret" not in serialized
        assert "nested-secret" not in serialized
        assert "another-secret" not in serialized
    finally:
        server.stop()


def test_mock_server_redacts_secret_values_in_query_headers_and_opaque_body_fields() -> None:
    scenario = MockOperationScenario(
        operation_id="updateUser",
        tool_name="updateUser",
        method="POST",
        path="/users/{id}",
        tool_arguments={"id": 7},
        status_code=200,
        response_headers={"content-type": "application/json"},
        response_body={"ok": True},
        scenario_kind="success",
    )
    server = OpenAPIMockServer(
        [scenario],
        secret_values=["cursor-secret", "trace-secret", "opaque-body-secret"],
    )

    base_url = server.start()

    try:
        response = httpx.post(
            f"{base_url}/users/7?cursor=cursor-secret&cursor=public&api_key=visible-key",
            headers={
                "X-Trace-Id": "trace-secret",
                "X-Visible": "visible",
            },
            json={
                "note": "opaque-body-secret",
                "nested": ["keep", {"label": "opaque-body-secret"}],
                "visible": "public",
            },
            timeout=2,
        )

        assert response.status_code == 200
        assert response.json() == {"ok": True}

        recorded = server.requests()

        assert len(recorded) == 1
        assert recorded[0].query == {
            "cursor": ["[REDACTED]", "public"],
            "api_key": ["[REDACTED]"],
        }
        assert _header_value(recorded[0].headers, "x-trace-id") == "[REDACTED]"
        assert _header_value(recorded[0].headers, "x-visible") == "visible"
        assert recorded[0].body == {
            "note": "[REDACTED]",
            "nested": ["keep", {"label": "[REDACTED]"}],
            "visible": "public",
        }

        serialized = json.dumps([asdict(item) for item in recorded], sort_keys=True)

        assert "cursor-secret" not in serialized
        assert "trace-secret" not in serialized
        assert "opaque-body-secret" not in serialized
    finally:
        server.stop()


def test_mock_server_can_activate_same_route_scenarios_independently() -> None:
    success = MockOperationScenario(
        operation_id="getUser",
        tool_name="getUser",
        method="GET",
        path="/users/{id}",
        tool_arguments={"id": 7},
        status_code=200,
        response_headers={"content-type": "application/json"},
        response_body={"id": 7, "name": "mock-user"},
        scenario_kind="success",
    )
    documented_error = MockOperationScenario(
        operation_id="getUser",
        tool_name="getUser",
        method="GET",
        path="/users/{id}",
        tool_arguments={"id": 7},
        status_code=404,
        response_headers={"content-type": "application/json"},
        response_body={"code": "not_found", "message": "missing"},
        scenario_kind="http_error",
    )
    server = OpenAPIMockServer([success, documented_error])

    base_url = server.start()

    try:
        default_response = httpx.get(f"{base_url}/users/7", timeout=2)
        assert default_response.status_code == 200
        assert default_response.json() == {"id": 7, "name": "mock-user"}

        server.activate_scenario(documented_error)

        error_response = httpx.get(f"{base_url}/users/7", timeout=2)
        assert error_response.status_code == 404
        assert error_response.json() == {"code": "not_found", "message": "missing"}

        server.activate_scenario(success)

        success_response = httpx.get(f"{base_url}/users/7", timeout=2)
        assert success_response.status_code == 200
        assert success_response.json() == {"id": 7, "name": "mock-user"}
    finally:
        server.stop()


def test_activate_scenario_rejects_unknown_scenario() -> None:
    known = MockOperationScenario(
        operation_id="getUser",
        tool_name="getUser",
        method="GET",
        path="/users/{id}",
        tool_arguments={"id": 7},
        status_code=200,
        response_headers={"content-type": "application/json"},
        response_body={"id": 7, "name": "mock-user"},
        scenario_kind="success",
    )
    unknown = MockOperationScenario(
        operation_id="getUser",
        tool_name="getUser",
        method="GET",
        path="/users/{id}",
        tool_arguments={"id": 7},
        status_code=404,
        response_headers={"content-type": "application/json"},
        response_body={"code": "not_found"},
        scenario_kind="http_error",
    )
    server = OpenAPIMockServer([known])

    with pytest.raises(ValueError, match="supplied when this server was created"):
        server.activate_scenario(unknown)


def test_mock_server_stop_is_idempotent() -> None:
    scenario = MockOperationScenario(
        operation_id="listUsers",
        tool_name="listUsers",
        method="GET",
        path="/users",
        tool_arguments={},
        status_code=200,
        response_headers={"content-type": "application/json"},
        response_body=[{"id": 1}],
        scenario_kind="success",
    )
    server = OpenAPIMockServer([scenario])

    server.start()
    server.stop()
    server.stop()


def _header_value(headers: dict[str, str], name: str) -> str | None:
    for key, value in headers.items():
        if key.lower() == name.lower():
            return value
    return None