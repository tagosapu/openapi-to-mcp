"""Deterministic schema-based mock data generation for OpenAPI operations."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from hashlib import sha256
from random import Random
import re
from typing import Any, Literal

from .openapi_mcp_codegen import OperationMetadata, collect_operations

_FIXED_FORMAT_VALUES = {
    "date": "2024-01-01",
    "date-time": "2024-01-01T00:00:00Z",
    "uuid": "00000000-0000-4000-8000-000000000000",
    "email": "user@example.com",
}
_SAFE_PATTERN_RE = re.compile(r"^[A-Za-z0-9 _-]+$")
_STRING_ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789"


@dataclass(frozen=True)
class MockOperationScenario:
    operation_id: str
    tool_name: str
    method: str
    path: str
    tool_arguments: dict[str, Any]
    status_code: int
    response_headers: dict[str, str]
    response_body: Any | None
    scenario_kind: Literal["success", "http_error"]
    validation_status: Literal["ready", "unvalidated", "skipped"] = "ready"
    validation_reason: str | None = None
    response_mode: Literal["json", "raw"] = "json"
    response_delay_seconds: float = 0.0


class MockDataGenerationError(ValueError):
    """Raised when a schema cannot be represented deterministically."""

    def __init__(self, path: str, reason: str) -> None:
        super().__init__(f"{path}: {reason}")
        self.path = path
        self.reason = reason


def build_mock_scenarios(
    openapi_spec: Mapping[str, Any],
    *,
    seed: int = 0,
) -> list[MockOperationScenario]:
    """Create deterministic mock scenarios for every collected operation."""
    scenarios: list[MockOperationScenario] = []

    for index, operation in enumerate(collect_operations(openapi_spec)):
        operation_path = (
            f"$.paths.{operation.path}.{operation.method.lower()}"
            f"[{index}]"
        )
        raw_operation = _lookup_raw_operation(openapi_spec, operation)
        tool_arguments, request_reasons = _build_request_arguments(
            operation,
            openapi_spec,
            seed,
            operation_path,
        )

        scenarios.append(
            _build_success_scenario(
                operation,
                raw_operation,
                openapi_spec,
                seed,
                operation_path,
                tool_arguments,
                request_reasons,
            )
        )
        scenarios.extend(
            _build_error_scenarios(
                operation,
                raw_operation,
                openapi_spec,
                seed,
                operation_path,
                tool_arguments,
                request_reasons,
            )
        )

    return scenarios


def generate_schema_value(
    schema: Mapping[str, Any],
    *,
    openapi_spec: Mapping[str, Any],
    seed: int,
    path: str = "$",
) -> Any:
    """Generate a deterministic example value from an OpenAPI schema."""
    resolved = _resolve_schema(schema, openapi_spec, path, set())
    return _generate_resolved_schema_value(resolved, openapi_spec, seed, path)


def _build_request_arguments(
    operation: OperationMetadata,
    openapi_spec: Mapping[str, Any],
    seed: int,
    operation_path: str,
) -> tuple[dict[str, Any], list[str]]:
    tool_arguments: dict[str, Any] = {}
    reasons: list[str] = []

    for parameter in operation.parameters:
        parameter_path = (
            f"{operation_path}.parameters.{parameter.location}.{parameter.python_name}"
        )
        try:
            tool_arguments[parameter.python_name] = generate_schema_value(
                parameter.schema,
                openapi_spec=openapi_spec,
                seed=seed,
                path=parameter_path,
            )
        except MockDataGenerationError as exc:
            reasons.append(_redact_generation_error(exc))

    if operation.request_body is not None and operation.request_body.required:
        body_name = _body_argument_name(operation)
        body_schema = operation.request_body.schema
        if body_schema is None:
            reasons.append("required request body schema is not documented")
        else:
            body_path = f"{operation_path}.requestBody"
            try:
                tool_arguments[body_name] = generate_schema_value(
                    body_schema,
                    openapi_spec=openapi_spec,
                    seed=seed,
                    path=body_path,
                )
            except MockDataGenerationError as exc:
                reasons.append(_redact_generation_error(exc))

    return tool_arguments, reasons


def _build_success_scenario(
    operation: OperationMetadata,
    raw_operation: Mapping[str, Any],
    openapi_spec: Mapping[str, Any],
    seed: int,
    operation_path: str,
    tool_arguments: Mapping[str, Any],
    request_reasons: Sequence[str],
) -> MockOperationScenario:
    status_code, raw_response = _first_response(raw_operation, kind="success")
    reasons = list(request_reasons)
    response_headers: dict[str, str] = {}
    response_body: Any | None = None

    if status_code is None:
        status_code = 200
        reasons.append("no documented 2xx response")
    elif status_code != 204:
        response_body, extra_headers, response_reasons = _build_response_payload(
            raw_response,
            openapi_spec,
            seed,
            f"{operation_path}.responses.{status_code}",
        )
        response_headers.update(extra_headers)
        reasons.extend(response_reasons)

    validation_status, validation_reason = _validation_fields(reasons)
    return MockOperationScenario(
        operation_id=operation.operation_id,
        tool_name=operation.tool_name,
        method=operation.method,
        path=operation.path,
        tool_arguments=dict(tool_arguments),
        status_code=status_code,
        response_headers=response_headers,
        response_body=response_body,
        scenario_kind="success",
        validation_status=validation_status,
        validation_reason=validation_reason,
    )


def _build_error_scenarios(
    operation: OperationMetadata,
    raw_operation: Mapping[str, Any],
    openapi_spec: Mapping[str, Any],
    seed: int,
    operation_path: str,
    tool_arguments: Mapping[str, Any],
    request_reasons: Sequence[str],
) -> list[MockOperationScenario]:
    scenarios: list[MockOperationScenario] = []
    responses = raw_operation.get("responses", {})
    if not isinstance(responses, Mapping):
        return scenarios

    for status_code_text, raw_response in responses.items():
        if not isinstance(status_code_text, str) or not status_code_text.isdigit():
            continue
        status_code = int(status_code_text)
        if status_code < 400 or status_code >= 600:
            continue

        response_body, response_headers, response_reasons = _build_response_payload(
            raw_response,
            openapi_spec,
            _derive_seed(seed, f"{operation_path}.responses.{status_code}.error"),
            f"{operation_path}.responses.{status_code}",
        )
        validation_reasons = list(request_reasons)
        if isinstance(raw_response, Mapping):
            response_content = raw_response.get("content")
            if isinstance(response_content, Mapping) and response_content:
                validation_reasons.append(
                    "documented HTTP error response body cannot be validated"
                )
                validation_reasons.extend(response_reasons)

        validation_status, validation_reason = _validation_fields(validation_reasons)
        scenarios.append(
            MockOperationScenario(
                operation_id=operation.operation_id,
                tool_name=operation.tool_name,
                method=operation.method,
                path=operation.path,
                tool_arguments=dict(tool_arguments),
                status_code=status_code,
                response_headers=response_headers,
                response_body=response_body,
                scenario_kind="http_error",
                validation_status=validation_status,
                validation_reason=validation_reason,
            )
        )

    return scenarios


def _build_response_payload(
    raw_response: Any,
    openapi_spec: Mapping[str, Any],
    seed: int,
    path: str,
) -> tuple[Any | None, dict[str, str], list[str]]:
    reasons: list[str] = []
    headers: dict[str, str] = {}

    if not isinstance(raw_response, Mapping):
        reasons.append("response entry is not an object")
        return None, headers, reasons

    content = raw_response.get("content", {})
    if not isinstance(content, Mapping) or not content:
        reasons.append("response schema is not documented")
        return None, headers, reasons

    content_type = (
        "application/json" if "application/json" in content else next(iter(content), None)
    )
    if not isinstance(content_type, str):
        reasons.append("response content type is not documented")
        return None, headers, reasons

    headers["content-type"] = content_type
    media_type = content.get(content_type, {})
    if not isinstance(media_type, Mapping):
        reasons.append(f"response content {content_type} is not an object")
        return None, headers, reasons

    schema = media_type.get("schema")
    if not isinstance(schema, Mapping):
        reasons.append("response schema is not documented")
        return None, headers, reasons

    try:
        body = generate_schema_value(
            schema,
            openapi_spec=openapi_spec,
            seed=seed,
            path=f"{path}.content.{content_type}.schema",
        )
    except MockDataGenerationError as exc:
        reasons.append(_redact_generation_error(exc))
        return None, headers, reasons
    return body, headers, reasons


def _first_response(
    raw_operation: Mapping[str, Any], *, kind: Literal["success"]
) -> tuple[int | None, Any]:
    responses = raw_operation.get("responses", {})
    if not isinstance(responses, Mapping):
        return None, None

    for status_code_text, raw_response in responses.items():
        if not isinstance(status_code_text, str) or not status_code_text.isdigit():
            continue
        status_code = int(status_code_text)
        if kind == "success" and 200 <= status_code < 300:
            return status_code, raw_response
    return None, None


def _lookup_raw_operation(
    openapi_spec: Mapping[str, Any], operation: OperationMetadata
) -> Mapping[str, Any]:
    paths = openapi_spec.get("paths", {})
    if not isinstance(paths, Mapping):
        return {}
    path_item = paths.get(operation.path, {})
    if not isinstance(path_item, Mapping):
        return {}
    raw_operation = path_item.get(operation.method.lower(), {})
    if not isinstance(raw_operation, Mapping):
        return {}
    return raw_operation


def _resolve_schema(
    schema: Mapping[str, Any],
    openapi_spec: Mapping[str, Any],
    path: str,
    resolving: set[str],
) -> Mapping[str, Any]:
    reference = schema.get("$ref")
    if isinstance(reference, str):
        if not reference.startswith("#/"):
            raise MockDataGenerationError(path, "only local $ref values are supported")
        if reference in resolving:
            raise MockDataGenerationError(path, f"circular reference: {reference}")

        target: Any = openapi_spec
        try:
            for part in reference[2:].split("/"):
                target = target[part.replace("~1", "/").replace("~0", "~")]
        except (KeyError, TypeError):
            raise MockDataGenerationError(path, f"unresolved reference: {reference}") from None
        if not isinstance(target, Mapping):
            raise MockDataGenerationError(path, f"reference does not resolve to an object: {reference}")

        merged = dict(
            _resolve_schema(target, openapi_spec, path, resolving | {reference})
        )
        for key, value in schema.items():
            if key == "$ref":
                continue
            merged[key] = value
        schema = merged

    all_of = schema.get("allOf")
    if isinstance(all_of, Sequence) and not isinstance(all_of, (str, bytes)):
        merged: dict[str, Any] = {}
        for index, branch in enumerate(all_of):
            if not isinstance(branch, Mapping):
                raise MockDataGenerationError(
                    f"{path}/allOf/{index}", "allOf branch is not an object"
                )
            merged = _merge_schema(
                merged,
                _resolve_schema(branch, openapi_spec, f"{path}/allOf/{index}", resolving),
            )
        merged = _merge_schema(
            merged,
            {key: value for key, value in schema.items() if key != "allOf"},
        )
        schema = merged

    return dict(schema)


def _generate_resolved_schema_value(
    schema: Mapping[str, Any],
    openapi_spec: Mapping[str, Any],
    seed: int,
    path: str,
) -> Any:
    if "example" in schema:
        return schema["example"]
    if isinstance(schema.get("enum"), Sequence) and not isinstance(
        schema["enum"], (str, bytes)
    ):
        values = list(schema["enum"])
        if values:
            return values[0]
    if "default" in schema:
        return schema["default"]

    for key in ("oneOf", "anyOf"):
        branches = schema.get(key)
        if isinstance(branches, Sequence) and not isinstance(branches, (str, bytes)):
            errors: list[str] = []
            for index, branch in enumerate(branches):
                if not isinstance(branch, Mapping):
                    errors.append(f"{key}[{index}] is not an object")
                    continue
                branch_path = f"{path}/{key}/{index}"
                try:
                    return generate_schema_value(
                        branch,
                        openapi_spec=openapi_spec,
                        seed=_derive_seed(seed, branch_path),
                        path=branch_path,
                    )
                except MockDataGenerationError as exc:
                    errors.append(_redact_generation_error(exc))
            raise MockDataGenerationError(path, f"no valid {key} branch: {'; '.join(errors)}")

    if schema.get("nullable") is True and not _has_material_schema(schema):
        return None
    if schema.get("type") == "null":
        return None

    schema_type = _schema_type(schema, path)
    rng = Random(_derive_seed(seed, path))

    if schema_type == "string":
        return _string_value(schema, rng, path)
    if schema_type == "integer":
        return _integer_value(schema, rng, path)
    if schema_type == "number":
        return _number_value(schema, rng, path)
    if schema_type == "boolean":
        return rng.choice([False, True])
    if schema_type == "object":
        return _object_value(schema, openapi_spec, seed, path)
    if schema_type == "array":
        return _array_value(schema, openapi_spec, seed, path, rng)
    raise MockDataGenerationError(path, f"unsupported schema type: {schema_type!r}")


def _string_value(schema: Mapping[str, Any], rng: Random, path: str) -> str:
    fmt = schema.get("format")
    if isinstance(fmt, str) and fmt in _FIXED_FORMAT_VALUES:
        return _apply_length_constraints(_FIXED_FORMAT_VALUES[fmt], schema, path)

    pattern = schema.get("pattern")
    if isinstance(pattern, str):
        literal = _literal_pattern(pattern, path)
        return _apply_length_constraints(literal, schema, path)

    minimum = _int_constraint(schema, "minLength", default=1)
    maximum = _int_constraint(schema, "maxLength", default=max(minimum, 12))
    if maximum < minimum:
        raise MockDataGenerationError(path, "maxLength is smaller than minLength")

    length = minimum if minimum == maximum else rng.randint(minimum, maximum)
    base = _path_token(path) or "value"
    raw = (base + "_" + "".join(rng.choice(_STRING_ALPHABET) for _ in range(length + 4)))
    return raw[:length]


def _integer_value(schema: Mapping[str, Any], rng: Random, path: str) -> int:
    minimum = _numeric_bound(schema, "minimum", default=0)
    maximum = _numeric_bound(schema, "maximum", default=minimum + 9)
    if maximum < minimum:
        raise MockDataGenerationError(path, "maximum is smaller than minimum")
    lower = int(minimum)
    upper = int(maximum)
    if upper < lower:
        upper = lower
    return lower if lower == upper else rng.randint(lower, upper)


def _number_value(schema: Mapping[str, Any], rng: Random, path: str) -> float:
    minimum = float(_numeric_bound(schema, "minimum", default=0.0))
    maximum = float(_numeric_bound(schema, "maximum", default=minimum + 9.0))
    if maximum < minimum:
        raise MockDataGenerationError(path, "maximum is smaller than minimum")
    if maximum == minimum:
        return minimum
    return round(rng.uniform(minimum, maximum), 3)


def _object_value(
    schema: Mapping[str, Any],
    openapi_spec: Mapping[str, Any],
    seed: int,
    path: str,
) -> dict[str, Any]:
    properties = schema.get("properties", {})
    required = {
        str(name)
        for name in schema.get("required", [])
        if isinstance(name, str)
    }
    value: dict[str, Any] = {}

    if isinstance(properties, Mapping):
        for property_name, property_schema in properties.items():
            if not isinstance(property_name, str) or not isinstance(property_schema, Mapping):
                continue
            property_path = f"{path}/{property_name}"
            try:
                property_value = generate_schema_value(
                    property_schema,
                    openapi_spec=openapi_spec,
                    seed=_derive_seed(seed, property_path),
                    path=property_path,
                )
            except MockDataGenerationError:
                if property_name in required:
                    raise
                continue
            value[property_name] = property_value

    additional_properties = schema.get("additionalProperties")
    if isinstance(additional_properties, Mapping):
        extra_key = "additional_property"
        value[extra_key] = generate_schema_value(
            additional_properties,
            openapi_spec=openapi_spec,
            seed=_derive_seed(seed, f"{path}/{extra_key}"),
            path=f"{path}/{extra_key}",
        )
    elif additional_properties is True and not value:
        raise MockDataGenerationError(path, "unconstrained additionalProperties is not supported")

    return value


def _array_value(
    schema: Mapping[str, Any],
    openapi_spec: Mapping[str, Any],
    seed: int,
    path: str,
    rng: Random,
) -> list[Any]:
    items = schema.get("items")
    if not isinstance(items, Mapping):
        raise MockDataGenerationError(path, "array items schema is not documented")

    minimum = _int_constraint(schema, "minItems", default=1)
    maximum = _int_constraint(schema, "maxItems", default=max(minimum, 2))
    if maximum < minimum:
        raise MockDataGenerationError(path, "maxItems is smaller than minItems")

    maximum = min(maximum, max(minimum, 3))
    length = minimum if minimum == maximum else rng.randint(minimum, maximum)
    return [
        generate_schema_value(
            items,
            openapi_spec=openapi_spec,
            seed=_derive_seed(seed, f"{path}/{index}"),
            path=f"{path}/{index}",
        )
        for index in range(length)
    ]


def _schema_type(schema: Mapping[str, Any], path: str) -> str:
    schema_type = schema.get("type")
    if isinstance(schema_type, str):
        return schema_type
    if isinstance(schema.get("properties"), Mapping) or "additionalProperties" in schema:
        return "object"
    if "items" in schema:
        return "array"
    raise MockDataGenerationError(path, "schema type is not documented")


def _body_argument_name(operation: OperationMetadata) -> str:
    used_names = {parameter.python_name for parameter in operation.parameters}
    candidate = "body"
    suffix = 2
    while candidate in used_names:
        candidate = f"body_{suffix}"
        suffix += 1
    return candidate


def _validation_fields(
    reasons: Sequence[str],
) -> tuple[Literal["ready", "unvalidated", "skipped"], str | None]:
    unique_reasons = [reason for index, reason in enumerate(reasons) if reason and reason not in reasons[:index]]
    if not unique_reasons:
        return "ready", None
    return "unvalidated", "; ".join(unique_reasons)


def _numeric_bound(schema: Mapping[str, Any], key: str, default: float) -> float:
    value = schema.get(key)
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return float(value)
    return default


def _int_constraint(schema: Mapping[str, Any], key: str, default: int) -> int:
    value = schema.get(key)
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    return default


def _apply_length_constraints(
    value: str, schema: Mapping[str, Any], path: str
) -> str:
    minimum = _int_constraint(schema, "minLength", default=0)
    maximum = _int_constraint(schema, "maxLength", default=max(minimum, len(value)))
    if maximum < minimum:
        raise MockDataGenerationError(path, "maxLength is smaller than minLength")
    if len(value) > maximum:
        value = value[:maximum]
    if len(value) < minimum:
        repeats = ((minimum - len(value)) // max(len(value), 1)) + 1
        value = (value or "x") * repeats
        value = value[:minimum]
    return value


def _literal_pattern(pattern: str, path: str) -> str:
    candidate = pattern.removeprefix("^").removesuffix("$")
    if not candidate or not _SAFE_PATTERN_RE.fullmatch(candidate):
        raise MockDataGenerationError(path, f"unsupported pattern: {pattern}")
    return candidate


def _path_token(path: str) -> str:
    candidate = path.split("/")[-1].split(".")[-1]
    candidate = re.sub(r"[^a-zA-Z0-9]+", "_", candidate).strip("_").lower()
    return candidate or "value"


def _derive_seed(seed: int, path: str) -> int:
    digest = sha256(f"{seed}:{path}".encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big")


def _merge_schema(base: Mapping[str, Any], update: Mapping[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in update.items():
        if key == "properties" and isinstance(merged.get(key), Mapping) and isinstance(value, Mapping):
            merged[key] = {**dict(merged[key]), **dict(value)}
        elif key == "required":
            existing = [item for item in merged.get(key, []) if isinstance(item, str)]
            incoming = [item for item in value if isinstance(item, str)] if isinstance(value, Sequence) and not isinstance(value, (str, bytes)) else []
            merged[key] = list(dict.fromkeys([*existing, *incoming]))
        else:
            merged[key] = value
    return merged


def _has_material_schema(schema: Mapping[str, Any]) -> bool:
    return any(
        key in schema
        for key in ("type", "properties", "items", "$ref", "oneOf", "anyOf", "allOf", "enum", "default", "example")
    )


def _redact_generation_error(error: MockDataGenerationError) -> str:
    return f"{error.path}: {error.reason}"