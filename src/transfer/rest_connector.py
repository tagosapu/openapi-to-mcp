from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import ipaddress
import json
import re
import socket
import time
from typing import Any
from urllib.parse import quote

import httpx
from jsonpointer import JsonPointerException, resolve_pointer
from jsonschema import Draft202012Validator

from .auth import CredentialResolver, SecretBundle
from .connector import ErrorClassification, OutboundOutcome, OutboundRequest, ReconciliationContext, ReconciliationResult, ValidationResult
from .errors import MappingValidationError, NotFoundError
from .mapping import MappingEngine
from .models import ConnectorDefinition, MappingDefinition, MappingIssue, OperationBinding, OperationSelection, OutboundRequestParts, TransferRequest, TransferResult
from .openapi_contract import ContractPreflight, ContractPreflightResult, NormalizedOperation, ParameterDefinition
from .settings import TransferSettings as Settings


INTERNAL_ERROR_CODE_HEADER = "x-openapi-to-mcp-error-code"
PROTECTED_HEADERS = {"authorization", "content-length", "cookie", "host"}
OAUTH_REGISTRATION_HOSTS_EXTENSION = "x-openapi-to-mcp-registration-oauth-hosts"


@dataclass(slots=True)
class _OAuthToken:
    access_token: str
    expires_at: datetime


class RestOpenApiConnector:
    def __init__(
        self,
        definition: ConnectorDefinition,
        mapping_engine: MappingEngine,
        credential_resolver: CredentialResolver,
        http_client: httpx.AsyncClient,
        *,
        settings: Settings | None = None,
    ) -> None:
        self._definition = definition
        self._mapping_engine = mapping_engine
        self._credential_resolver = credential_resolver
        self._http_client = http_client
        self._settings = settings
        self._preflight = ContractPreflight().run(definition.spec)
        self._last_operation_id: str | None = None
        self._oauth_tokens: dict[tuple[str, str], _OAuthToken] = {}
        self._registration_addresses = definition.spec.get("x-openapi-to-mcp-registration-hosts")
        if not isinstance(self._registration_addresses, list):
            self._registration_addresses = resolve_host_addresses(definition.base_url.host)
        self._oauth_registration_addresses = _oauth_registration_addresses(definition.spec)

    async def validate_config(self) -> ContractPreflightResult:
        return self._preflight

    async def resolve_operation(self, operation_name: str) -> OperationSelection:
        binding = self._definition.operation_bindings.get(operation_name)
        if binding is None:
            raise ValueError("OPERATION_UNBOUND")
        operation = self._preflight.operations.get(binding.operation_id)
        if operation is None:
            raise ValueError("OPERATION_UNRESOLVED")
        return OperationSelection(
            name=operation_name,
            operation_id=operation.operation_id,
            method=operation.method,
            path=operation.path,
        )

    async def validate_payload(
        self,
        payload: TransferRequest,
        mapping: MappingDefinition,
        operation: OperationSelection,
    ) -> ValidationResult:
        try:
            preview = self._mapping_engine.preview(payload, mapping, operation)
        except MappingValidationError as exc:
            return ValidationResult(
                valid=False,
                issues=[
                    MappingIssue(
                        source_path=mapping.target_schema_ref,
                        target_path=operation.path,
                        code="MAPPING_INVALID",
                        message=str(exc),
                        retryable=False,
                    )
                ],
            )
        issues = list(preview.issues)
        body = preview.json_body
        for schema_ref in (mapping.target_schema_ref, f"openapi:#/operations/{operation.operation_id}/request_schema"):
            try:
                schema = self._preflight.resolve_target_schema(schema_ref, operation.operation_id)
            except ValueError:
                if schema_ref == mapping.target_schema_ref:
                    issues.append(
                        MappingIssue(
                            source_path=mapping.target_schema_ref,
                            target_path=operation.path,
                            code="TARGET_SCHEMA_UNRESOLVED",
                            message="target schema ref could not be resolved",
                            retryable=False,
                        )
                    )
                continue
            validator = Draft202012Validator(schema)
            for error in validator.iter_errors(body):
                issues.append(
                    MappingIssue(
                        source_path=mapping.target_schema_ref,
                        target_path="/" + "/".join(str(part) for part in error.path),
                        code="REQUEST_SCHEMA_INVALID",
                        message=error.message,
                        retryable=False,
                    )
                )
        return ValidationResult(valid=not issues, issues=issues)

    async def build_request(self, parts: OutboundRequestParts) -> OutboundRequest:
        binding = self._binding_by_operation_id(parts.operation_id)
        operation = self._operation_by_id(parts.operation_id)
        headers = {key: value for key, value in parts.headers.items() if key.lower() not in PROTECTED_HEADERS}
        if binding is not None and binding.idempotency_header:
            headers[binding.idempotency_header] = parts.idempotency_key
        for additional in self._definition.additional_headers:
            bundle = await self._credential_resolver.resolve(additional.value_ref)
            headers[additional.name] = _bundle_secret_value(bundle)
        auth_headers, auth_query = await self._build_auth(operation)
        headers.update(auth_headers)
        for key in operation.required_headers:
            if key not in headers:
                raise ValueError(f"REQUIRED_HEADER_MISSING:{key}")
        path = _render_path(operation.path, parts.path_params)
        url = _join_base_url_path(self._definition.base_url, path)
        query = dict(parts.query_params)
        query.update(auth_query)
        if query:
            url = url.copy_merge_params(query)
        self._last_operation_id = parts.operation_id
        return OutboundRequest(
            method=parts.method,
            url=str(url),
            headers=headers,
            json_body=parts.json_body,
        )

    async def send(self, request: OutboundRequest) -> OutboundOutcome:
        if request.url.host != self._definition.base_url.host:
            return self._internal_outcome("not_sent", "REQUEST_URL_HOST_MISMATCH")
        try:
            self._validate_send_target(request.url)
        except ValueError as exc:
            return self._internal_outcome("not_sent", str(exc))

        start = time.perf_counter()
        try:
            async with asyncio.timeout(self._definition.policy.total_timeout_seconds):
                response, response_stream = await self._send_http(request)
                try:
                    body_bytes, read_error = await self._read_response_bytes(response)
                    if response.status_code in {301, 302, 303, 307, 308}:
                        outcome = self._build_outcome(response, body_bytes=body_bytes, elapsed_ms=_elapsed_ms(start))
                        outcome.headers[INTERNAL_ERROR_CODE_HEADER] = "HTTP_REDIRECT_BLOCKED"
                        return outcome
                    if read_error is not None:
                        outcome = OutboundOutcome(
                            delivery_state="received",
                            status_code=response.status_code,
                            headers={key: value for key, value in response.headers.items()},
                            body=None,
                            request_id=response.headers.get("X-Request-Id") or response.headers.get("x-request-id"),
                            elapsed_ms=_elapsed_ms(start),
                        )
                        outcome.headers[INTERNAL_ERROR_CODE_HEADER] = read_error
                        return outcome
                    return self._build_outcome(response, body_bytes=body_bytes, elapsed_ms=_elapsed_ms(start))
                finally:
                    await response_stream.__aexit__(None, None, None)
        except TimeoutError:
            return self._internal_outcome("unknown", "DELIVERY_UNKNOWN", start)
        except httpx.ConnectTimeout:
            return self._internal_outcome("not_sent", "CONNECTION_TIMEOUT", start)
        except httpx.ConnectError:
            return self._internal_outcome("not_sent", "CONNECTION_FAILED", start)
        except httpx.ReadTimeout:
            return self._internal_outcome("unknown", "DELIVERY_UNKNOWN", start)
        except httpx.TimeoutException:
            return self._internal_outcome("unknown", "DELIVERY_UNKNOWN", start)

    def parse_response(self, outcome: OutboundOutcome) -> TransferResult:
        if self._last_operation_id is None:
            raise RuntimeError("operation context is missing")
        binding = self._binding_by_operation_id(self._last_operation_id)
        if binding is None:
            raise RuntimeError("operation binding is missing")
        target_resource_id: str | None = None
        if binding.postcondition is not None:
            target_resource_id = _extract_result_id(outcome.body, binding.postcondition.result_id_pointer)
        return TransferResult(
            target_resource_id=target_resource_id,
            target_request_id=outcome.request_id,
            postcondition_verified=False,
            completed_at=datetime.now(UTC),
            response_ref=None,
        )

    def classify_error(self, outcome: OutboundOutcome | Exception) -> ErrorClassification:
        if isinstance(outcome, Exception):
            if isinstance(outcome, (httpx.ConnectTimeout, httpx.ConnectError)):
                return ErrorClassification(code="CONNECTION_FAILED", retryable=True, delivery_state="not_sent")
            if isinstance(outcome, httpx.TimeoutException):
                return ErrorClassification(code="DELIVERY_UNKNOWN", retryable=False, delivery_state="unknown")
            return ErrorClassification(code="UNEXPECTED_ERROR", retryable=False, delivery_state="unknown")
        internal_code = outcome.headers.get(INTERNAL_ERROR_CODE_HEADER)
        if internal_code is not None:
            return ErrorClassification(
                code=internal_code,
                retryable=False,
                delivery_state=outcome.delivery_state,
                retry_after_seconds=_retry_after_seconds(outcome.headers),
            )
        status_code = outcome.status_code
        if status_code in {408, 425, 429}:
            return ErrorClassification(
            code=f"HTTP_{status_code}",
                retryable=True,
                delivery_state=outcome.delivery_state,
                retry_after_seconds=_retry_after_seconds(outcome.headers),
            )
        if status_code is not None and 500 <= status_code <= 599:
            return ErrorClassification(code="HTTP_5XX", retryable=True, delivery_state=outcome.delivery_state)
        if status_code is not None and 400 <= status_code <= 499:
            return ErrorClassification(code=f"HTTP_{status_code}", retryable=False, delivery_state=outcome.delivery_state)
        if outcome.delivery_state == "unknown":
            return ErrorClassification(code="DELIVERY_UNKNOWN", retryable=False, delivery_state="unknown")
        return ErrorClassification(code="OK", retryable=False, delivery_state=outcome.delivery_state)

    async def reconcile(self, context: ReconciliationContext) -> ReconciliationResult:
        _, binding, _ = self._binding_for_operation_id(context.operation_id)
        lookup_operation_id = binding.lookup_operation_id or binding.postcondition.reconcile_operation_id if binding.postcondition else None
        if lookup_operation_id is None:
            return ReconciliationResult(state="unknown")
        lookup_operation = self._preflight.operations.get(lookup_operation_id)
        if lookup_operation is None:
            return ReconciliationResult(state="unknown")
        lookup_name = binding.lookup_parameter_name
        lookup_location = binding.lookup_parameter_location
        if lookup_name is None or lookup_location is None:
            return ReconciliationResult(state="unknown")
        lookup_parameter = _lookup_parameter(lookup_operation, lookup_name, lookup_location)
        if lookup_parameter is None:
            return ReconciliationResult(state="unknown")
        raw_lookup_value: Any
        if context.target_resource_id is not None:
            raw_lookup_value = context.target_resource_id
        else:
            raw_lookup_value = _decode_canonical_scalar(context.deduplication_value)
        try:
            parameter_value = _coerce_lookup_parameter_value(raw_lookup_value, lookup_parameter)
        except ValueError:
            return ReconciliationResult(state="unknown")
        parts = OutboundRequestParts(
            operation_id=lookup_operation_id,
            method=lookup_operation.method,
            path=lookup_operation.path,
            path_params={lookup_name: parameter_value} if lookup_location == "path" else {},
            query_params={lookup_name: parameter_value} if lookup_location == "query" else {},
            headers={},
            json_body=None,
            idempotency_key=context.idempotency_key,
        )
        request = await self.build_request(parts)
        outcome = await self.send(request)
        if outcome.delivery_state != "received" or outcome.status_code is None:
            return ReconciliationResult(state="unknown")
        if binding.postcondition is not None and outcome.status_code in binding.postcondition.not_found_statuses:
            return ReconciliationResult(state="not_registered")
        if binding.postcondition is not None and outcome.status_code in binding.postcondition.registered_statuses:
            try:
                resource_id = _extract_result_id(outcome.body, binding.postcondition.result_id_pointer)
            except ValueError:
                resource_id = context.target_resource_id
            return ReconciliationResult(state="registered", target_resource_id=resource_id)
        return ReconciliationResult(state="unknown")

    async def _send_http(self, request: OutboundRequest) -> tuple[httpx.Response, Any]:
        response, response_stream = await self._dispatch_request(request.method, request.url, request.headers, request.json_body)
        if response.status_code == 401 and self._last_operation_id is not None and _token_expired(response.headers):
            operation = self._operation_by_id(self._last_operation_id)
            if self._invalidate_oauth_token(operation):
                await response_stream.__aexit__(None, None, None)
                auth_headers, auth_query = await self._build_auth(operation)
                retry_headers = dict(request.headers)
                retry_headers.update(auth_headers)
                retry_url = request.url.copy_merge_params(auth_query) if auth_query else request.url
                return await self._dispatch_request(request.method, retry_url, retry_headers, request.json_body)
        return response, response_stream

    async def _dispatch_request(
        self,
        method: str,
        url: httpx.URL,
        headers: dict[str, str],
        json_body: dict[str, Any] | list[Any] | None,
    ) -> tuple[httpx.Response, Any]:
        response_stream = self._http_client.stream(
            method,
            str(url),
            headers=headers,
            json=json_body,
            timeout=httpx.Timeout(
                connect=self._definition.policy.connect_timeout_seconds,
                read=self._definition.policy.read_timeout_seconds,
                write=self._definition.policy.read_timeout_seconds,
                pool=self._definition.policy.connect_timeout_seconds,
            ),
            follow_redirects=False,
        )
        response = await response_stream.__aenter__()
        return response, response_stream

    async def _read_response_bytes(self, response: httpx.Response) -> tuple[bytes, str | None]:
        max_bytes = self._definition.policy.max_response_bytes
        content_length = response.headers.get("content-length") or response.headers.get("Content-Length")
        if content_length is not None:
            try:
                if int(content_length) > max_bytes:
                    return b"", "RESPONSE_TOO_LARGE"
            except ValueError:
                pass
        chunks: list[bytes] = []
        total_bytes = 0
        async for chunk in response.aiter_bytes():
            total_bytes += len(chunk)
            if total_bytes > max_bytes:
                return b"", "RESPONSE_TOO_LARGE"
            chunks.append(chunk)
        return b"".join(chunks), None

    async def _build_auth(self, operation: NormalizedOperation) -> tuple[dict[str, str], dict[str, str]]:
        if not operation.security_options:
            return {}, {}
        option = operation.security_options[0]
        spec_schemes = self._definition.spec.get("components", {}).get("securitySchemes", {})
        bundle = await self._credential_resolver.resolve(self._definition.credential_ref)
        headers: dict[str, str] = {}
        query: dict[str, str] = {}
        for scheme_name, scopes in option.schemes.items():
            scheme = spec_schemes.get(scheme_name, {})
            scheme_type = scheme.get("type")
            if scheme_type == "apiKey":
                secret = _bundle_secret_value(bundle, preferred=(scheme_name, "auth", "api_key", "token", "value"))
                if scheme.get("in") == "query":
                    query[str(scheme.get("name"))] = secret
                else:
                    headers[str(scheme.get("name"))] = secret
                continue
            if scheme_type == "http" and str(scheme.get("scheme", "")).lower() == "bearer":
                token = _bundle_secret_value(bundle, preferred=(scheme_name, "token", "access_token", "value"))
                headers["Authorization"] = f"Bearer {token}"
                continue
            if scheme_type == "http" and str(scheme.get("scheme", "")).lower() == "basic":
                import base64

                username = _bundle_secret_value(bundle, preferred=("username", "user"))
                password = _bundle_secret_value(bundle, preferred=("password", "pass"))
                token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
                headers["Authorization"] = f"Basic {token}"
                continue
            if scheme_type == "oauth2":
                token = await self._oauth_access_token(scheme_name, scheme, scopes, bundle)
                headers["Authorization"] = f"Bearer {token}"
        return headers, query

    async def _oauth_access_token(
        self,
        scheme_name: str,
        scheme: dict[str, Any],
        scopes: list[str],
        bundle: SecretBundle,
    ) -> str:
        token_url = scheme.get("flows", {}).get("clientCredentials", {}).get("tokenUrl")
        if not isinstance(token_url, str):
            raise ValueError("OAUTH_TOKEN_URL_MISSING")
        token_url_object = httpx.URL(token_url)
        self._validate_target(
            token_url_object,
            self._oauth_registration_addresses.get(token_url_object.host),
        )
        cache_key = (scheme_name, token_url)
        cached = self._oauth_tokens.get(cache_key)
        if cached is not None and cached.expires_at > datetime.now(UTC) + timedelta(seconds=30):
            return cached.access_token
        data = {
            "grant_type": "client_credentials",
            "scope": " ".join(scopes),
        }
        response = await self._http_client.post(
            str(token_url_object),
            data=data,
            auth=(
                _bundle_secret_value(bundle, preferred=("client_id", "username", "user")),
                _bundle_secret_value(bundle, preferred=("client_secret", "password", "pass")),
            ),
            follow_redirects=False,
        )
        payload = response.json()
        access_token = str(payload["access_token"])
        expires_in = int(payload.get("expires_in", 3600))
        self._oauth_tokens[cache_key] = _OAuthToken(
            access_token=access_token,
            expires_at=datetime.now(UTC) + timedelta(seconds=expires_in),
        )
        return access_token

    def _invalidate_oauth_token(self, operation: NormalizedOperation) -> bool:
        if not operation.security_options:
            return False
        option = operation.security_options[0]
        invalidated = False
        for scheme_name in option.schemes:
            scheme = self._definition.spec.get("components", {}).get("securitySchemes", {}).get(scheme_name, {})
            token_url = scheme.get("flows", {}).get("clientCredentials", {}).get("tokenUrl")
            cache_key = (scheme_name, token_url)
            if cache_key in self._oauth_tokens:
                self._oauth_tokens.pop(cache_key, None)
                invalidated = True
        return invalidated

    def _binding_for_operation_id(self, operation_id: str) -> tuple[str, OperationBinding, NormalizedOperation]:
        for operation_name, binding in self._definition.operation_bindings.items():
            if binding.operation_id == operation_id:
                return operation_name, binding, self._operation_by_id(operation_id)
        raise ValueError("OPERATION_UNBOUND")

    def _binding_by_operation_id(self, operation_id: str) -> OperationBinding | None:
        for binding in self._definition.operation_bindings.values():
            if binding.operation_id == operation_id or binding.lookup_operation_id == operation_id:
                return binding
        return None

    def _operation_by_id(self, operation_id: str) -> NormalizedOperation:
        operation = self._preflight.operations.get(operation_id)
        if operation is None:
            raise ValueError("OPERATION_UNRESOLVED")
        return operation

    def _validate_send_target(self, url: httpx.URL) -> None:
        self._validate_target(url, self._registration_addresses)

    def _validate_target(
        self,
        url: httpx.URL,
        registration_addresses: list[str] | None,
    ) -> None:
        if url.scheme not in {"http", "https"} or url.host is None:
            raise ValueError("TARGET_URL_INVALID")
        if self._settings is not None and self._settings.allowed_hosts:
            if url.host not in self._settings.allowed_hosts:
                raise ValueError("HOST_NOT_ALLOWED")
        current_addresses = resolve_host_addresses(url.host)
        if any(_is_blocked_ip(address) for address in current_addresses):
            raise ValueError("SSRF_ADDRESS_BLOCKED")
        if registration_addresses is not None and set(current_addresses) != set(registration_addresses):
            raise ValueError("SSRF_DNS_REBINDING_DETECTED")

    def _build_outcome(self, response: httpx.Response, *, body_bytes: bytes, elapsed_ms: int = 0) -> OutboundOutcome:
        body: dict[str, Any] | list[Any] | None = None
        parse_error = False
        content_type = response.headers.get("content-type", "")
        if body_bytes and "json" in content_type:
            try:
                parsed = json.loads(body_bytes)
                if isinstance(parsed, (dict, list)):
                    body = parsed
                else:
                    parse_error = True
            except ValueError:
                parse_error = True
        elif body_bytes:
            try:
                parsed = json.loads(body_bytes.decode("utf-8"))
                if isinstance(parsed, (dict, list)):
                    body = parsed
                else:
                    parse_error = True
            except (UnicodeDecodeError, json.JSONDecodeError):
                parse_error = True
        operation = self._operation_by_id(self._last_operation_id) if self._last_operation_id is not None else None
        if _requires_success_body_validation(response.status_code, operation):
            if parse_error or body is None:
                return self._response_format_unknown(response, elapsed_ms)
            if operation is not None and operation.success_schema is not None:
                validator = Draft202012Validator(operation.success_schema)
                if not validator.is_valid(body):
                    return self._response_format_unknown(response, elapsed_ms)
        return OutboundOutcome(
            delivery_state="received",
            status_code=response.status_code,
            headers={key: value for key, value in response.headers.items()},
            body=body,
            request_id=response.headers.get("X-Request-Id") or response.headers.get("x-request-id"),
            elapsed_ms=elapsed_ms,
        )

    def _response_format_unknown(self, response: httpx.Response, elapsed_ms: int) -> OutboundOutcome:
        return OutboundOutcome(
            delivery_state="unknown",
            status_code=response.status_code,
            headers={
                **{key: value for key, value in response.headers.items()},
                INTERNAL_ERROR_CODE_HEADER: "RESPONSE_FORMAT_UNKNOWN",
            },
            body=None,
            request_id=response.headers.get("X-Request-Id") or response.headers.get("x-request-id"),
            elapsed_ms=elapsed_ms,
        )

    def _internal_outcome(self, delivery_state: str, code: str, start: float | None = None) -> OutboundOutcome:
        return OutboundOutcome(
            delivery_state=delivery_state,
            status_code=None,
            headers={INTERNAL_ERROR_CODE_HEADER: code},
            body=None,
            request_id=None,
            elapsed_ms=0 if start is None else _elapsed_ms(start),
        )


class ConnectorRegistry:
    def __init__(self, store: Any, credential_resolver: CredentialResolver, settings: Settings) -> None:
        self._store = store
        self._credential_resolver = credential_resolver
        self._settings = settings

    async def register(self, tenant_id: str, definition: ConnectorDefinition) -> None:
        client = httpx.AsyncClient(follow_redirects=False)
        try:
            snapshot = definition.model_copy(deep=True)
            registration_addresses = resolve_host_addresses(definition.base_url.host)
            if self._settings.allowed_hosts and definition.base_url.host not in self._settings.allowed_hosts:
                raise ValueError("HOST_NOT_ALLOWED")
            if any(_is_blocked_ip(address) for address in registration_addresses):
                raise ValueError("SSRF_ADDRESS_BLOCKED")
            snapshot.spec = deepcopy(snapshot.spec)
            snapshot.spec["x-openapi-to-mcp-registration-hosts"] = registration_addresses
            oauth_registration_addresses = _resolve_oauth_registration_addresses(
                snapshot.spec,
                self._settings,
            )
            if oauth_registration_addresses:
                snapshot.spec[OAUTH_REGISTRATION_HOSTS_EXTENSION] = oauth_registration_addresses
            connector = RestOpenApiConnector(
                snapshot,
                MappingEngine(),
                self._credential_resolver,
                client,
                settings=self._settings,
            )
            result = await connector.validate_config()
            if not result.valid:
                raise ValueError("CONTRACT_INVALID")
            for operation_name, binding in snapshot.operation_bindings.items():
                if binding.operation_id not in result.operations:
                    raise ValueError("OPERATION_UNRESOLVED")
                if operation_name in {"create", "upsert"} and not _has_reconciliation(binding):
                    raise ValueError("RECONCILIATION_NOT_CONFIGURED")
                if operation_name == "update" and not _update_identifies_target(binding, result.operations[binding.operation_id]):
                    raise ValueError("RECONCILIATION_NOT_CONFIGURED")
                if binding.postcondition is not None:
                    operation = result.operations[binding.operation_id]
                    try:
                        _validate_result_pointer(operation.success_schema, binding.postcondition.result_id_pointer)
                    except ValueError:
                        raise ValueError("RESULT_ID_POINTER_INVALID") from None
            await self._store.save_connector(snapshot, tenant_id)
        finally:
            await client.aclose()

    async def get(self, tenant_id: str, connector_id: str, version: int | None = None) -> RestOpenApiConnector:
        definition = await self._store.get_connector(tenant_id, connector_id, version)
        if definition is None:
            raise NotFoundError("connector not found")
        return RestOpenApiConnector(
            definition,
            MappingEngine(),
            self._credential_resolver,
            httpx.AsyncClient(follow_redirects=False),
            settings=self._settings,
        )

    async def validate(self, tenant_id: str, connector_id: str) -> ContractPreflightResult:
        connector = await self.get(tenant_id, connector_id)
        return await connector.validate_config()

    async def resolve_operation(
        self,
        tenant_id: str,
        connector_id: str,
        version: int,
        operation_name: str,
    ) -> OperationSelection:
        connector = await self.get(tenant_id, connector_id, version)
        return await connector.resolve_operation(operation_name)


def resolve_host_addresses(host: str) -> list[str]:
    values = {record[4][0] for record in socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)}
    return sorted(values)


def _oauth_token_hosts(spec: dict[str, Any]) -> set[str]:
    hosts: set[str] = set()
    security_schemes = spec.get("components", {}).get("securitySchemes", {})
    if not isinstance(security_schemes, dict):
        return hosts
    for scheme in security_schemes.values():
        if not isinstance(scheme, dict) or scheme.get("type") != "oauth2":
            continue
        token_url = scheme.get("flows", {}).get("clientCredentials", {}).get("tokenUrl")
        if not isinstance(token_url, str):
            continue
        url = httpx.URL(token_url)
        if url.host is not None:
            hosts.add(url.host)
    return hosts


def _oauth_registration_addresses(spec: dict[str, Any]) -> dict[str, list[str]]:
    raw = spec.get(OAUTH_REGISTRATION_HOSTS_EXTENSION)
    if isinstance(raw, dict):
        return {
            str(host): [str(address) for address in addresses]
            for host, addresses in raw.items()
            if isinstance(addresses, list)
        }
    return {host: resolve_host_addresses(host) for host in _oauth_token_hosts(spec)}


def _resolve_oauth_registration_addresses(
    spec: dict[str, Any],
    settings: Settings,
) -> dict[str, list[str]]:
    addresses_by_host: dict[str, list[str]] = {}
    security_schemes = spec.get("components", {}).get("securitySchemes", {})
    if not isinstance(security_schemes, dict):
        return addresses_by_host
    for scheme in security_schemes.values():
        if not isinstance(scheme, dict) or scheme.get("type") != "oauth2":
            continue
        token_url = scheme.get("flows", {}).get("clientCredentials", {}).get("tokenUrl")
        if not isinstance(token_url, str):
            continue
        url = httpx.URL(token_url)
        if url.scheme not in {"http", "https"} or url.host is None:
            raise ValueError("OAUTH_TOKEN_URL_INVALID")
        if settings.allowed_hosts and url.host not in settings.allowed_hosts:
            raise ValueError("HOST_NOT_ALLOWED")
        try:
            addresses = resolve_host_addresses(url.host)
        except OSError:
            raise ValueError("HOST_RESOLUTION_FAILED") from None
        if any(_is_blocked_ip(address) for address in addresses):
            raise ValueError("SSRF_ADDRESS_BLOCKED")
        addresses_by_host[url.host] = addresses
    return addresses_by_host


def _elapsed_ms(start: float) -> int:
    return max(0, int((time.perf_counter() - start) * 1000))


def _render_path(path: str, params: dict[str, str]) -> str:
    rendered = path
    for key, value in params.items():
        rendered = rendered.replace("{" + key + "}", quote(str(value), safe=""))
    if "{" in rendered:
        raise ValueError("PATH_PARAMETER_MISSING")
    return rendered


def _bundle_secret_value(bundle: SecretBundle, *, preferred: tuple[str, ...] = ("value",)) -> str:
    for key in preferred:
        if key in bundle.values:
            return bundle.values[key].get_secret_value()
    if len(bundle.values) == 1:
        return next(iter(bundle.values.values())).get_secret_value()
    raise ValueError("secret value not found")


def _extract_result_id(body: dict[str, Any] | list[Any] | None, pointer: str) -> str:
    if body is None:
        raise ValueError("RESPONSE_ID_INVALID")
    try:
        value = resolve_pointer(body, pointer)
    except JsonPointerException as exc:
        raise ValueError("RESPONSE_ID_INVALID") from exc
    if not isinstance(value, str) or not value:
        raise ValueError("RESPONSE_ID_INVALID")
    return value


def _token_expired(headers: dict[str, str]) -> bool:
    header_value = headers.get("WWW-Authenticate", headers.get("www-authenticate", ""))
    lowered = header_value.lower()
    return "invalid_token" in lowered or "expired" in lowered


def _retry_after_seconds(headers: dict[str, str]) -> int | None:
    raw = headers.get("Retry-After") or headers.get("retry-after")
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _decode_canonical_scalar(value: str) -> Any:
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        return value
    if isinstance(decoded, (dict, list)):
        return value
    return decoded


def _has_reconciliation(binding: OperationBinding) -> bool:
    return bool(
        binding.postcondition is not None
        and binding.lookup_operation_id
        and binding.lookup_parameter_name
        and binding.lookup_parameter_location
    )


def _update_identifies_target(binding: OperationBinding, operation: NormalizedOperation) -> bool:
    if binding.lookup_operation_id and binding.lookup_parameter_name and binding.lookup_parameter_location:
        return True
    return any(parameter.location == "path" and parameter.required for parameter in operation.parameters)


def _validate_result_pointer(schema: dict[str, Any] | None, pointer: str) -> None:
    if schema is None:
        raise ValueError(pointer)
    current: Any = schema
    for part in pointer.strip("/").split("/"):
        if not part:
            continue
        properties = current.get("properties") if isinstance(current, dict) else None
        if not isinstance(properties, dict) or part not in properties:
            raise ValueError(pointer)
        current = properties[part]
    if not isinstance(current, dict) or current.get("type") != "string":
        raise ValueError(pointer)


def _is_blocked_ip(address: str) -> bool:
    ip = ipaddress.ip_address(address)
    return bool(
        ip.is_loopback
        or ip.is_private
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def _join_base_url_path(base_url: Any, path: str) -> httpx.URL:
    url = httpx.URL(str(base_url))
    base_path = url.path.rstrip("/")
    if base_path == "/":
        base_path = ""
    rendered_path = path if path.startswith("/") else f"/{path}"
    return url.copy_with(path=f"{base_path}{rendered_path}")


def _requires_success_body_validation(
    status_code: int,
    operation: NormalizedOperation | None,
) -> bool:
    return operation is not None and operation.success_schema is not None and 200 <= status_code <= 299


def _lookup_parameter(
    operation: NormalizedOperation,
    name: str,
    location: str,
) -> ParameterDefinition | None:
    for parameter in operation.parameters:
        if parameter.name == name and parameter.location == location:
            return parameter
    return None


def _coerce_lookup_parameter_value(value: Any, parameter: ParameterDefinition) -> str:
    schema = parameter.schema
    schema_type = schema.get("type")
    if schema_type in {"array", "object"} or isinstance(value, (dict, list)) or value is None:
        raise ValueError("LOOKUP_PARAMETER_INVALID")
    if schema_type == "string" or schema_type is None:
        coerced: Any = value if isinstance(value, str) else _encode_scalar(value)
    elif schema_type == "integer":
        coerced = _coerce_integer(value)
    elif schema_type == "number":
        coerced = _coerce_number(value)
    elif schema_type == "boolean":
        coerced = _coerce_boolean(value)
    else:
        raise ValueError("LOOKUP_PARAMETER_UNSUPPORTED")
    enum_values = schema.get("enum")
    if isinstance(enum_values, list) and coerced not in enum_values:
        raise ValueError("LOOKUP_PARAMETER_INVALID")
    return _encode_scalar(coerced)


def _coerce_integer(value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError("LOOKUP_PARAMETER_INVALID")
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and re.fullmatch(r"[+-]?\d+", value):
        return int(value)
    raise ValueError("LOOKUP_PARAMETER_INVALID")


def _coerce_number(value: Any) -> int | float:
    if isinstance(value, bool):
        raise ValueError("LOOKUP_PARAMETER_INVALID")
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        try:
            number = float(value)
        except ValueError as exc:
            raise ValueError("LOOKUP_PARAMETER_INVALID") from exc
        return int(number) if number.is_integer() else number
    raise ValueError("LOOKUP_PARAMETER_INVALID")


def _coerce_boolean(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.lower()
        if lowered == "true":
            return True
        if lowered == "false":
            return False
    raise ValueError("LOOKUP_PARAMETER_INVALID")


def _encode_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return json.dumps(value, separators=(",", ":"))
    return str(value)