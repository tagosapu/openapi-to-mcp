"""HTTP runtime shared by deterministically generated MCP servers."""

from __future__ import annotations

import base64
import json
import os
import re
from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import quote

import httpx


class StructuredApiError(RuntimeError):
    """An API failure that can be returned to an MCP caller as structured data."""

    def __init__(
        self,
        kind: str,
        operation_id: str,
        message: str,
        status_code: int | None = None,
        target_code: str | None = None,
    ) -> None:
        self.kind = kind
        self.operation_id = operation_id
        self.message = message
        self.status_code = status_code
        self.target_code = target_code
        super().__init__(message)

    def as_dict(self) -> dict[str, Any]:
        error: dict[str, Any] = {
            "kind": self.kind,
            "operation_id": self.operation_id,
            "message": self.message,
        }
        if self.status_code is not None:
            error["status_code"] = self.status_code
        if self.target_code is not None:
            error["target_code"] = self.target_code
        return {"error": error}


def resolve_auth_headers(
    security: Sequence[Mapping[str, Sequence[str]]],
    schemes: Mapping[str, Mapping[str, Any]],
    environ: Mapping[str, str],
) -> dict[str, str]:
    """Resolve the first fully configured security alternative."""
    if not security:
        return {}

    for requirement in security:
        headers: dict[str, str] = {}
        complete = True
        for scheme_name in requirement:
            scheme = schemes.get(scheme_name)
            scheme_headers = _resolve_scheme_headers(scheme_name, scheme, environ)
            if scheme_headers is None:
                complete = False
                break
            headers.update(scheme_headers)
        if complete:
            return headers

    raise StructuredApiError(
        kind="configuration",
        operation_id="",
        message="no configured authentication alternative is available",
    )


def _resolve_scheme_headers(
    scheme_name: str,
    scheme: Mapping[str, Any] | None,
    environ: Mapping[str, str],
) -> dict[str, str] | None:
    if not isinstance(scheme, Mapping):
        return None

    scheme_type = str(scheme.get("type", ""))
    normalized_name = _normalize_scheme_name(scheme_name)
    if scheme_type == "apiKey":
        header_name = str(scheme.get("name", ""))
        if str(scheme.get("in", "")) != "header" or not header_name:
            return None
        if scheme_name == "apiToken" or header_name.lower() == "x-cybozu-api-token":
            env_name = "KINTONE_API_TOKEN"
        else:
            env_name = f"OPENAPI_API_KEY_{normalized_name}"
        value = environ.get(env_name)
        return {header_name: value} if value else None

    if scheme_type == "http":
        scheme_kind = str(scheme.get("scheme", "")).lower()
        if scheme_kind == "bearer":
            value = environ.get(f"OPENAPI_BEARER_TOKEN_{normalized_name}")
            return {"Authorization": f"Bearer {value}"} if value else None
        if scheme_kind == "basic":
            username = environ.get(f"OPENAPI_BASIC_USERNAME_{normalized_name}")
            password = environ.get(f"OPENAPI_BASIC_PASSWORD_{normalized_name}")
            if username is None or password is None:
                return None
            encoded = base64.b64encode(f"{username}:{password}".encode()).decode()
            return {"Authorization": f"Basic {encoded}"}
        return None

    if scheme_type == "oauth2":
        value = environ.get(f"OPENAPI_OAUTH_TOKEN_{normalized_name}")
        return {"Authorization": f"Bearer {value}"} if value else None

    return None


class GeneratedMcpRuntime:
    """Build and execute HTTP requests for generated MCP tools."""

    def __init__(
        self,
        base_url: str,
        timeout_seconds: float = 30.0,
        environ: Mapping[str, str] | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.environ = dict(environ or os.environ)
        self.transport = transport

    def request(
        self, operation: Mapping[str, Any], arguments: Mapping[str, Any]
    ) -> Any:
        operation_id = str(operation.get("operation_id", ""))
        try:
            url = self._build_url(operation, arguments)
            headers = self._build_headers(operation, arguments, operation_id)
            query = self._build_query(operation, arguments, operation_id)
            cookies = self._build_cookies(operation, arguments, operation_id)
            body = self._build_body(operation, arguments, operation_id)
        except StructuredApiError as exc:
            raise _for_operation(exc, operation_id) from exc

        method = str(operation.get("method", "GET")).upper()
        try:
            client_kwargs: dict[str, Any] = {"trust_env": False}
            if self.transport is not None:
                client_kwargs["transport"] = self.transport
            with httpx.Client(**client_kwargs) as client:
                response = client.request(
                    method,
                    url,
                    params=query,
                    headers=headers,
                    cookies=cookies,
                    json=body,
                    timeout=self.timeout_seconds,
                )
        except httpx.TimeoutException as exc:
            raise StructuredApiError(
                kind="timeout",
                operation_id=operation_id,
                message="request timed out",
            ) from exc
        except httpx.InvalidURL as exc:
            raise StructuredApiError(
                kind="configuration",
                operation_id=operation_id,
                message="target URL is invalid",
            ) from exc
        except httpx.RequestError as exc:
            raise StructuredApiError(
                kind="connection",
                operation_id=operation_id,
                message="could not connect to target API",
            ) from exc

        if response.status_code >= 400:
            raise self._http_error(response, operation_id)
        return self._decode_success(response, operation_id)

    def _build_url(
        self, operation: Mapping[str, Any], arguments: Mapping[str, Any]
    ) -> str:
        if not self.base_url:
            raise StructuredApiError(
                kind="configuration",
                operation_id=str(operation.get("operation_id", "")),
                message="API_BASE_URL or --base-url is required",
            )
        path = str(operation.get("path", ""))
        path_values = _mapping(arguments.get("path"))

        def replace_path(match: re.Match[str]) -> str:
            name = match.group(1)
            if name not in path_values or path_values[name] is None:
                raise StructuredApiError(
                    kind="configuration",
                    operation_id=str(operation.get("operation_id", "")),
                    message=f"required path parameter {name} is missing",
                )
            return quote(str(path_values[name]), safe="")

        expanded_path = re.sub(r"\{([^{}]+)\}", replace_path, path)
        return f"{self.base_url}/{expanded_path.lstrip('/')}"

    def _build_headers(
        self,
        operation: Mapping[str, Any],
        arguments: Mapping[str, Any],
        operation_id: str,
    ) -> dict[str, str]:
        try:
            auth_headers = resolve_auth_headers(
                operation.get("security", []),
                operation.get("security_schemes", {}),
                self.environ,
            )
        except StructuredApiError as exc:
            raise _for_operation(exc, operation_id) from exc
        headers = {
            str(name): str(value)
            for name, value in _mapping(arguments.get("headers")).items()
            if value is not None
        }
        headers.update(auth_headers)
        self._validate_required_parameter(operation, arguments, "header", operation_id)
        return headers

    def _build_query(
        self,
        operation: Mapping[str, Any],
        arguments: Mapping[str, Any],
        operation_id: str,
    ) -> dict[str, Any]:
        self._validate_required_parameter(operation, arguments, "query", operation_id)
        return {
            str(name): value
            for name, value in _mapping(arguments.get("query")).items()
            if value is not None
        }

    def _build_cookies(
        self,
        operation: Mapping[str, Any],
        arguments: Mapping[str, Any],
        operation_id: str,
    ) -> dict[str, Any]:
        self._validate_required_parameter(operation, arguments, "cookie", operation_id)
        return {
            str(name): value
            for name, value in _mapping(arguments.get("cookies")).items()
            if value is not None
        }

    def _build_body(
        self,
        operation: Mapping[str, Any],
        arguments: Mapping[str, Any],
        operation_id: str,
    ) -> Any:
        request_body = operation.get("request_body")
        body = arguments.get("body")
        if isinstance(request_body, Mapping) and request_body.get("required") and body is None:
            raise StructuredApiError(
                kind="configuration",
                operation_id=operation_id,
                message="required request body is missing",
            )
        return body

    def _validate_required_parameter(
        self,
        operation: Mapping[str, Any],
        arguments: Mapping[str, Any],
        location: str,
        operation_id: str,
    ) -> None:
        group_name = {
            "path": "path",
            "query": "query",
            "header": "headers",
            "cookie": "cookies",
        }[location]
        values = _mapping(arguments.get(group_name))
        for parameter in operation.get("parameters", []):
            if not isinstance(parameter, Mapping):
                continue
            parameter_location = parameter.get("location", parameter.get("in"))
            if parameter_location == location and parameter.get("required"):
                wire_name = str(parameter.get("wire_name", parameter.get("name", "")))
                if wire_name not in values or values[wire_name] is None:
                    raise StructuredApiError(
                        kind="configuration",
                        operation_id=operation_id,
                        message=f"required {location} parameter {wire_name} is missing",
                    )

    def _http_error(
        self, response: httpx.Response, operation_id: str
    ) -> StructuredApiError:
        target_code = None
        message = f"target API returned HTTP {response.status_code}"
        try:
            payload = response.json()
        except (json.JSONDecodeError, ValueError):
            payload = None
        if isinstance(payload, Mapping):
            if payload.get("code") is not None:
                target_code = str(payload["code"])
            if payload.get("message") is not None:
                message = _redact(str(payload["message"]), self.environ)
        return StructuredApiError(
            kind="http",
            operation_id=operation_id,
            status_code=response.status_code,
            target_code=target_code,
            message=message,
        )

    def _decode_success(self, response: httpx.Response, operation_id: str) -> Any:
        if not response.content:
            return None
        content_type = response.headers.get("content-type", "").lower()
        if "json" in content_type:
            try:
                return response.json()
            except (json.JSONDecodeError, ValueError) as exc:
                raise StructuredApiError(
                    kind="invalid_json",
                    operation_id=operation_id,
                    message="target returned invalid JSON",
                ) from exc
        return _redact(response.text, self.environ)


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _normalize_scheme_name(value: str) -> str:
    return re.sub(r"[^A-Z0-9]", "_", value.upper())


def _redact(value: str, environ: Mapping[str, str]) -> str:
    redacted = value
    for name, secret in environ.items():
        if not secret or len(secret) < 3:
            continue
        if any(marker in name.upper() for marker in ("TOKEN", "PASSWORD", "SECRET", "API_KEY", "AUTH")):
            redacted = redacted.replace(secret, "[REDACTED]")
    return redacted


def _for_operation(error: StructuredApiError, operation_id: str) -> StructuredApiError:
    return StructuredApiError(
        kind=error.kind,
        operation_id=operation_id,
        message=error.message,
        status_code=error.status_code,
        target_code=error.target_code,
    )