import json
import os
import socket
from pathlib import Path
import subprocess
from dataclasses import replace
import sys

import pytest
import yaml

from src.models.generated_validation import VerificationStatus
from src.services.generated_artifact_verifier import verify_generated_artifacts
from src.services.openapi_mcp_codegen import collect_operations, write_generated_artifacts
from src.services import generated_artifact_verifier


def _build_http_error_spec(status_code: int) -> dict:
    return {
        "openapi": "3.0.3",
        "info": {"title": "Error API", "version": "1.0.0"},
        "paths": {
            "/users/{userId}": {
                "get": {
                    "operationId": "getUser",
                    "parameters": [
                        {
                            "name": "userId",
                            "in": "path",
                            "required": True,
                            "schema": {"type": "integer", "minimum": 1},
                        }
                    ],
                    "responses": {
                        "200": {
                            "description": "Success",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "required": ["id"],
                                        "properties": {
                                            "id": {"type": "integer", "minimum": 1}
                                        },
                                    }
                                }
                            },
                        },
                        str(status_code): {"description": "Documented error"},
                    },
                }
            }
        },
    }


def _build_http_error_body_spec(status_code: int) -> dict:
    return {
        "openapi": "3.0.3",
        "info": {"title": "Error Body API", "version": "1.0.0"},
        "paths": {
            "/users/{userId}": {
                "get": {
                    "operationId": "getUser",
                    "parameters": [
                        {
                            "name": "userId",
                            "in": "path",
                            "required": True,
                            "schema": {"type": "integer", "minimum": 1},
                        }
                    ],
                    "responses": {
                        "200": {
                            "description": "Success",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "required": ["id"],
                                        "properties": {
                                            "id": {"type": "integer", "minimum": 1}
                                        },
                                    }
                                }
                            },
                        },
                        str(status_code): {
                            "description": "Documented error",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "required": ["code", "message"],
                                        "properties": {
                                            "code": {"type": "string"},
                                            "message": {"type": "string"},
                                        },
                                    }
                                }
                            },
                        },
                    },
                }
            }
        },
    }


def _build_transport_spec() -> dict:
    return {
        "openapi": "3.0.3",
        "info": {"title": "Transport API", "version": "1.0.0"},
        "paths": {
            "/records/{recordId}": {
                "get": {
                    "operationId": "getRecord",
                    "parameters": [
                        {
                            "name": "recordId",
                            "in": "path",
                            "required": True,
                            "schema": {"type": "integer", "minimum": 1},
                        }
                    ],
                    "responses": {
                        "200": {
                            "description": "Success",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "required": ["id"],
                                        "properties": {
                                            "id": {"type": "integer", "minimum": 1}
                                        },
                                    }
                                }
                            },
                        }
                    },
                }
            }
        },
    }


def _build_unvalidated_http_error_spec() -> dict:
    return {
        "openapi": "3.0.3",
        "info": {"title": "Malformed Error API", "version": "1.0.0"},
        "paths": {
            "/records/{recordId}": {
                "get": {
                    "operationId": "getRecord",
                    "parameters": [
                        {
                            "name": "recordId",
                            "in": "path",
                            "required": True,
                            "schema": {"type": "integer", "minimum": 1},
                        }
                    ],
                    "responses": {
                        "200": {
                            "description": "Success",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "required": ["id"],
                                        "properties": {
                                            "id": {"type": "integer", "minimum": 1}
                                        },
                                    }
                                }
                            },
                        },
                        "404": {
                            "description": "Error",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "$ref": "#/components/schemas/MissingErrorSchema"
                                    }
                                }
                            },
                        },
                    },
                }
            }
        },
    }


def _closed_local_base_url() -> str:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    return f"http://127.0.0.1:{port}"


def test_importing_verifier_does_not_import_cli_or_attempt_network() -> None:
    workspace_root = Path(__file__).resolve().parents[1]
    script = """
import importlib
import json
import socket
import sys

attempts = []

def blocked_connect(self, address):
    attempts.append(["connect", repr(address)])
    raise AssertionError(f"unexpected network connect: {address!r}")

def blocked_create_connection(address, *args, **kwargs):
    attempts.append(["create_connection", repr(address)])
    raise AssertionError(f"unexpected network create_connection: {address!r}")

def blocked_getaddrinfo(*args, **kwargs):
    attempts.append(["getaddrinfo", repr(args[:2])])
    raise AssertionError(f"unexpected network getaddrinfo: {args[:2]!r}")

socket.socket.connect = blocked_connect
socket.create_connection = blocked_create_connection
socket.getaddrinfo = blocked_getaddrinfo

module = importlib.import_module("src.services.generated_artifact_verifier")

print(json.dumps({
    "module": module.__name__,
    "cli_imported": "src.cli" in sys.modules,
    "network_attempts": attempts,
}, sort_keys=True))
"""
    env = {
        "HOME": os.environ.get("HOME", ""),
        "NO_PROXY": "*",
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": str(workspace_root),
        "PYTHONUNBUFFERED": "1",
    }

    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=workspace_root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr or completed.stdout
    payload = json.loads(completed.stdout)
    assert payload["module"] == "src.services.generated_artifact_verifier"
    assert payload["cli_imported"] is False
    assert payload["network_attempts"] == []


def test_verifier_builds_local_credentials_for_documented_security_schemes() -> None:
    spec = {
        "components": {
            "securitySchemes": {
                "apiToken": {
                    "type": "apiKey",
                    "in": "header",
                    "name": "X-Cybozu-API-Token",
                },
                "bearerAuth": {"type": "http", "scheme": "bearer"},
                "basicAuth": {"type": "http", "scheme": "basic"},
                "oauthAuth": {"type": "oauth2", "flows": {}},
            }
        }
    }

    environment = generated_artifact_verifier._base_env(spec)

    assert environment["KINTONE_API_TOKEN"] == "generated-verification-token"
    assert environment["OPENAPI_BEARER_TOKEN_BEARERAUTH"] == (
        "generated-verification-bearer"
    )
    assert environment["OPENAPI_BASIC_USERNAME_BASICAUTH"] == (
        "generated-verification-user"
    )
    assert environment["OPENAPI_BASIC_PASSWORD_BASICAUTH"] == (
        "generated-verification-password"
    )
    assert environment["OPENAPI_OAUTH_TOKEN_OAUTHAUTH"] == (
        "generated-verification-oauth"
    )


@pytest.mark.asyncio
async def test_verifier_calls_each_generated_tool_against_local_mock(tmp_path) -> None:
    spec = {
        "openapi": "3.0.3",
        "info": {"title": "Users API", "version": "1.0.0"},
        "paths": {
            "/users/{userId}": {
                "get": {
                    "operationId": "getUser",
                    "parameters": [
                        {
                            "name": "userId",
                            "in": "path",
                            "required": True,
                            "schema": {"type": "integer", "minimum": 1},
                        },
                        {
                            "name": "mode",
                            "in": "query",
                            "schema": {"type": "string", "enum": ["full", "summary"]},
                        },
                    ],
                    "responses": {
                        "200": {
                            "description": "Success",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "required": ["id", "name"],
                                        "properties": {
                                            "id": {"type": "integer", "minimum": 1},
                                            "name": {"type": "string", "minLength": 3},
                                        },
                                    }
                                }
                            },
                        }
                    },
                }
            }
        },
    }
    artifact_dir = tmp_path / "mcpserver"
    write_generated_artifacts(spec, artifact_dir)

    result = await verify_generated_artifacts(spec, artifact_dir, seed=5)

    assert result.status == VerificationStatus.PASSED
    assert result.spec_sha256
    assert {item.tool_name for item in result.operations} == {
        operation.tool_name for operation in collect_operations(spec)
    }
    assert all(item.request_valid for item in result.operations)
    assert all(item.response_valid for item in result.operations)


@pytest.mark.parametrize("status_code", [401, 404, 409, 429, 500])
@pytest.mark.asyncio
async def test_verifier_accepts_documented_http_error_responses(
    tmp_path, status_code: int
) -> None:
    spec = _build_http_error_spec(status_code)
    artifact_dir = tmp_path / "mcpserver"
    write_generated_artifacts(spec, artifact_dir)

    result = await verify_generated_artifacts(spec, artifact_dir, seed=5)

    error_result = next(
        item for item in result.operations if item.scenario_kind == "http_error"
    )
    success_result = next(
        item for item in result.operations if item.scenario_kind == "success"
    )

    assert error_result.request_valid is True
    assert error_result.response_valid is True
    assert error_result.status == VerificationStatus.PASSED
    assert error_result.error_kind == "http"
    assert error_result.failure_code is None
    assert error_result.redacted_output["status_code"] == status_code
    assert success_result.request_valid is True
    assert success_result.response_valid is True
    assert success_result.status == VerificationStatus.PASSED


@pytest.mark.asyncio
async def test_verifier_marks_documented_http_error_bodies_as_unvalidated(
    tmp_path,
) -> None:
    spec = _build_http_error_body_spec(404)
    artifact_dir = tmp_path / "mcpserver"
    write_generated_artifacts(spec, artifact_dir)

    result = await verify_generated_artifacts(spec, artifact_dir, seed=7)

    success_result = next(
        item for item in result.operations if item.scenario_kind == "success"
    )
    error_result = next(
        item for item in result.operations if item.scenario_kind == "http_error"
    )

    assert success_result.status == VerificationStatus.PASSED
    assert error_result.request_valid is True
    assert error_result.response_valid is False
    assert error_result.status == VerificationStatus.UNVALIDATED
    assert error_result.failure_code == "unvalidated_response_schema"
    assert "cannot be validated" in (error_result.message or "")


@pytest.mark.asyncio
async def test_verifier_marks_unvalidated_documented_http_errors_as_unvalidated(
    tmp_path,
) -> None:
    spec = _build_unvalidated_http_error_spec()
    artifact_dir = tmp_path / "mcpserver"
    write_generated_artifacts(spec, artifact_dir)

    result = await verify_generated_artifacts(spec, artifact_dir, seed=11)

    success_result = next(
        item for item in result.operations if item.scenario_kind == "success"
    )
    error_result = next(
        item for item in result.operations if item.scenario_kind == "http_error"
    )

    assert success_result.status == VerificationStatus.PASSED
    assert error_result.request_valid is True
    assert error_result.response_valid is False
    assert error_result.status == VerificationStatus.UNVALIDATED
    assert error_result.failure_code == "unvalidated_response_schema"
    assert "unresolved reference" in (error_result.message or "")


@pytest.mark.asyncio
async def test_verifier_maps_unreachable_upstream_to_connection(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = _build_transport_spec()
    artifact_dir = tmp_path / "mcpserver"
    write_generated_artifacts(spec, artifact_dir)

    closed_base_url = _closed_local_base_url()
    monkeypatch.setattr(
        generated_artifact_verifier.OpenAPIMockServer,
        "start",
        lambda self: closed_base_url,
    )
    monkeypatch.setattr(
        generated_artifact_verifier.OpenAPIMockServer,
        "stop",
        lambda self: None,
    )

    result = await verify_generated_artifacts(spec, artifact_dir, seed=19)

    operation_result = result.operations[0]

    assert result.status == VerificationStatus.FAILED
    assert operation_result.error_kind == "connection"


@pytest.mark.asyncio
async def test_verifier_maps_short_timeout_to_timeout(tmp_path) -> None:
    spec = _build_transport_spec()
    artifact_dir = tmp_path / "mcpserver"
    write_generated_artifacts(spec, artifact_dir)

    original_builder = generated_artifact_verifier.build_mock_scenarios

    def build_timeout_scenarios(openapi_spec, *, seed: int = 0):
        scenarios = original_builder(openapi_spec, seed=seed)
        return [
            replace(scenario, response_delay_seconds=0.2)
            if scenario.scenario_kind == "success"
            else scenario
            for scenario in scenarios
        ]

    generated_artifact_verifier.build_mock_scenarios = build_timeout_scenarios
    try:
        result = await verify_generated_artifacts(
            spec,
            artifact_dir,
            seed=23,
            timeout_seconds=0.05,
        )
    finally:
        generated_artifact_verifier.build_mock_scenarios = original_builder

    operation_result = result.operations[0]

    assert result.status == VerificationStatus.FAILED
    assert operation_result.request_valid is True
    assert operation_result.error_kind == "timeout"


@pytest.mark.asyncio
async def test_verifier_maps_invalid_json_to_invalid_json(tmp_path) -> None:
    spec = _build_transport_spec()
    artifact_dir = tmp_path / "mcpserver"
    write_generated_artifacts(spec, artifact_dir)

    original_builder = generated_artifact_verifier.build_mock_scenarios

    def build_invalid_json_scenarios(openapi_spec, *, seed: int = 0):
        scenarios = original_builder(openapi_spec, seed=seed)
        return [
            replace(
                scenario,
                response_body="{not-json",
                response_headers={"content-type": "application/json"},
                response_mode="raw",
            )
            if scenario.scenario_kind == "success"
            else scenario
            for scenario in scenarios
        ]

    generated_artifact_verifier.build_mock_scenarios = build_invalid_json_scenarios
    try:
        result = await verify_generated_artifacts(spec, artifact_dir, seed=29)
    finally:
        generated_artifact_verifier.build_mock_scenarios = original_builder

    operation_result = result.operations[0]

    assert result.status == VerificationStatus.FAILED
    assert operation_result.request_valid is True
    assert operation_result.error_kind == "invalid_json"


@pytest.mark.asyncio
async def test_verifier_validates_request_body_path_query_and_accepts_204(
    tmp_path,
) -> None:
    spec = {
        "openapi": "3.0.3",
        "info": {"title": "Verifier API", "version": "1.0.0"},
        "paths": {
            "/users/{userId}": {
                "post": {
                    "operationId": "createUser",
                    "parameters": [
                        {
                            "name": "userId",
                            "in": "path",
                            "required": True,
                            "schema": {"type": "integer", "minimum": 1},
                        },
                        {
                            "name": "mode",
                            "in": "query",
                            "required": True,
                            "schema": {"type": "string", "enum": ["sync", "async"]},
                        },
                    ],
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "required": ["name"],
                                    "properties": {
                                        "name": {"type": "string", "minLength": 3},
                                        "email": {"type": "string", "format": "email"},
                                    },
                                }
                            }
                        },
                    },
                    "responses": {
                        "201": {
                            "description": "Created",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "required": ["id", "email"],
                                        "properties": {
                                            "id": {"type": "integer", "minimum": 1},
                                            "email": {"type": "string", "format": "email"},
                                        },
                                    }
                                }
                            },
                        }
                    },
                }
            },
            "/users/{userId}/archive": {
                "delete": {
                    "operationId": "archiveUser",
                    "parameters": [
                        {
                            "name": "userId",
                            "in": "path",
                            "required": True,
                            "schema": {"type": "integer", "minimum": 1},
                        }
                    ],
                    "responses": {"204": {"description": "Archived"}},
                }
            },
        },
    }
    artifact_dir = tmp_path / "mcpserver"
    write_generated_artifacts(spec, artifact_dir)

    result = await verify_generated_artifacts(spec, artifact_dir, seed=7)

    assert result.status == VerificationStatus.PASSED
    assert {item.tool_name for item in result.operations} == {"createUser", "archiveUser"}
    create_result = next(item for item in result.operations if item.tool_name == "createUser")
    archive_result = next(item for item in result.operations if item.tool_name == "archiveUser")

    assert create_result.request_valid is True
    assert create_result.response_valid is True
    assert create_result.status == VerificationStatus.PASSED

    assert archive_result.request_valid is True
    assert archive_result.response_valid is True
    assert archive_result.status == VerificationStatus.PASSED
    assert archive_result.error_kind is None
    assert archive_result.failure_code is None


@pytest.mark.asyncio
async def test_verifier_marks_missing_response_schema_as_unvalidated(tmp_path) -> None:
    spec = yaml.safe_load(
        Path("examples/minimal_users_api.yaml").read_text(encoding="utf-8")
    )
    artifact_dir = tmp_path / "mcpserver"
    write_generated_artifacts(spec, artifact_dir)

    result = await verify_generated_artifacts(spec, artifact_dir, seed=11)

    assert result.status == VerificationStatus.UNVALIDATED
    assert len(result.operations) == 1
    operation_result = result.operations[0]
    assert operation_result.tool_name == "get_users"
    assert operation_result.status == VerificationStatus.UNVALIDATED
    assert operation_result.request_valid is True
    assert operation_result.response_valid is False
    assert operation_result.failure_code == "unvalidated_response_schema"


@pytest.mark.asyncio
async def test_verifier_keeps_other_results_when_one_operation_fails(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = {
        "openapi": "3.0.3",
        "info": {"title": "Mixed API", "version": "1.0.0"},
        "paths": {
            "/ok": {
                "get": {
                    "operationId": "listOk",
                    "responses": {
                        "200": {
                            "description": "Success",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "required": ["status"],
                                        "properties": {
                                            "status": {"type": "string", "enum": ["ready"]}
                                        },
                                    }
                                }
                            },
                        }
                    },
                }
            },
            "/broken": {
                "get": {
                    "operationId": "listBroken",
                    "responses": {
                        "200": {
                            "description": "Success",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "required": ["status"],
                                        "properties": {
                                            "status": {"type": "string", "enum": ["ready"]}
                                        },
                                    }
                                }
                            },
                        }
                    },
                }
            },
        },
    }
    artifact_dir = tmp_path / "mcpserver"
    write_generated_artifacts(spec, artifact_dir)

    original_builder = generated_artifact_verifier.build_mock_scenarios

    def build_invalid_scenarios(openapi_spec, *, seed: int = 0):
        scenarios = original_builder(openapi_spec, seed=seed)
        return [
            replace(scenario, response_body={"status": "wrong"})
            if scenario.tool_name == "listBroken" and scenario.scenario_kind == "success"
            else scenario
            for scenario in scenarios
        ]

    monkeypatch.setattr(
        generated_artifact_verifier,
        "build_mock_scenarios",
        build_invalid_scenarios,
    )

    result = await verify_generated_artifacts(spec, artifact_dir, seed=13)

    assert result.status == VerificationStatus.FAILED
    assert {item.tool_name for item in result.operations} == {"listOk", "listBroken"}

    ok_result = next(item for item in result.operations if item.tool_name == "listOk")
    broken_result = next(item for item in result.operations if item.tool_name == "listBroken")

    assert ok_result.status == VerificationStatus.PASSED
    assert broken_result.status == VerificationStatus.FAILED
    assert broken_result.failure_code == "response_schema_mismatch"


@pytest.mark.asyncio
async def test_verifier_redacts_secret_query_and_opaque_body_values_in_report(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = {
        "openapi": "3.0.3",
        "info": {"title": "Secret API", "version": "1.0.0"},
        "paths": {
            "/users/{userId}": {
                "post": {
                    "operationId": "updateUser",
                    "parameters": [
                        {
                            "name": "userId",
                            "in": "path",
                            "required": True,
                            "schema": {"type": "integer", "minimum": 1},
                        },
                        {
                            "name": "cursor",
                            "in": "query",
                            "required": True,
                            "schema": {"type": "string", "minLength": 3},
                        },
                    ],
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "required": ["note", "name"],
                                    "properties": {
                                        "note": {"type": "string", "minLength": 3},
                                        "name": {"type": "string", "minLength": 3},
                                    },
                                }
                            }
                        },
                    },
                    "responses": {
                        "200": {
                            "description": "Success",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "required": ["ok"],
                                        "properties": {
                                            "ok": {"type": "boolean"}
                                        },
                                    }
                                }
                            },
                        }
                    },
                }
            }
        },
    }
    artifact_dir = tmp_path / "mcpserver"
    write_generated_artifacts(spec, artifact_dir)

    query_secret = "cursor-secret-value"
    body_secret = "opaque-body-secret-value"
    monkeypatch.setenv("SERVICE_AUTH_TOKEN", query_secret)
    monkeypatch.setenv("SERVICE_PASSWORD", body_secret)

    original_builder = generated_artifact_verifier.build_mock_scenarios

    def build_secret_scenarios(openapi_spec, *, seed: int = 0):
        scenarios = original_builder(openapi_spec, seed=seed)
        return [
            replace(
                scenario,
                tool_arguments={
                    **scenario.tool_arguments,
                    "user_id": 1,
                    "cursor": query_secret,
                    "body": {
                        "note": body_secret,
                        "name": "visible-name",
                    },
                },
                response_body={"ok": True},
            )
            if scenario.tool_name == "updateUser" and scenario.scenario_kind == "success"
            else scenario
            for scenario in scenarios
        ]

    monkeypatch.setattr(
        generated_artifact_verifier,
        "build_mock_scenarios",
        build_secret_scenarios,
    )

    result = await verify_generated_artifacts(spec, artifact_dir, seed=17)

    assert result.status == VerificationStatus.PASSED
    assert len(result.operations) == 1
    operation_result = result.operations[0]
    assert operation_result.request_valid is True
    assert operation_result.redacted_input == {
        "method": "POST",
        "path": "/users/1",
        "query": {"cursor": ["[REDACTED]"]},
        "body": {
            "note": "[REDACTED]",
            "name": "visible-name",
        },
    }

    serialized = json.dumps(result.model_dump(mode="json"), sort_keys=True)

    assert query_secret not in serialized
    assert body_secret not in serialized
    assert "[REDACTED]" in serialized


def test_invoke_generated_client_converts_subprocess_timeout_to_error(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def raise_timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=["client.py"], timeout=5)

    monkeypatch.setattr(generated_artifact_verifier.subprocess, "run", raise_timeout)

    invocation = generated_artifact_verifier._invoke_generated_client(
        tmp_path,
        "http://127.0.0.1:9001/mcp/",
        "getRecord",
        {"id": 1},
        {"PATH": os.environ.get("PATH", "")},
        1.0,
    )

    assert invocation.returncode == 124
    assert invocation.payload == {
        "error": {
            "kind": "timeout",
            "message": "generated MCP client timed out",
        }
    }