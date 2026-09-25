"""Local OpenAPI-backed mock server with redacted request recording."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
import json
import os
import re
import socket
import threading
import time
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
import uvicorn

from .openapi_mock_data import MockOperationScenario

_REDACTED = "[REDACTED]"
_SENSITIVE_HEADER_NAMES = {
    "authorization",
    "proxy-authorization",
    "x-api-key",
    "x-cybozu-api-token",
}
_SENSITIVE_KEY_TOKENS = (
    "authorization",
    "password",
    "api_key",
    "secret",
    "token",
)
_STARTUP_TIMEOUT_SECONDS = 5.0
_SHUTDOWN_TIMEOUT_SECONDS = 5.0
_ALL_HTTP_METHODS = [
    "DELETE",
    "GET",
    "HEAD",
    "OPTIONS",
    "PATCH",
    "POST",
    "PUT",
]


@dataclass(frozen=True)
class RecordedRequest:
    method: str
    path: str
    query: dict[str, list[str]]
    headers: dict[str, str]
    body: Any | None


@dataclass(frozen=True)
class _CompiledScenario:
    scenario: MockOperationScenario
    method: str
    route_key: tuple[str, str]
    path_pattern: re.Pattern[str]


class OpenAPIMockServer:
    def __init__(
        self,
        scenarios: Sequence[MockOperationScenario],
        *,
        host: str = "127.0.0.1",
        port: int = 0,
        secret_values: Sequence[str] = (),
    ) -> None:
        self._host = host
        self._port = port
        self._secret_values = {value for value in secret_values if value}
        self._compiled_scenarios = [_compile_scenario(scenario) for scenario in scenarios]
        self._compiled_scenario_lookup = {
            id(compiled.scenario): compiled for compiled in self._compiled_scenarios
        }
        self._raw_requests: list[RecordedRequest] = []
        self._active_scenarios: dict[tuple[str, str], _CompiledScenario] = {}
        self._recorded_requests: list[RecordedRequest] = []
        self._requests_lock = threading.Lock()
        self._scenario_lock = threading.Lock()
        self._startup_error: BaseException | None = None
        self._base_url: str | None = None
        self._server: uvicorn.Server | None = None
        self._server_thread: threading.Thread | None = None
        self._server_socket: socket.socket | None = None
        self._proxy_env_updates: dict[str, str | None] = {}
        self._app = self._build_app()

    def start(self) -> str:
        if self._base_url is not None and self._server_thread is not None:
            return self._base_url

        self._install_local_no_proxy()

        server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server_socket.bind((self._host, self._port))
        server_socket.listen(128)

        actual_host, actual_port = server_socket.getsockname()[:2]
        config = uvicorn.Config(
            self._app,
            host=self._host,
            port=self._port,
            log_level="warning",
            access_log=False,
            lifespan="off",
        )
        server = uvicorn.Server(config)
        self._startup_error = None

        def run_server() -> None:
            try:
                server.run(sockets=[server_socket])
            except BaseException as exc:  # pragma: no cover - startup failures are timing dependent
                self._startup_error = exc

        server_thread = threading.Thread(
            target=run_server,
            name="openapi-mock-server",
            daemon=True,
        )

        self._server = server
        self._server_thread = server_thread
        self._server_socket = server_socket
        self._base_url = f"http://{actual_host}:{actual_port}"
        server_thread.start()

        try:
            self._wait_for_startup()
        except Exception:
            self.stop()
            raise

        return self._base_url

    def stop(self) -> None:
        server = self._server
        thread = self._server_thread
        server_socket = self._server_socket

        if server is not None:
            server.should_exit = True

        if thread is not None and thread.is_alive():
            thread.join(timeout=_SHUTDOWN_TIMEOUT_SECONDS)
            if thread.is_alive() and server is not None:
                server.force_exit = True
                thread.join(timeout=_SHUTDOWN_TIMEOUT_SECONDS)

        if server_socket is not None:
            server_socket.close()

        self._restore_proxy_env()

        self._server = None
        self._server_thread = None
        self._server_socket = None
        self._base_url = None
        self._startup_error = None

    def requests(self) -> list[RecordedRequest]:
        with self._requests_lock:
            return list(self._recorded_requests)

    def raw_requests(self) -> list[RecordedRequest]:
        with self._requests_lock:
            return list(self._raw_requests)

    def activate_scenario(self, scenario: MockOperationScenario) -> None:
        compiled = self._compiled_scenario_lookup.get(id(scenario))
        if compiled is None:
            raise ValueError("Scenario was not supplied when this server was created.")
        with self._scenario_lock:
            self._active_scenarios[compiled.route_key] = compiled

    def _build_app(self) -> FastAPI:
        app = FastAPI()

        @app.api_route("/{path:path}", methods=_ALL_HTTP_METHODS)
        async def handle_request(path: str, request: Request) -> Response:
            del path
            recorded_request = await self._record_request(request)
            scenario = self._match_scenario(recorded_request.method, recorded_request.path)
            if scenario is None:
                return JSONResponse(
                    status_code=404,
                    content={"detail": "No mock scenario matched the request."},
                )
            if scenario.response_delay_seconds > 0:
                await asyncio.sleep(scenario.response_delay_seconds)
            return _build_response(scenario)

        return app

    async def _record_request(self, request: Request) -> RecordedRequest:
        body = await _read_request_body(request)
        raw_query = _read_query(request)
        raw_headers = dict(request.headers.items())
        raw_request = RecordedRequest(
            method=request.method.upper(),
            path=_normalize_path(request.url.path),
            query=raw_query,
            headers=raw_headers,
            body=body,
        )
        recorded_request = RecordedRequest(
            method=raw_request.method,
            path=raw_request.path,
            query=_redact_query(raw_query, self._secret_values),
            headers=_redact_headers(raw_headers, self._secret_values),
            body=_redact_body(body, self._secret_values),
        )
        with self._requests_lock:
            self._raw_requests.append(raw_request)
            self._recorded_requests.append(recorded_request)
        return recorded_request

    def _match_scenario(
        self,
        method: str,
        path: str,
    ) -> MockOperationScenario | None:
        for compiled in self._compiled_scenarios:
            if compiled.method != method:
                continue
            if compiled.path_pattern.fullmatch(path):
                with self._scenario_lock:
                    active = self._active_scenarios.get(compiled.route_key)
                if active is not None and active.path_pattern.fullmatch(path):
                    return active.scenario
                return compiled.scenario
        return None

    def _wait_for_startup(self) -> None:
        deadline = time.monotonic() + _STARTUP_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if self._startup_error is not None:
                raise RuntimeError("OpenAPI mock server failed to start.") from self._startup_error
            if self._server is not None and self._server.started:
                return
            if self._server_thread is not None and not self._server_thread.is_alive():
                raise RuntimeError("OpenAPI mock server stopped before startup completed.")
            time.sleep(0.01)
        raise TimeoutError("Timed out waiting for OpenAPI mock server startup.")

    def _install_local_no_proxy(self) -> None:
        if self._host not in {"127.0.0.1", "localhost", "::1"}:
            return

        for variable_name in ("NO_PROXY", "no_proxy"):
            current_value = os.environ.get(variable_name)
            entries = []
            if current_value:
                entries = [entry.strip() for entry in current_value.split(",") if entry.strip()]
            if self._host in entries:
                continue
            self._proxy_env_updates[variable_name] = current_value
            os.environ[variable_name] = ",".join([*entries, self._host])

    def _restore_proxy_env(self) -> None:
        for variable_name, previous_value in self._proxy_env_updates.items():
            if previous_value is None:
                os.environ.pop(variable_name, None)
            else:
                os.environ[variable_name] = previous_value
        self._proxy_env_updates.clear()


def _compile_scenario(scenario: MockOperationScenario) -> _CompiledScenario:
    normalized_path = _normalize_path(scenario.path)
    pattern_text = re.sub(
        r"\{[^/{}]+\}",
        r"[^/]+",
        re.escape(normalized_path).replace(r"\{", "{").replace(r"\}", "}"),
    )
    return _CompiledScenario(
        scenario=scenario,
        method=scenario.method.upper(),
        route_key=(scenario.method.upper(), normalized_path),
        path_pattern=re.compile(f"^{pattern_text}$"),
    )


async def _read_request_body(request: Request) -> Any | None:
    body_bytes = await request.body()
    if not body_bytes:
        return None

    content_type = request.headers.get("content-type", "")
    if "json" not in content_type.lower():
        return None

    try:
        return json.loads(body_bytes)
    except json.JSONDecodeError:
        return None


def _read_query(request: Request) -> dict[str, list[str]]:
    values: defaultdict[str, list[str]] = defaultdict(list)
    for key, value in request.query_params.multi_items():
        values[key].append(value)
    return dict(values)


def _build_response(scenario: MockOperationScenario) -> Response:
    headers = dict(scenario.response_headers)
    if scenario.response_body is None:
        return Response(status_code=scenario.status_code, headers=headers)

    if scenario.response_mode == "raw":
        if isinstance(scenario.response_body, bytes):
            return Response(
                content=scenario.response_body,
                status_code=scenario.status_code,
                headers=headers,
            )
        return Response(
            content=str(scenario.response_body),
            status_code=scenario.status_code,
            headers=headers,
        )

    content_type = headers.get("content-type", headers.get("Content-Type", ""))
    if "json" in content_type.lower() or isinstance(
        scenario.response_body,
        dict | list,
    ):
        return JSONResponse(
            content=scenario.response_body,
            status_code=scenario.status_code,
            headers=headers,
        )
    return Response(
        content=str(scenario.response_body),
        status_code=scenario.status_code,
        headers=headers,
    )


def _redact_query(
    query: dict[str, list[str]],
    secret_values: set[str],
) -> dict[str, list[str]]:
    return {
        key: [
            _REDACTED if _is_sensitive_query_key(key) or value in secret_values else value
            for value in values
        ]
        for key, values in query.items()
    }


def _redact_headers(headers: dict[str, str], secret_values: set[str]) -> dict[str, str]:
    return {
        name: _REDACTED
        if _is_sensitive_header_name(name) or value in secret_values
        else value
        for name, value in headers.items()
    }


def _redact_body(value: Any, secret_values: set[str]) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _REDACTED
            if _is_sensitive_body_key(str(key))
            else _redact_body(child, secret_values)
            for key, child in value.items()
        }
    if isinstance(value, list):
        return [_redact_body(item, secret_values) for item in value]
    if isinstance(value, str) and value in secret_values:
        return _REDACTED
    return value


def _is_sensitive_header_name(name: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    if normalized in _SENSITIVE_HEADER_NAMES:
        return True
    return any(token in normalized for token in _SENSITIVE_KEY_TOKENS)


def _is_sensitive_query_key(name: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    return any(token in normalized for token in _SENSITIVE_KEY_TOKENS)


def _is_sensitive_body_key(name: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    return any(token in normalized for token in _SENSITIVE_KEY_TOKENS)


def _normalize_path(path: str) -> str:
    normalized = path.strip() or "/"
    if not normalized.startswith("/"):
        normalized = f"/{normalized}"
    if len(normalized) > 1:
        normalized = normalized.rstrip("/")
    return normalized or "/"