from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import re
from typing import Any, Literal

from openapi_spec_validator import validate
from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, PrivateAttr, ValidationError


HTTP_METHODS = {"get", "put", "post", "delete", "options", "head", "patch", "trace"}


class ParameterDefinition(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    name: str
    location: Literal["path", "query", "header", "cookie"]
    required: bool
    schema_: dict[str, Any] = Field(alias="schema", serialization_alias="schema")

    @property
    def schema(self) -> dict[str, Any]:
        return self.schema_


class SecurityOption(BaseModel):
    schemes: dict[str, list[str]]


class ContractIssue(BaseModel):
    code: str
    location: str
    message: str
    severity: Literal["error", "warning"]


class NormalizedOperation(BaseModel):
    operation_id: str
    method: str
    path: str
    parameters: list[ParameterDefinition]
    request_schema: dict[str, Any] | None
    success_schema: dict[str, Any] | None
    error_statuses: list[int]
    security_options: list[SecurityOption]
    required_headers: list[str]


class ContractPreflightResult(BaseModel):
    valid: bool
    base_url: AnyHttpUrl | None
    operations: dict[str, NormalizedOperation]
    issues: list[ContractIssue] = Field(default_factory=list)
    spec_hash: str
    _resolved_spec: dict[str, Any] = PrivateAttr(default_factory=dict)

    def resolve_target_schema(self, schema_ref: str, operation_id: str) -> dict[str, Any]:
        if not schema_ref.startswith("openapi:#/"):
            raise ValueError("TARGET_SCHEMA_UNRESOLVED")
        pointer = schema_ref[len("openapi:#") :]
        if pointer.startswith(f"/operations/{operation_id}/request_schema"):
            operation = self.operations.get(operation_id)
            if operation is None or operation.request_schema is None:
                raise ValueError("TARGET_SCHEMA_UNRESOLVED")
            return deepcopy(operation.request_schema)
        if pointer.startswith("/components/schemas/"):
            schema = _resolve_pointer(self._resolved_spec, pointer)
            if not isinstance(schema, dict):
                raise ValueError("TARGET_SCHEMA_UNRESOLVED")
            return deepcopy(schema)
        raise ValueError("TARGET_SCHEMA_UNRESOLVED")


class ContractPreflight:
    def run(self, spec: dict[str, Any]) -> ContractPreflightResult:
        source = deepcopy(spec)
        issues: list[ContractIssue] = []
        spec_hash = hashlib.sha256(
            json.dumps(source, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        ).hexdigest()
        _collect_ref_issues(source, issues, path="#")
        try:
            validate(source)
        except Exception as exc:
            issues.append(
                ContractIssue(
                    code="OPENAPI_INVALID",
                    location="#",
                    message=str(exc),
                    severity="error",
                )
            )

        resolved_spec = _resolve_local_refs(source, source, issues, "#")
        base_url = _resolve_base_url(resolved_spec, issues)
        operations: dict[str, NormalizedOperation] = {}
        seen_ids: set[str] = set()

        for path_name, path_item in resolved_spec.get("paths", {}).items():
            if not isinstance(path_item, dict):
                continue
            path_parameters = path_item.get("parameters", [])
            for method_name, raw_operation in path_item.items():
                if method_name not in HTTP_METHODS or not isinstance(raw_operation, dict):
                    continue
                operation = _resolve_local_refs(raw_operation, resolved_spec, issues, f"#/paths/{path_name}/{method_name}")
                operation_id = operation.get("operationId") or _generated_operation_id(method_name, path_name)
                if operation_id in seen_ids:
                    issues.append(
                        ContractIssue(
                            code="DUPLICATE_OPERATION_ID",
                            location=f"#/paths/{path_name}/{method_name}",
                            message=f"operationId {operation_id} is duplicated",
                            severity="error",
                        )
                    )
                    continue
                seen_ids.add(operation_id)
                parameters = _normalize_parameters(path_parameters, operation.get("parameters", []), resolved_spec, issues)
                security_options = _normalize_security_options(
                    operation.get("security"),
                    resolved_spec.get("components", {}).get("securitySchemes", {}),
                    issues,
                    f"#/paths/{path_name}/{method_name}/security",
                )
                request_schema = _request_schema(operation)
                success_schema = _success_schema(operation)
                error_statuses = _error_statuses(operation)
                required_headers = [parameter.name for parameter in parameters if parameter.location == "header" and parameter.required]
                operations[operation_id] = NormalizedOperation(
                    operation_id=operation_id,
                    method=method_name.upper(),
                    path=path_name,
                    parameters=parameters,
                    request_schema=request_schema,
                    success_schema=success_schema,
                    error_statuses=error_statuses,
                    security_options=security_options,
                    required_headers=required_headers,
                )

        result = ContractPreflightResult(
            valid=not any(issue.severity == "error" for issue in issues),
            base_url=base_url,
            operations=operations,
            issues=issues,
            spec_hash=spec_hash,
        )
        result._resolved_spec = resolved_spec
        return result


def _resolve_base_url(spec: dict[str, Any], issues: list[ContractIssue]) -> AnyHttpUrl | None:
    servers = spec.get("servers") or []
    if not servers:
        issues.append(
            ContractIssue(
                code="BASE_URL_MISSING",
                location="#/servers",
                message="servers[0].url is required",
                severity="error",
            )
        )
        return None
    url = servers[0].get("url") if isinstance(servers[0], dict) else None
    if not isinstance(url, str):
        issues.append(
            ContractIssue(
                code="BASE_URL_MISSING",
                location="#/servers/0/url",
                message="server url is required",
                severity="error",
            )
        )
        return None
    try:
        return AnyHttpUrl(url)
    except ValidationError:
        issues.append(
            ContractIssue(
                code="BASE_URL_NOT_ABSOLUTE",
                location="#/servers/0/url",
                message="server url must be absolute http or https",
                severity="error",
            )
        )
        return None


def _normalize_parameters(
    path_parameters: list[Any],
    operation_parameters: list[Any],
    spec: dict[str, Any],
    issues: list[ContractIssue],
) -> list[ParameterDefinition]:
    normalized: dict[tuple[str, str], ParameterDefinition] = {}
    for source in list(path_parameters) + list(operation_parameters):
        parameter = _resolve_local_refs(source, spec, issues, "#/parameter")
        if not isinstance(parameter, dict):
            continue
        location = parameter.get("in")
        name = parameter.get("name")
        if location not in {"path", "query", "header", "cookie"} or not isinstance(name, str):
            continue
        schema = parameter.get("schema") if isinstance(parameter.get("schema"), dict) else {}
        normalized[(location, name)] = ParameterDefinition(
            name=name,
            location=location,
            required=bool(parameter.get("required", False)),
            schema=deepcopy(schema),
        )
    return list(normalized.values())


def _normalize_security_options(
    security: Any,
    security_schemes: dict[str, Any],
    issues: list[ContractIssue],
    location: str,
) -> list[SecurityOption]:
    if security is None:
        issues.append(
            ContractIssue(
                code="OPERATION_SECURITY_REQUIRED",
                location=location,
                message="operation-level security is required",
                severity="error",
            )
        )
        return []
    options: list[SecurityOption] = []
    for entry in security:
        if not isinstance(entry, dict):
            continue
        resolved: dict[str, list[str]] = {}
        for scheme_name, scopes in entry.items():
            if scheme_name not in security_schemes:
                issues.append(
                    ContractIssue(
                        code="SECURITY_UNDEFINED",
                        location=location,
                        message=f"security scheme {scheme_name} is not defined",
                        severity="error",
                    )
                )
                continue
            resolved[scheme_name] = list(scopes) if isinstance(scopes, list) else []
        if resolved:
            options.append(SecurityOption(schemes=resolved))
    return options


def _request_schema(operation: dict[str, Any]) -> dict[str, Any] | None:
    content = operation.get("requestBody", {}).get("content", {})
    for media_type in ("application/json", "application/*+json"):
        schema = content.get(media_type, {}).get("schema")
        if isinstance(schema, dict):
            return deepcopy(schema)
    return None


def _success_schema(operation: dict[str, Any]) -> dict[str, Any] | None:
    responses = operation.get("responses", {})
    for status in sorted(responses):
        if not str(status).startswith("2"):
            continue
        schema = responses.get(status, {}).get("content", {}).get("application/json", {}).get("schema")
        if isinstance(schema, dict):
            return deepcopy(schema)
    return None


def _error_statuses(operation: dict[str, Any]) -> list[int]:
    statuses: list[int] = []
    for status in operation.get("responses", {}):
        if not isinstance(status, str) or len(status) != 3 or not status.isdigit():
            continue
        code = int(status)
        if 400 <= code <= 599:
            statuses.append(code)
    return sorted(set(statuses))


def _generated_operation_id(method: str, path: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9]+", "_", path).strip("_") or "root"
    return f"{method.lower()}_{normalized}"


def _collect_ref_issues(value: Any, issues: list[ContractIssue], path: str) -> None:
    if isinstance(value, dict):
        ref = value.get("$ref")
        if isinstance(ref, str) and not ref.startswith("#"):
            issues.append(
                ContractIssue(
                    code="EXTERNAL_REF_UNSUPPORTED",
                    location=f"{path}/$ref",
                    message="only local document refs are supported",
                    severity="error",
                )
            )
        for key, child in value.items():
            _collect_ref_issues(child, issues, f"{path}/{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _collect_ref_issues(child, issues, f"{path}/{index}")


def _resolve_local_refs(value: Any, root: dict[str, Any], issues: list[ContractIssue], path: str) -> Any:
    if isinstance(value, dict):
        if "$ref" in value and isinstance(value["$ref"], str):
            ref = value["$ref"]
            if not ref.startswith("#"):
                return {}
            try:
                resolved = _resolve_pointer(root, ref[1:] or "/")
            except ValueError:
                issues.append(
                    ContractIssue(
                        code="REF_UNRESOLVED",
                        location=f"{path}/$ref",
                        message=f"could not resolve {ref}",
                        severity="error",
                    )
                )
                return {}
            if not isinstance(resolved, dict):
                return resolved
            merged = deepcopy(resolved)
            for key, child in value.items():
                if key == "$ref":
                    continue
                merged[key] = _resolve_local_refs(child, root, issues, f"{path}/{key}")
            return merged
        return {key: _resolve_local_refs(child, root, issues, f"{path}/{key}") for key, child in value.items()}
    if isinstance(value, list):
        return [_resolve_local_refs(child, root, issues, f"{path}/{index}") for index, child in enumerate(value)]
    return deepcopy(value)


def _resolve_pointer(document: Any, pointer: str) -> Any:
    if pointer in {"", "/"}:
        return document
    parts = [part.replace("~1", "/").replace("~0", "~") for part in pointer.lstrip("/").split("/")]
    current = document
    for part in parts:
        if isinstance(current, list):
            current = current[int(part)]
            continue
        if not isinstance(current, dict) or part not in current:
            raise ValueError(pointer)
        current = current[part]
    return current