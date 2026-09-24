import httpx
import pytest

from src.services.generated_mcp_runtime import (
    GeneratedMcpRuntime,
    StructuredApiError,
    resolve_auth_headers,
)


def test_kintone_api_token_is_added_to_the_expected_header():
    headers = resolve_auth_headers(
        [{"apiToken": []}],
        {
            "apiToken": {
                "type": "apiKey",
                "in": "header",
                "name": "X-Cybozu-API-Token",
            }
        },
        {"KINTONE_API_TOKEN": "mock-token"},
    )

    assert headers == {"X-Cybozu-API-Token": "mock-token"}


def test_security_requirements_are_or_alternatives():
    headers = resolve_auth_headers(
        [{"apiToken": []}, {"oauth2": ["k:app_record:read"]}],
        {
            "apiToken": {
                "type": "apiKey",
                "in": "header",
                "name": "X-Cybozu-API-Token",
            },
            "oauth2": {"type": "oauth2"},
        },
        {"OPENAPI_OAUTH_TOKEN_OAUTH2": "bearer-token"},
    )

    assert headers == {"Authorization": "Bearer bearer-token"}


def test_basic_auth_requires_both_credentials_and_returns_basic_header():
    headers = resolve_auth_headers(
        [{"basicAuth": []}],
        {"basicAuth": {"type": "http", "scheme": "basic"}},
        {
            "OPENAPI_BASIC_USERNAME_BASICAUTH": "user",
            "OPENAPI_BASIC_PASSWORD_BASICAUTH": "pass",
        },
    )

    assert headers["Authorization"].startswith("Basic ")


def test_security_schemes_inside_one_requirement_are_and_combined():
    headers = resolve_auth_headers(
        [{"first": [], "second": []}],
        {
            "first": {"type": "apiKey", "in": "header", "name": "X-First"},
            "second": {"type": "apiKey", "in": "header", "name": "X-Second"},
        },
        {
            "OPENAPI_API_KEY_FIRST": "one",
            "OPENAPI_API_KEY_SECOND": "two",
        },
    )

    assert headers == {"X-First": "one", "X-Second": "two"}


def test_missing_auth_alternatives_are_configuration_errors():
    with pytest.raises(StructuredApiError) as error:
        resolve_auth_headers(
            [{"apiToken": []}],
            {
                "apiToken": {
                    "type": "apiKey",
                    "in": "header",
                    "name": "X-Cybozu-API-Token",
                }
            },
            {},
        )

    assert error.value.kind == "configuration"
    assert "token" not in error.value.message.lower()


def test_request_assembles_httpx_arguments(monkeypatch):
    captured = {}

    def fake_request(_client, method, url, **kwargs):
        captured.update(method=method, url=url, kwargs=kwargs)
        return httpx.Response(200, json={"ok": True})

    monkeypatch.setattr(httpx.Client, "request", fake_request)
    runtime = GeneratedMcpRuntime("http://127.0.0.1:9100", timeout_seconds=7.5)

    result = runtime.request(
        {
            "method": "POST",
            "path": "/records/{recordId}",
            "operation_id": "postRecord",
            "parameters": [],
            "request_body": None,
            "security": [],
            "security_schemes": {},
        },
        {
            "path": {"recordId": "a/b"},
            "query": {"fields": ["name"]},
            "headers": {"X-Trace": "trace"},
            "body": {"name": "Ada"},
        },
    )

    assert result == {"ok": True}
    assert captured["method"] == "POST"
    assert captured["url"] == "http://127.0.0.1:9100/records/a%2Fb"
    assert captured["kwargs"]["params"] == {"fields": ["name"]}
    assert captured["kwargs"]["headers"]["X-Trace"] == "trace"
    assert captured["kwargs"]["json"] == {"name": "Ada"}
    assert captured["kwargs"]["timeout"] == 7.5


def test_timeout_is_classified_without_exposing_request_data(monkeypatch):
    def fake_request(_client, _method, _url, **_kwargs):
        raise httpx.TimeoutException("token=secret")

    monkeypatch.setattr(httpx.Client, "request", fake_request)
    runtime = GeneratedMcpRuntime("http://127.0.0.1:9100")

    with pytest.raises(StructuredApiError) as error:
        runtime.request(
            {
                "method": "GET",
                "path": "/records",
                "operation_id": "getRecords",
                "parameters": [],
                "request_body": None,
                "security": [],
                "security_schemes": {},
            },
            {},
        )

    assert error.value.kind == "timeout"
    assert "secret" not in error.value.message


def test_connection_error_is_classified(monkeypatch):
    def fake_request(_client, _method, _url, **_kwargs):
        raise httpx.ConnectError("connection details")

    monkeypatch.setattr(httpx.Client, "request", fake_request)
    runtime = GeneratedMcpRuntime("http://127.0.0.1:9100")

    with pytest.raises(StructuredApiError) as error:
        runtime.request(
            {
                "method": "GET",
                "path": "/records",
                "operation_id": "getRecords",
                "parameters": [],
                "request_body": None,
                "security": [],
                "security_schemes": {},
            },
            {},
        )

    assert error.value.kind == "connection"


def test_http_error_contains_target_code_and_redacts_token(monkeypatch):
    def fake_request(_client, _method, _url, **_kwargs):
        return httpx.Response(
            401,
            json={"code": "CB_AU01", "message": "bad token secret-token"},
        )

    monkeypatch.setattr(httpx.Client, "request", fake_request)
    runtime = GeneratedMcpRuntime(
        "http://127.0.0.1:9100", environ={"KINTONE_API_TOKEN": "secret-token"}
    )

    with pytest.raises(StructuredApiError) as error:
        runtime.request(
            {
                "method": "GET",
                "path": "/records",
                "operation_id": "getRecords",
                "parameters": [],
                "request_body": None,
                "security": [],
                "security_schemes": {},
            },
            {},
        )

    assert error.value.as_dict() == {
        "error": {
            "kind": "http",
            "operation_id": "getRecords",
            "status_code": 401,
            "target_code": "CB_AU01",
            "message": "bad token [REDACTED]",
        }
    }


def test_invalid_json_is_classified_without_response_bytes(monkeypatch):
    def fake_request(_client, _method, _url, **_kwargs):
        return httpx.Response(
            200,
            content=b"not-json-secret",
            headers={"Content-Type": "application/json"},
        )

    monkeypatch.setattr(httpx.Client, "request", fake_request)
    runtime = GeneratedMcpRuntime("http://127.0.0.1:9100")

    with pytest.raises(StructuredApiError) as error:
        runtime.request(
            {
                "method": "GET",
                "path": "/records",
                "operation_id": "getRecords",
                "parameters": [],
                "request_body": None,
                "security": [],
                "security_schemes": {},
            },
            {},
        )

    assert error.value.kind == "invalid_json"
    assert "not-json-secret" not in error.value.message