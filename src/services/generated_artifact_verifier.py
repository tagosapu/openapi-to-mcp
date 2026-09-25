"""Generated artifact verification against a local OpenAPI mock server."""

from __future__ import annotations

import copy
import ast
import asyncio
import json
import os
import re
import socket
import subprocess
import sys
import time
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
import shutil
from stat import S_IMODE
from typing import Any
from types import MappingProxyType
from urllib.parse import quote, unquote

from jsonschema import FormatChecker, ValidationError, validate

from src import __version__
from src.models.generated_validation import (
    GeneratedArtifactVerificationResult,
    OperationVerificationResult,
    RepairAttemptResult,
    VerificationStatus,
)

from .generated_artifact_repair import GeneratedArtifactRepairer, NoOpRepairer
from .openapi_mcp_codegen import OperationMetadata, collect_operations
from .openapi_mock_data import MockOperationScenario, build_mock_scenarios
from .openapi_mock_server import OpenAPIMockServer, RecordedRequest


_SECRET_ENV_NAME_PATTERN = re.compile(r"(token|secret|password|api[_-]?key|auth)", re.IGNORECASE)


@dataclass(frozen=True)
class _ClientInvocation:
    returncode: int
    payload: Any


@dataclass(frozen=True)
class _RequestValidation:
    valid: bool
    failure_code: str | None = None
    message: str | None = None
    redacted_input: Any | None = None


@dataclass(frozen=True)
class _ResponseValidation:
    valid: bool
    status: VerificationStatus
    failure_code: str | None = None
    message: str | None = None
    error_kind: str | None = None
    redacted_output: Any | None = None


async def verify_generated_artifacts(
    openapi_spec: Mapping[str, Any],
    artifact_dir: Path,
    *,
    seed: int = 0,
    timeout_seconds: float = 30.0,
) -> GeneratedArtifactVerificationResult:
    return await asyncio.to_thread(
        _verify_generated_artifacts_sync,
        openapi_spec,
        artifact_dir,
        seed,
        timeout_seconds,
    )


async def verify_with_repair(
    openapi_spec: Mapping[str, Any],
    artifact_dir: Path,
    *,
    repairer: GeneratedArtifactRepairer | None = None,
    seed: int = 0,
    timeout_seconds: float = 30.0,
    max_repair_attempts: int = 2,
) -> GeneratedArtifactVerificationResult:
    verification_root = artifact_dir / ".verification"
    _remove_path(verification_root)

    try:
        baseline_result = await verify_generated_artifacts(
            openapi_spec,
            artifact_dir,
            seed=seed,
            timeout_seconds=timeout_seconds,
        )
    except Exception as exc:  # pragma: no cover - exercised by repair tests
        baseline_result = _verification_failure_result(
            artifact_dir,
            openapi_spec,
            seed=seed,
            failure_code=_verification_exception_code(exc),
        )
    if baseline_result.status != VerificationStatus.FAILED:
        return baseline_result

    attempt_limit = _clamp_repair_attempts(max_repair_attempts)
    if attempt_limit == 0:
        return baseline_result

    active_repairer = repairer or NoOpRepairer()
    _reset_verification_root(verification_root)
    pristine_state = _snapshot_tree(artifact_dir, excluded_dirs={".verification"})

    repair_attempts: list[RepairAttemptResult] = []
    accepted_result: GeneratedArtifactVerificationResult | None = None

    try:
        for attempt in range(1, attempt_limit + 1):
            _reset_verification_root(verification_root)
            candidate_dir = verification_root / f"attempt-{attempt}"
            _restore_artifact_tree(artifact_dir, pristine_state)
            _restore_artifact_tree(candidate_dir, pristine_state)
            repair_spec = _immutable_repair_spec(openapi_spec)

            preliminary_attempt = await active_repairer.repair(
                repair_spec,
                baseline_result,
                attempt,
                candidate_dir,
            )
            current_snapshot = _snapshot_tree(artifact_dir, excluded_dirs={".verification"})
            if current_snapshot != pristine_state:
                _restore_artifact_tree(artifact_dir, pristine_state)
                repaired_attempt = preliminary_attempt.model_copy(
                    update={
                        "accepted": False,
                        "failure_codes": _unique(
                            [
                                *preliminary_attempt.failure_codes,
                                "original_artifact_mutated",
                            ]
                        ),
                    }
                )
                repair_attempts.append(repaired_attempt)
                continue

            validation_errors = _validate_candidate_tree(
                verification_root,
                candidate_dir,
                pristine_state,
            )
            if validation_errors:
                repaired_attempt = preliminary_attempt.model_copy(
                    update={
                        "accepted": False,
                        "failure_codes": _unique(
                            [*preliminary_attempt.failure_codes, *validation_errors]
                        ),
                    }
                )
                repair_attempts.append(repaired_attempt)
                _restore_artifact_tree(artifact_dir, pristine_state)
                continue

            try:
                candidate_result = await verify_generated_artifacts(
                    openapi_spec,
                    candidate_dir,
                    seed=seed,
                    timeout_seconds=timeout_seconds,
                )
            except Exception as exc:  # pragma: no cover - exercised by repair tests
                candidate_result = _verification_failure_result(
                    candidate_dir,
                    openapi_spec,
                    seed=seed,
                    failure_code=_verification_exception_code(exc),
                )
            accepted = _verification_result_is_acceptable(candidate_result)
            repaired_attempt = preliminary_attempt.model_copy(
                update={
                    "accepted": accepted,
                    "failure_codes": [] if accepted else candidate_result.failures,
                }
            )
            repair_attempts.append(repaired_attempt)

            if accepted:
                _restore_artifact_tree(artifact_dir, pristine_state)
                _copy_selected_files(candidate_dir, artifact_dir)
                accepted_result = candidate_result.model_copy(
                    update={
                        "artifact_dir": str(artifact_dir.resolve()),
                        "attempts": baseline_result.attempts + len(repair_attempts),
                        "repair_attempts": repair_attempts,
                    }
                )
                break
            _restore_artifact_tree(artifact_dir, pristine_state)
    finally:
        if accepted_result is None:
            _restore_artifact_tree(artifact_dir, pristine_state)
        _remove_path(verification_root)

    if accepted_result is not None:
        return accepted_result
    return baseline_result.model_copy(
        update={
            "attempts": baseline_result.attempts + len(repair_attempts),
            "repair_attempts": repair_attempts,
        }
    )


def _immutable_repair_spec(openapi_spec: Mapping[str, Any]) -> Mapping[str, Any]:
    frozen_spec = _freeze_json_like(copy.deepcopy(dict(openapi_spec)))
    if not isinstance(frozen_spec, Mapping):
        raise TypeError("immutable repair spec must be a mapping")
    return frozen_spec


def _freeze_json_like(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {key: _freeze_json_like(item) for key, item in value.items()}
        )
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return tuple(_freeze_json_like(item) for item in value)
    return value


def _verify_generated_artifacts_sync(
    openapi_spec: Mapping[str, Any],
    artifact_dir: Path,
    seed: int,
    timeout_seconds: float,
) -> GeneratedArtifactVerificationResult:
    operations = collect_operations(openapi_spec)
    scenarios = build_mock_scenarios(openapi_spec, seed=seed)
    scenario_by_operation: dict[str, list[MockOperationScenario]] = defaultdict(list)
    for scenario in scenarios:
        scenario_by_operation[scenario.operation_id].append(scenario)

    secret_values = _secret_env_values()
    mock_server = OpenAPIMockServer(scenarios, secret_values=secret_values)
    server_process: subprocess.Popen[str] | None = None

    try:
        base_url = mock_server.start()
        mcp_port = _free_port()
        base_env = _base_env()
        server_url = f"http://127.0.0.1:{mcp_port}/mcp/"
        server_process = _start_process(
            [
                sys.executable,
                "server.py",
                "--port",
                str(mcp_port),
                "--transport",
                "streamable-http",
                "--base-url",
                base_url,
                "--timeout",
                str(timeout_seconds),
            ],
            artifact_dir,
            {
                **base_env,
                "API_BASE_URL": base_url,
                "MCP_SERVER_LISTEN_PORT": str(mcp_port),
            },
        )
        _wait_for_generated_server(artifact_dir, server_process, server_url, base_env)

        operation_results = [
            result
            for operation in operations
            for scenario in scenario_by_operation.get(operation.operation_id, [])
            for result in [
                _verify_operation(
                    operation,
                    scenario,
                    openapi_spec,
                    mock_server,
                    artifact_dir,
                    server_url,
                    base_env,
                    timeout_seconds,
                    secret_values,
                )
            ]
        ]
    finally:
        if server_process is not None:
            _stop_process(server_process)
        mock_server.stop()

    failures = [
        failure_code
        for failure_code in (
            result.failure_code for result in operation_results if result.failure_code
        )
        if failure_code is not None
    ]
    return GeneratedArtifactVerificationResult(
        artifact_dir=str(artifact_dir.resolve()),
        spec_sha256=_spec_sha256(openapi_spec),
        generator_version=__version__,
        seed=seed,
        status=_overall_status(operation_results),
        attempts=1,
        operations=operation_results,
        failures=_unique(failures),
    )


def _verify_operation(
    operation: OperationMetadata,
    scenario: MockOperationScenario,
    openapi_spec: Mapping[str, Any],
    mock_server: OpenAPIMockServer,
    artifact_dir: Path,
    server_url: str,
    env: dict[str, str],
    timeout_seconds: float,
    secret_values: Sequence[str],
) -> OperationVerificationResult:
    mock_server.activate_scenario(scenario)
    request_offset = len(mock_server.raw_requests())
    invocation = _invoke_generated_client(
        artifact_dir,
        server_url,
        scenario.tool_name,
        scenario.tool_arguments,
        env,
        timeout_seconds,
    )
    raw_requests = mock_server.raw_requests()[request_offset:]
    recorded_requests = mock_server.requests()[request_offset:]

    request_validation = _validate_request(
        operation,
        scenario,
        raw_requests,
        recorded_requests,
    )
    response_validation = _validate_response(
        operation,
        scenario,
        openapi_spec,
        invocation,
        secret_values,
    )

    status = _merge_status(request_validation, response_validation)
    failure_code = request_validation.failure_code or response_validation.failure_code
    message = request_validation.message or response_validation.message

    return OperationVerificationResult(
        operation_id=operation.operation_id,
        tool_name=operation.tool_name,
        scenario_kind=scenario.scenario_kind,
        status=status,
        request_valid=request_validation.valid,
        response_valid=response_validation.valid,
        error_kind=response_validation.error_kind,
        failure_code=failure_code,
        message=message,
        redacted_input=request_validation.redacted_input,
        redacted_output=response_validation.redacted_output,
    )


def _validate_request(
    operation: OperationMetadata,
    scenario: MockOperationScenario,
    raw_requests: Sequence[RecordedRequest],
    recorded_requests: Sequence[RecordedRequest],
) -> _RequestValidation:
    if not raw_requests:
        return _RequestValidation(
            valid=False,
            failure_code="missing_request",
            message="generated tool did not issue an upstream request",
        )
    if len(raw_requests) != 1:
        return _RequestValidation(
            valid=False,
            failure_code="unexpected_request",
            message="generated tool issued an unexpected number of upstream requests",
            redacted_input=[_recorded_request_summary(item) for item in recorded_requests],
        )

    recorded_request = raw_requests[0]
    redacted_request = recorded_requests[0]
    if recorded_request.method != operation.method:
        return _RequestValidation(
            valid=False,
            failure_code="unexpected_request",
            message="upstream request used an unexpected HTTP method",
            redacted_input=_recorded_request_summary(redacted_request),
        )

    expected_path = _expand_path(operation, scenario.tool_arguments)
    if recorded_request.path != expected_path:
        return _RequestValidation(
            valid=False,
            failure_code="unexpected_request",
            message="upstream request path did not match the generated tool arguments",
            redacted_input=_recorded_request_summary(redacted_request),
        )

    path_values = _extract_path_values(operation.path, recorded_request.path)
    if path_values is None:
        return _RequestValidation(
            valid=False,
            failure_code="request_schema_mismatch",
            message="request path parameters could not be decoded",
            redacted_input=_recorded_request_summary(redacted_request),
        )

    for parameter in operation.parameters:
        if parameter.location == "path":
            raw_value = path_values.get(parameter.wire_name)
            if raw_value is None:
                return _RequestValidation(
                    valid=False,
                    failure_code="request_schema_mismatch",
                    message=f"required path parameter {parameter.wire_name} was missing",
                    redacted_input=_recorded_request_summary(redacted_request),
                )
            actual_value = _coerce_schema_value(parameter.schema, raw_value)
            if actual_value != scenario.tool_arguments.get(parameter.python_name):
                return _RequestValidation(
                    valid=False,
                    failure_code="request_schema_mismatch",
                    message=f"path parameter {parameter.wire_name} did not match the scenario value",
                    redacted_input=_recorded_request_summary(redacted_request),
                )
            schema_error = _schema_validation_error(parameter.schema, actual_value)
            if schema_error is not None:
                return _RequestValidation(
                    valid=False,
                    failure_code="request_schema_mismatch",
                    message=f"path parameter {parameter.wire_name} failed schema validation: {schema_error}",
                    redacted_input=_recorded_request_summary(redacted_request),
                )

    expected_query_names = {
        parameter.wire_name for parameter in operation.parameters if parameter.location == "query"
    }
    if set(recorded_request.query) != {
        name
        for name in expected_query_names
        if scenario.tool_arguments.get(_parameter_name(operation, name, "query")) is not None
    }:
        return _RequestValidation(
            valid=False,
            failure_code="request_schema_mismatch",
            message="query parameters did not match the generated tool arguments",
            redacted_input=_recorded_request_summary(redacted_request),
        )

    for parameter in operation.parameters:
        if parameter.location != "query":
            continue
        expected_value = scenario.tool_arguments.get(parameter.python_name)
        actual_values = recorded_request.query.get(parameter.wire_name)
        if expected_value is None:
            continue
        expected_query_values = _serialize_query_values(expected_value)
        if actual_values != expected_query_values:
            return _RequestValidation(
                valid=False,
                failure_code="request_schema_mismatch",
                message=f"query parameter {parameter.wire_name} did not match the scenario value",
                redacted_input=_recorded_request_summary(redacted_request),
            )
        actual_value = _coerce_query_value(parameter.schema, actual_values)
        schema_error = _schema_validation_error(parameter.schema, actual_value)
        if schema_error is not None:
            return _RequestValidation(
                valid=False,
                failure_code="request_schema_mismatch",
                message=f"query parameter {parameter.wire_name} failed schema validation: {schema_error}",
                redacted_input=_recorded_request_summary(redacted_request),
            )

    if operation.request_body is None:
        if recorded_request.body is not None:
            return _RequestValidation(
                valid=False,
                failure_code="request_schema_mismatch",
                message="request body was sent for an operation without a request body",
                redacted_input=_recorded_request_summary(redacted_request),
            )
        return _RequestValidation(valid=True, redacted_input=_recorded_request_summary(redacted_request))

    expected_body_name = _body_argument_name(operation)
    expected_body = scenario.tool_arguments.get(expected_body_name)
    if expected_body != recorded_request.body:
        return _RequestValidation(
            valid=False,
            failure_code="request_schema_mismatch",
            message="request body did not match the generated scenario payload",
            redacted_input=_recorded_request_summary(redacted_request),
        )
    if operation.request_body.schema is None:
        return _RequestValidation(
            valid=False,
            failure_code="unvalidated_request_schema",
            message="request schema is not documented",
            redacted_input=_recorded_request_summary(redacted_request),
        )

    schema_error = _schema_validation_error(operation.request_body.schema, recorded_request.body)
    if schema_error is not None:
        return _RequestValidation(
            valid=False,
            failure_code="request_schema_mismatch",
            message=f"request body failed schema validation: {schema_error}",
            redacted_input=_recorded_request_summary(redacted_request),
        )
    return _RequestValidation(valid=True, redacted_input=_recorded_request_summary(redacted_request))


def _validate_response(
    operation: OperationMetadata,
    scenario: MockOperationScenario,
    openapi_spec: Mapping[str, Any],
    invocation: _ClientInvocation,
    secret_values: Sequence[str],
) -> _ResponseValidation:
    payload = invocation.payload
    structured_error = _structured_error_payload(payload)
    if scenario.scenario_kind == "http_error":
        return _validate_http_error_response(
            scenario,
            payload,
            structured_error,
            secret_values,
        )

    if scenario.status_code == 204:
        if structured_error is not None:
            return _ResponseValidation(
                valid=False,
                status=VerificationStatus.FAILED,
                failure_code="response_schema_mismatch",
                message="generated MCP client returned an error for an empty response scenario",
                error_kind=str(structured_error.get("kind") or "unknown"),
                redacted_output=_redact_payload(structured_error, secret_values),
            )
        if payload is None:
            return _ResponseValidation(
                valid=True,
                status=VerificationStatus.PASSED,
                redacted_output=_redact_payload(payload, secret_values),
            )
        return _ResponseValidation(
            valid=False,
            status=VerificationStatus.FAILED,
            failure_code="response_schema_mismatch",
            message="204 response returned an unexpected payload",
            redacted_output=_redact_payload(payload, secret_values),
        )

    response_schema = _success_response_schema(openapi_spec, operation, scenario.status_code)
    if response_schema is None or scenario.validation_status != "ready":
        return _ResponseValidation(
            valid=False,
            status=VerificationStatus.UNVALIDATED,
            failure_code="unvalidated_response_schema",
            message=scenario.validation_reason or "response schema is not documented",
            redacted_output=_redact_payload(payload, secret_values),
        )

    if structured_error is not None:
        return _ResponseValidation(
            valid=False,
            status=VerificationStatus.FAILED,
            failure_code="response_schema_mismatch",
            message="generated MCP client returned an error for a success scenario",
            error_kind=str(structured_error.get("kind") or "unknown"),
            redacted_output=_redact_payload(structured_error, secret_values),
        )

    if payload != scenario.response_body:
        return _ResponseValidation(
            valid=False,
            status=VerificationStatus.FAILED,
            failure_code="response_schema_mismatch",
            message="response payload did not match the activated mock scenario",
            redacted_output=_redact_payload(payload, secret_values),
        )

    schema_error = _schema_validation_error(response_schema, payload)
    if schema_error is not None:
        return _ResponseValidation(
            valid=False,
            status=VerificationStatus.FAILED,
            failure_code="response_schema_mismatch",
            message=f"response payload failed schema validation: {schema_error}",
            redacted_output=_redact_payload(payload, secret_values),
        )
    return _ResponseValidation(
        valid=True,
        status=VerificationStatus.PASSED,
        redacted_output=_redact_payload(payload, secret_values),
    )


def _validate_http_error_response(
    scenario: MockOperationScenario,
    payload: Any,
    structured_error: dict[str, Any] | None,
    secret_values: Sequence[str],
) -> _ResponseValidation:
    redacted_output = _redact_payload(
        structured_error if structured_error is not None else payload,
        secret_values,
    )
    if scenario.validation_status != "ready" or scenario.response_body is not None:
        message = scenario.validation_reason or "response schema is not documented"
        if scenario.response_body is not None and scenario.validation_status == "ready":
            message = "documented HTTP error response body cannot be validated"
        return _ResponseValidation(
            valid=False,
            status=VerificationStatus.UNVALIDATED,
            failure_code="unvalidated_response_schema",
            message=message,
            error_kind=str(structured_error.get("kind") or "unknown")
            if structured_error is not None
            else None,
            redacted_output=redacted_output,
        )

    if structured_error is None:
        return _ResponseValidation(
            valid=False,
            status=VerificationStatus.FAILED,
            failure_code="missing_error_mapping",
            message="generated MCP client did not return a structured error for an expected HTTP failure",
            redacted_output=redacted_output,
        )

    error_kind = str(structured_error.get("kind") or "unknown")
    if _payload_contains_secret(structured_error, secret_values):
        return _ResponseValidation(
            valid=False,
            status=VerificationStatus.FAILED,
            failure_code="credential_leak",
            message="structured error leaked credential material",
            error_kind=error_kind,
            redacted_output=redacted_output,
        )

    if error_kind != "http":
        return _ResponseValidation(
            valid=False,
            status=VerificationStatus.FAILED,
            failure_code="unexpected_error_kind",
            message="generated MCP client returned an unexpected error kind for a documented HTTP error",
            error_kind=error_kind,
            redacted_output=redacted_output,
        )

    actual_status = structured_error.get("status_code")
    if actual_status != scenario.status_code:
        return _ResponseValidation(
            valid=False,
            status=VerificationStatus.FAILED,
            failure_code="unexpected_error_status",
            message="generated MCP client returned the wrong HTTP status code",
            error_kind=error_kind,
            redacted_output=redacted_output,
        )

    return _ResponseValidation(
        valid=True,
        status=VerificationStatus.PASSED,
        error_kind=error_kind,
        redacted_output=redacted_output,
    )


def _merge_status(
    request_validation: _RequestValidation,
    response_validation: _ResponseValidation,
) -> VerificationStatus:
    failure_codes = {
        request_validation.failure_code,
        response_validation.failure_code,
    }
    if any(
        code in {"missing_request", "unexpected_request", "request_schema_mismatch", "response_schema_mismatch"}
        for code in failure_codes
        if code is not None
    ):
        return VerificationStatus.FAILED
    if request_validation.failure_code == "unvalidated_request_schema":
        return VerificationStatus.UNVALIDATED
    return response_validation.status


def _invoke_generated_client(
    artifact_dir: Path,
    server_url: str,
    tool_name: str,
    arguments: Mapping[str, Any],
    env: Mapping[str, str],
    timeout_seconds: float,
) -> _ClientInvocation:
    completed = subprocess.run(
        [
            sys.executable,
            "client.py",
            "--transport",
            "streamable-http",
            "--server-url",
            server_url,
            "--tool",
            tool_name,
            "--arguments",
            json.dumps(arguments),
        ],
        cwd=artifact_dir,
        env=dict(env),
        capture_output=True,
        text=True,
        timeout=max(timeout_seconds, 5.0),
        check=False,
    )
    try:
        payload = json.loads(completed.stdout) if completed.stdout.strip() else None
    except json.JSONDecodeError:
        payload = {
            "error": {
                "kind": "connection",
                "message": "generated MCP client returned invalid JSON",
            }
        }
    return _ClientInvocation(returncode=completed.returncode, payload=payload)


def _wait_for_generated_server(
    artifact_dir: Path,
    process: subprocess.Popen[str],
    server_url: str,
    env: Mapping[str, str],
) -> None:
    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("generated MCP server exited before becoming ready")
        completed = subprocess.run(
            [
                sys.executable,
                "client.py",
                "--transport",
                "streamable-http",
                "--server-url",
                server_url,
            ],
            cwd=artifact_dir,
            env=dict(env),
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if completed.returncode == 0:
            try:
                if isinstance(json.loads(completed.stdout), list):
                    return
            except json.JSONDecodeError:
                pass
        time.sleep(0.05)
    raise TimeoutError("timed out waiting for generated MCP server readiness")


def _start_process(
    command: list[str],
    cwd: Path,
    env: Mapping[str, str],
) -> subprocess.Popen[str]:
    return subprocess.Popen(
        command,
        cwd=cwd,
        env=dict(env),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


def _stop_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
    if process.stdout is not None:
        process.stdout.read()


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _base_env() -> dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", ""),
        "NO_PROXY": "127.0.0.1,localhost,::1",
        "PYTHONUNBUFFERED": "1",
    }


def _secret_env_values() -> list[str]:
    return _unique(
        [
            value
            for name, value in os.environ.items()
            if value and _SECRET_ENV_NAME_PATTERN.search(name)
        ]
    )


def _expand_path(operation: OperationMetadata, tool_arguments: Mapping[str, Any]) -> str:
    expanded_path = operation.path
    for parameter in operation.parameters:
        if parameter.location != "path":
            continue
        value = tool_arguments.get(parameter.python_name)
        expanded_path = expanded_path.replace(
            "{" + parameter.wire_name + "}",
            quote(str(value), safe=""),
        )
    return _normalize_path(expanded_path)


def _extract_path_values(template: str, actual_path: str) -> dict[str, str] | None:
    normalized_template = _normalize_path(template)
    names = re.findall(r"\{([^{}]+)\}", normalized_template)
    pattern_text = re.sub(
        r"\{[^/{}]+\}",
        r"([^/]+)",
        re.escape(normalized_template).replace(r"\{", "{").replace(r"\}", "}"),
    )
    match = re.fullmatch(pattern_text, _normalize_path(actual_path))
    if match is None:
        return None
    return {
        name: unquote(value)
        for name, value in zip(names, match.groups(), strict=False)
    }


def _serialize_query_values(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value]
    return [str(value)]


def _coerce_query_value(schema: Mapping[str, Any], values: Sequence[str]) -> Any:
    schema_type = schema.get("type")
    if schema_type == "array":
        item_schema = schema.get("items") if isinstance(schema.get("items"), Mapping) else {}
        return [_coerce_schema_value(item_schema, value) for value in values]
    return _coerce_schema_value(schema, values[0])


def _coerce_schema_value(schema: Mapping[str, Any], value: Any) -> Any:
    schema_type = schema.get("type")
    if schema_type == "integer" and isinstance(value, str):
        return int(value)
    if schema_type == "number" and isinstance(value, str):
        return float(value)
    if schema_type == "boolean" and isinstance(value, str):
        if value.lower() == "true":
            return True
        if value.lower() == "false":
            return False
    return value


def _success_response_schema(
    openapi_spec: Mapping[str, Any],
    operation: OperationMetadata,
    status_code: int,
) -> Mapping[str, Any] | None:
    raw_operation = _raw_operation(openapi_spec, operation)
    responses = raw_operation.get("responses")
    if not isinstance(responses, Mapping):
        return None
    response = responses.get(str(status_code))
    if not isinstance(response, Mapping):
        return None
    content = response.get("content")
    if not isinstance(content, Mapping) or not content:
        return None
    content_type = "application/json" if "application/json" in content else next(iter(content), None)
    if not isinstance(content_type, str):
        return None
    media_type = content.get(content_type)
    if not isinstance(media_type, Mapping):
        return None
    schema = media_type.get("schema")
    resolved = _resolve_value(schema, openapi_spec)
    return resolved if isinstance(resolved, Mapping) else None


def _raw_operation(
    openapi_spec: Mapping[str, Any],
    operation: OperationMetadata,
) -> Mapping[str, Any]:
    paths = openapi_spec.get("paths")
    if not isinstance(paths, Mapping):
        return {}
    path_item = paths.get(operation.path)
    if not isinstance(path_item, Mapping):
        return {}
    raw_operation = path_item.get(operation.method.lower())
    return raw_operation if isinstance(raw_operation, Mapping) else {}


def _resolve_value(value: Any, openapi_spec: Mapping[str, Any]) -> Any:
    return _resolve_value_with_stack(value, openapi_spec, set())


def _resolve_value_with_stack(
    value: Any,
    openapi_spec: Mapping[str, Any],
    resolving: set[str],
) -> Any:
    if isinstance(value, Mapping):
        reference = value.get("$ref")
        if isinstance(reference, str) and reference.startswith("#/"):
            if reference in resolving:
                return {}
            target: Any = openapi_spec
            try:
                for part in reference[2:].split("/"):
                    target = target[part.replace("~1", "/").replace("~0", "~")]
            except (KeyError, TypeError):
                return dict(value)
            resolved = _resolve_value_with_stack(target, openapi_spec, resolving | {reference})
            if isinstance(resolved, Mapping):
                merged = dict(resolved)
                merged.update(
                    {
                        key: _resolve_value_with_stack(item, openapi_spec, resolving)
                        for key, item in value.items()
                        if key != "$ref"
                    }
                )
                return merged
            return resolved
        return {
            str(key): _resolve_value_with_stack(item, openapi_spec, resolving)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_resolve_value_with_stack(item, openapi_spec, resolving) for item in value]
    return value


def _schema_validation_error(schema: Mapping[str, Any], value: Any) -> str | None:
    try:
        validate(
            value,
            _normalize_schema(schema),
            format_checker=FormatChecker(),
        )
    except ValidationError as exc:
        return exc.message
    return None


def _normalize_schema(schema: Any) -> Any:
    if isinstance(schema, Mapping):
        normalized = {
            str(key): _normalize_schema(value)
            for key, value in schema.items()
            if key not in {"nullable"}
        }
        if schema.get("nullable") is True:
            schema_type = normalized.get("type")
            if isinstance(schema_type, str):
                normalized["type"] = [schema_type, "null"]
            else:
                normalized = {"anyOf": [normalized, {"type": "null"}]}
        return normalized
    if isinstance(schema, list):
        return [_normalize_schema(item) for item in schema]
    return schema


def _structured_error_payload(payload: Any) -> dict[str, Any] | None:
    if not isinstance(payload, Mapping):
        return None
    error = payload.get("error")
    if isinstance(error, str):
        return {"kind": "mcp"}
    if not isinstance(error, Mapping):
        return None
    result = {"kind": error.get("kind")}
    for key in ("status_code", "operation_id", "target_code"):
        if key in error:
            result[key] = error[key]
    return result


def _payload_contains_secret(payload: Any, secret_values: Sequence[str]) -> bool:
    if not secret_values:
        return False
    if isinstance(payload, str):
        return any(secret in payload for secret in secret_values)
    if isinstance(payload, Mapping):
        return any(
            _payload_contains_secret(value, secret_values)
            for value in payload.values()
        )
    if isinstance(payload, Sequence) and not isinstance(payload, (str, bytes)):
        return any(_payload_contains_secret(value, secret_values) for value in payload)
    return False


def _redact_payload(payload: Any, secret_values: Sequence[str]) -> Any:
    if not secret_values:
        return payload
    if isinstance(payload, str):
        redacted = payload
        for secret in secret_values:
            if secret:
                redacted = redacted.replace(secret, "[REDACTED]")
        return redacted
    if isinstance(payload, Mapping):
        return {
            str(key): _redact_payload(value, secret_values)
            for key, value in payload.items()
        }
    if isinstance(payload, Sequence) and not isinstance(payload, (str, bytes)):
        return [_redact_payload(value, secret_values) for value in payload]
    return payload


def _recorded_request_summary(recorded_request: RecordedRequest) -> dict[str, Any]:
    return {
        "method": recorded_request.method,
        "path": recorded_request.path,
        "query": recorded_request.query,
        "body": recorded_request.body,
    }


def _body_argument_name(operation: OperationMetadata) -> str:
    used_names = {parameter.python_name for parameter in operation.parameters}
    candidate = "body"
    suffix = 2
    while candidate in used_names:
        candidate = f"body_{suffix}"
        suffix += 1
    return candidate


def _parameter_name(
    operation: OperationMetadata,
    wire_name: str,
    location: str,
) -> str | None:
    for parameter in operation.parameters:
        if parameter.location == location and parameter.wire_name == wire_name:
            return parameter.python_name
    return None


def _normalize_path(path: str) -> str:
    normalized = path.strip() or "/"
    if not normalized.startswith("/"):
        normalized = f"/{normalized}"
    if len(normalized) > 1:
        normalized = normalized.rstrip("/")
    return normalized or "/"


def _overall_status(
    operation_results: Sequence[OperationVerificationResult],
) -> VerificationStatus:
    statuses = {result.status for result in operation_results}
    if VerificationStatus.FAILED in statuses:
        return VerificationStatus.FAILED
    if VerificationStatus.UNVALIDATED in statuses:
        return VerificationStatus.UNVALIDATED
    return VerificationStatus.PASSED


def _spec_sha256(openapi_spec: Mapping[str, Any]) -> str:
    serialized = json.dumps(openapi_spec, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256(serialized.encode("utf-8")).hexdigest()


def _unique(values: Sequence[str]) -> list[str]:
    return [value for index, value in enumerate(values) if value not in values[:index]]


def _clamp_repair_attempts(max_repair_attempts: int) -> int:
    return max(0, min(2, int(max_repair_attempts)))


def _reset_verification_root(verification_root: Path) -> None:
    if verification_root.exists():
        shutil.rmtree(verification_root)
    verification_root.mkdir(parents=True, exist_ok=True)


def _copy_artifact_tree(source_dir: Path, destination_dir: Path) -> None:
    destination_dir.mkdir(parents=True, exist_ok=True)
    for entry in source_dir.iterdir():
        if entry.name == ".verification":
            continue
        target = destination_dir / entry.name
        if entry.is_symlink():
            target.symlink_to(os.readlink(entry))
        elif entry.is_dir():
            _copy_artifact_tree(entry, target)
        elif entry.is_file():
            shutil.copy2(entry, target)


def _snapshot_tree(
    root_dir: Path,
    *,
    excluded_dirs: set[str] | None = None,
) -> dict[str, _TreeEntry]:
    snapshot: dict[str, _TreeEntry] = {}

    def walk(current_dir: Path) -> None:
        for entry in current_dir.iterdir():
            if excluded_dirs and entry.name in excluded_dirs:
                continue
            relative_path = entry.relative_to(root_dir).as_posix()
            if entry.is_symlink():
                snapshot[relative_path] = _TreeEntry(
                    kind="symlink",
                    target=os.readlink(entry),
                )
                continue
            if entry.is_dir():
                snapshot[relative_path] = _TreeEntry(
                    kind="directory",
                    mode=S_IMODE(entry.lstat().st_mode),
                )
                walk(entry)
                continue
            snapshot[relative_path] = _TreeEntry(
                kind="file",
                mode=S_IMODE(entry.lstat().st_mode),
                content=entry.read_bytes(),
            )

    walk(root_dir)
    return snapshot


def _validate_candidate_tree(
    verification_root: Path,
    candidate_dir: Path,
    expected_state: Mapping[str, _TreeEntry],
) -> list[str]:
    errors: list[str] = []
    allowed_updates = {"server.py", "client.py", "runtime.py"}

    if not verification_root.exists():
        return ["candidate_verification_root_missing"]

    expected_verification_entries = {candidate_dir.name}
    actual_verification_entries = {
        entry.name for entry in verification_root.iterdir() if entry.name != ".gitkeep"
    }
    unexpected_entries = actual_verification_entries - expected_verification_entries
    if unexpected_entries:
        errors.append("candidate_path_traversal")

    symlink_errors = _candidate_symlink_errors(candidate_dir)
    if symlink_errors:
        errors.extend(symlink_errors)
        return _unique(errors)

    candidate_state = _snapshot_tree(candidate_dir)
    if set(candidate_state) != set(expected_state):
        errors.append("candidate_tree_state_mismatch")

    for relative_path, expected_entry in expected_state.items():
        actual_entry = candidate_state.get(relative_path)
        if actual_entry is None:
            errors.append(f"missing:{relative_path}")
            continue
        if actual_entry.kind != expected_entry.kind:
            errors.append(f"modified_kind:{relative_path}")
            continue
        if actual_entry.kind != "file":
            if actual_entry.mode != expected_entry.mode:
                errors.append(f"modified_mode:{relative_path}")
            if actual_entry != expected_entry:
                errors.append(f"modified:{relative_path}")
            continue
        if actual_entry.mode != expected_entry.mode:
            errors.append(f"modified_mode:{relative_path}")
        if relative_path in allowed_updates:
            continue
        if actual_entry.content != expected_entry.content:
            errors.append(f"modified:{relative_path}")

    errors.extend(_static_validate_candidate(candidate_dir))
    return _unique(errors)


def _candidate_symlink_errors(candidate_dir: Path) -> list[str]:
    errors: list[str] = []
    if candidate_dir.is_symlink():
        errors.append("candidate_symlink_detected:.")
        return errors

    for path in candidate_dir.rglob("*"):
        if path.is_symlink():
            relative_path = path.relative_to(candidate_dir).as_posix()
            errors.append(f"candidate_symlink_detected:{relative_path}")
    return errors


def _static_validate_candidate(candidate_dir: Path) -> list[str]:
    errors: list[str] = []
    for filename in ("server.py", "client.py", "runtime.py"):
        path = candidate_dir / filename
        if not path.exists():
            errors.append(f"missing_candidate_file:{filename}")
            continue
        try:
            source = path.read_text(encoding="utf-8")
            ast.parse(source, filename=filename)
            compile(source, filename, "exec")
        except (OSError, SyntaxError, ValueError) as exc:
            errors.append(f"static_validation_failed:{filename}:{type(exc).__name__}")
    return errors


def _verification_result_is_acceptable(
    result: GeneratedArtifactVerificationResult,
) -> bool:
    if result.status != VerificationStatus.PASSED:
        return False
    if any(operation.failure_code == "credential_leak" for operation in result.operations):
        return False
    return not any(failure == "credential_leak" for failure in result.failures)


def _verification_failure_result(
    artifact_dir: Path,
    openapi_spec: Mapping[str, Any],
    *,
    seed: int,
    failure_code: str,
) -> GeneratedArtifactVerificationResult:
    return GeneratedArtifactVerificationResult(
        artifact_dir=str(artifact_dir.resolve()),
        spec_sha256=_spec_sha256(openapi_spec),
        generator_version=__version__,
        seed=seed,
        status=VerificationStatus.FAILED,
        attempts=1,
        operations=[],
        repair_attempts=[],
        failures=[failure_code],
    )


def _verification_exception_code(exc: BaseException) -> str:
    return f"verification_error:{type(exc).__name__}"


def _restore_artifact_tree(artifact_dir: Path, backup_dir: Path) -> None:
    _restore_tree_from_snapshot(artifact_dir, backup_dir, preserve_names={".verification"})


def _restore_tree_from_snapshot(
    target_dir: Path,
    snapshot: Mapping[str, _TreeEntry],
    *,
    preserve_names: set[str] | None = None,
) -> None:
    target_dir.mkdir(parents=True, exist_ok=True)
    for entry in list(target_dir.iterdir()):
        if preserve_names and entry.name in preserve_names:
            continue
        _remove_path(entry)

    directory_entries = sorted(
        (
            (relative_path, entry)
            for relative_path, entry in snapshot.items()
            if entry.kind == "directory"
        ),
        key=lambda item: (item[0].count("/"), item[0]),
    )
    for relative_path, entry in directory_entries:
        path = target_dir / relative_path
        path.mkdir(parents=True, exist_ok=True)
        if entry.mode is not None:
            os.chmod(path, entry.mode)

    file_entries = sorted(
        (
            (relative_path, entry)
            for relative_path, entry in snapshot.items()
            if entry.kind == "file"
        ),
        key=lambda item: (item[0].count("/"), item[0]),
    )
    for relative_path, entry in file_entries:
        path = target_dir / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        _remove_path(path)
        path.write_bytes(entry.content or b"")
        if entry.mode is not None:
            os.chmod(path, entry.mode)

    symlink_entries = sorted(
        (
            (relative_path, entry)
            for relative_path, entry in snapshot.items()
            if entry.kind == "symlink"
        ),
        key=lambda item: (item[0].count("/"), item[0]),
    )
    for relative_path, entry in symlink_entries:
        path = target_dir / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        _remove_path(path)
        path.symlink_to(entry.target)


def _copy_selected_files(source_dir: Path, destination_dir: Path) -> None:
    for filename in ("server.py", "client.py", "runtime.py"):
        source = source_dir / filename
        if source.exists():
            shutil.copy2(source, destination_dir / filename)


def _remove_path(path: Path) -> None:
    if not path.exists():
        return
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
        return
    path.unlink()


@dataclass(frozen=True)
class _TreeEntry:
    kind: str
    mode: int | None = None
    content: bytes | None = None
    target: str | None = None