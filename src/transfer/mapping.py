from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import date, datetime
import hashlib
import json
import math
from typing import Any

from jsonpointer import JsonPointerException, resolve_pointer

from .errors import MappingValidationError
from .models import MappingDefinition, MappingIssue, MappingPreview, MappingRule, OperationSelection, OutboundRequestParts, ReviewCorrection, TransferRequest


_MISSING = object()
_REVIEWABLE_STATUSES = {"missing", "ambiguous", "invalid"}
_FORBIDDEN_HEADERS = {"authorization", "content-length", "cookie", "host"}
_ALLOWED_TRANSFORMS = {
    "trim",
    "lower",
    "upper",
    "to_string",
    "to_integer",
    "to_number",
    "to_date",
    "to_datetime",
    "currency_amount",
}


@dataclass(slots=True)
class _BuildResult:
    path_params: dict[str, str]
    query_params: dict[str, str]
    headers: dict[str, str]
    json_body: dict[str, Any] | list[Any] | None
    issues: list[MappingIssue]


class MappingEngine:
    def apply_corrections(
        self,
        payload: TransferRequest,
        correction: ReviewCorrection | None,
    ) -> TransferRequest:
        if correction is None:
            return payload.model_copy(deep=True)

        document = payload.model_dump(mode="json")
        for pointer, value in correction.values.items():
            _set_pointer(document, pointer, deepcopy(value), create_missing=False)
        return TransferRequest.model_validate(document)

    def preview(
        self,
        payload: TransferRequest,
        mapping: MappingDefinition,
        operation: OperationSelection,
    ) -> MappingPreview:
        result = self._build(payload, mapping, operation)
        return MappingPreview(
            method=operation.method,
            path=operation.path,
            path_params=result.path_params,
            query_params=result.query_params,
            headers=result.headers,
            json_body=result.json_body,
            issues=result.issues,
            requires_review=bool(result.issues),
        )

    def apply(
        self,
        payload: TransferRequest,
        mapping: MappingDefinition,
        operation: OperationSelection,
    ) -> OutboundRequestParts:
        if payload.delivery.deduplication_key_path != mapping.deduplication_key_path:
            raise MappingValidationError("deduplication_key_path does not match mapping")

        result = self._build(payload, mapping, operation)
        if result.issues:
            raise MappingValidationError("mapping requires review before apply")

        idempotency_key = self._build_idempotency_key(payload)
        return OutboundRequestParts(
            operation_id=operation.operation_id,
            method=operation.method,
            path=operation.path,
            path_params=result.path_params,
            query_params=result.query_params,
            headers=result.headers,
            json_body=result.json_body,
            idempotency_key=idempotency_key,
        )

    def _build(
        self,
        payload: TransferRequest,
        mapping: MappingDefinition,
        operation: OperationSelection,
    ) -> _BuildResult:
        self._validate_operation(mapping, operation)
        self._validate_duplicate_targets(mapping.rules)

        document = payload.model_dump(mode="json")
        path_params: dict[str, str] = {}
        query_params: dict[str, str] = {}
        headers: dict[str, str] = {}
        json_body: dict[str, Any] | list[Any] | None = None
        issues: list[MappingIssue] = []

        for rule in mapping.rules:
            if rule.condition is not None and not self._condition_matches(document, rule):
                continue

            resolved = _resolve_pointer(document, rule.source)
            value = self._resolve_rule_value(document, rule, resolved)
            if value is _MISSING:
                continue
            if rule.required and _is_empty_value(value):
                raise MappingValidationError(f"required value is empty for rule {rule.rule_id}")

            value = self._apply_enum_map(rule, value)
            value = self._apply_transforms(document, rule, value)
            if rule.required and _is_empty_value(value):
                raise MappingValidationError(f"required value is empty for rule {rule.rule_id}")

            issue = self._build_issue(document, rule)
            if issue is not None:
                issues.append(issue)

            target = rule.target
            if target.location == "body":
                if json_body is None:
                    json_body = {} if target.pointer != "" else None
                if target.pointer == "":
                    json_body = deepcopy(value)
                else:
                    if json_body is None:
                        json_body = {}
                    _set_pointer(json_body, target.pointer, deepcopy(value), create_missing=True)
                continue

            rendered = _http_value(value, rule.rule_id)
            if target.location == "path":
                path_params[target.name or ""] = rendered
            elif target.location == "query":
                query_params[target.name or ""] = rendered
            else:
                header_name = target.name or ""
                _validate_header_name(header_name)
                headers[header_name] = rendered

        return _BuildResult(
            path_params=path_params,
            query_params=query_params,
            headers=headers,
            json_body=json_body,
            issues=issues,
        )

    def _validate_operation(
        self,
        mapping: MappingDefinition,
        operation: OperationSelection,
    ) -> None:
        if operation.name not in mapping.operations:
            raise MappingValidationError(
                f"operation {operation.name} is not allowed by mapping {mapping.mapping_id}"
            )

    def _validate_duplicate_targets(self, rules: list[MappingRule]) -> None:
        seen: set[str] = set()
        for rule in rules:
            target = rule.target
            target_key = (
                f"body:{target.pointer}" if target.location == "body" else f"{target.location}:{target.name}"
            )
            if target_key in seen:
                raise MappingValidationError(f"duplicate target detected: {target_key}")
            seen.add(target_key)

    def _resolve_rule_value(self, document: dict[str, Any], rule: MappingRule, value: Any) -> Any:
        if value is not _MISSING:
            return value
        if rule.default is not None:
            return deepcopy(rule.default)
        if rule.required or rule.on_missing == "error":
            raise MappingValidationError(f"source pointer missing for rule {rule.rule_id}")
        if rule.on_missing == "null":
            return None
        return _MISSING

    def _apply_enum_map(self, rule: MappingRule, value: Any) -> Any:
        if not rule.enum_map:
            return value
        if isinstance(value, str) and value in rule.enum_map:
            return rule.enum_map[value]
        value_key = str(value)
        return rule.enum_map.get(value_key, value)

    def _apply_transforms(self, document: dict[str, Any], rule: MappingRule, value: Any) -> Any:
        transformed = value
        for transform in rule.transforms:
            if transform.startswith("concat:"):
                transformed = self._apply_concat(document, transform, transformed, rule.rule_id)
                continue
            if transform not in _ALLOWED_TRANSFORMS:
                raise MappingValidationError(f"unsupported transform {transform} for rule {rule.rule_id}")
            transformed = _apply_transform(transform, transformed, rule.rule_id)
        return transformed

    def _apply_concat(
        self,
        document: dict[str, Any],
        transform: str,
        current: Any,
        rule_id: str,
    ) -> str:
        payload = transform.split(":", 1)[1]
        try:
            parts = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise MappingValidationError(f"invalid concat transform for rule {rule_id}") from exc
        if not isinstance(parts, list):
            raise MappingValidationError(f"invalid concat transform for rule {rule_id}")

        rendered: list[str] = []
        for item in parts:
            if item == "$":
                rendered.append(_stringify(current))
                continue
            if isinstance(item, str) and item.startswith("/"):
                resolved = _resolve_pointer(document, item)
                if resolved is _MISSING:
                    raise MappingValidationError(
                        f"source pointer missing inside concat for rule {rule_id}"
                    )
                rendered.append(_stringify(resolved))
                continue
            rendered.append(_stringify(item))
        return "".join(rendered)

    def _condition_matches(self, document: dict[str, Any], rule: MappingRule) -> bool:
        assert rule.condition is not None
        source_value = _resolve_pointer(document, rule.condition.source)
        if rule.condition.operator == "exists":
            return source_value is not _MISSING
        if source_value is _MISSING:
            return False
        if rule.condition.operator == "equals":
            return source_value == rule.condition.value
        if rule.condition.operator == "not_equals":
            return source_value != rule.condition.value
        if not isinstance(rule.condition.value, list | tuple | set):
            return False
        return source_value in rule.condition.value

    def _build_issue(self, document: dict[str, Any], rule: MappingRule) -> MappingIssue | None:
        field_pointer = _field_pointer_from_source(rule.source)
        if field_pointer is None:
            return None

        field = _resolve_pointer(document, field_pointer)
        if field is _MISSING or not isinstance(field, dict):
            return None

        confidence = field.get("confidence")
        status = field.get("status")
        threshold = 0.90 if rule.required else 0.70

        if isinstance(confidence, (int, float)) and confidence < threshold:
            message = (
                "required field confidence is below threshold"
                if rule.required
                else "optional field confidence is below threshold"
            )
            return MappingIssue(
                source_path=field_pointer,
                target_path=_target_path(rule),
                code="LOW_CONFIDENCE",
                message=message,
                retryable=False,
            )

        if status in _REVIEWABLE_STATUSES:
            return MappingIssue(
                source_path=field_pointer,
                target_path=_target_path(rule),
                code="LOW_CONFIDENCE",
                message="field status requires review",
                retryable=False,
            )
        return None

    def _build_idempotency_key(self, payload: TransferRequest) -> str:
        document = payload.model_dump(mode="json")
        deduplication_value = _resolve_pointer(document, payload.delivery.deduplication_key_path)
        if not _is_valid_deduplication_value(deduplication_value):
            raise MappingValidationError("DEDUPLICATION_KEY_INVALID")
        canonical = json.dumps(
            {
                "document_id": payload.document.document_id,
                "deduplication_value": deduplication_value,
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _apply_transform(transform: str, value: Any, rule_id: str) -> Any:
    if transform == "trim":
        if not isinstance(value, str):
            raise MappingValidationError(f"trim requires string input for rule {rule_id}")
        return value.strip()
    if transform == "lower":
        if not isinstance(value, str):
            raise MappingValidationError(f"lower requires string input for rule {rule_id}")
        return value.lower()
    if transform == "upper":
        if not isinstance(value, str):
            raise MappingValidationError(f"upper requires string input for rule {rule_id}")
        return value.upper()
    if transform == "to_string":
        return _stringify(value)
    if transform == "to_integer":
        return _to_integer(value, rule_id)
    if transform == "to_number":
        return _to_number(value, rule_id)
    if transform == "to_date":
        return _to_date(value, rule_id)
    if transform == "to_datetime":
        return _to_datetime(value, rule_id)
    return _currency_amount(value, rule_id)


def _to_integer(value: Any, rule_id: str) -> int:
    if isinstance(value, bool):
        raise MappingValidationError(f"to_integer rejects boolean input for rule {rule_id}")
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not value.is_integer():
            raise MappingValidationError(f"to_integer requires an integer-like value for rule {rule_id}")
        return int(value)
    if isinstance(value, str):
        normalized = value.strip().replace(",", "")
        return int(normalized)
    raise MappingValidationError(f"to_integer requires scalar input for rule {rule_id}")


def _to_number(value: Any, rule_id: str) -> int | float:
    if isinstance(value, bool):
        raise MappingValidationError(f"to_number rejects boolean input for rule {rule_id}")
    if isinstance(value, int | float):
        if isinstance(value, float) and not math.isfinite(value):
            raise MappingValidationError(f"to_number rejects non-finite values for rule {rule_id}")
        return value
    if isinstance(value, str):
        normalized = (
            value.strip()
            .replace(",", "")
            .replace("$", "")
            .replace("¥", "")
            .replace("€", "")
            .replace("£", "")
        )
        parsed = float(normalized)
        return int(parsed) if parsed.is_integer() else parsed
    raise MappingValidationError(f"to_number requires scalar input for rule {rule_id}")


def _to_date(value: Any, rule_id: str) -> str:
    if not isinstance(value, str):
        raise MappingValidationError(f"to_date requires string input for rule {rule_id}")
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        try:
            return _parse_datetime(value).date().isoformat()
        except ValueError as exc:
            raise MappingValidationError(f"to_date requires ISO date input for rule {rule_id}") from exc


def _to_datetime(value: Any, rule_id: str) -> str:
    if not isinstance(value, str):
        raise MappingValidationError(f"to_datetime requires string input for rule {rule_id}")
    try:
        return _parse_datetime(value).isoformat()
    except ValueError as exc:
        raise MappingValidationError(f"to_datetime requires ISO datetime input for rule {rule_id}") from exc


def _parse_datetime(value: str) -> datetime:
    normalized = value.replace("Z", "+00:00") if value.endswith("Z") else value
    return datetime.fromisoformat(normalized)


def _currency_amount(value: Any, rule_id: str) -> int | float:
    if isinstance(value, bool):
        raise MappingValidationError(f"currency_amount rejects boolean input for rule {rule_id}")
    if isinstance(value, int | float):
        return value
    if isinstance(value, dict):
        amount = value.get("value")
        if not isinstance(amount, (int, float)) or isinstance(amount, bool):
            raise MappingValidationError(
                f"currency_amount requires a numeric currency field for rule {rule_id}"
            )
        return amount
    raise MappingValidationError(f"currency_amount requires currency input for rule {rule_id}")


def _resolve_pointer(document: dict[str, Any] | list[Any], pointer: str) -> Any:
    try:
        return resolve_pointer(document, pointer)
    except JsonPointerException:
        return _MISSING


def _set_pointer(document: Any, pointer: str, value: Any, *, create_missing: bool) -> Any:
    if pointer == "":
        return value

    current = document
    tokens = _pointer_tokens(pointer)
    for index, token in enumerate(tokens[:-1]):
        next_token = tokens[index + 1]
        if isinstance(current, list):
            list_index = _list_index(token, pointer)
            if list_index >= len(current):
                if not create_missing:
                    raise MappingValidationError(f"invalid pointer {pointer}")
                while len(current) <= list_index:
                    current.append([] if _token_is_index(next_token) else {})
            current = current[list_index]
            continue

        if not isinstance(current, dict):
            raise MappingValidationError(f"invalid pointer {pointer}")
        if token not in current:
            if not create_missing:
                raise MappingValidationError(f"invalid pointer {pointer}")
            current[token] = [] if _token_is_index(next_token) else {}
        current = current[token]

    last = tokens[-1]
    if isinstance(current, list):
        list_index = _list_index(last, pointer)
        if list_index >= len(current):
            if not create_missing:
                raise MappingValidationError(f"invalid pointer {pointer}")
            while len(current) < list_index:
                current.append(None)
            current.append(value)
            return document
        current[list_index] = value
        return document

    if not isinstance(current, dict):
        raise MappingValidationError(f"invalid pointer {pointer}")
    current[last] = value
    return document


def _pointer_tokens(pointer: str) -> list[str]:
    if pointer == "":
        return []
    return [token.replace("~1", "/").replace("~0", "~") for token in pointer.lstrip("/").split("/")]


def _token_is_index(token: str) -> bool:
    return token.isdigit()


def _list_index(token: str, pointer: str) -> int:
    if not _token_is_index(token):
        raise MappingValidationError(f"invalid list index in pointer {pointer}")
    return int(token)


def _http_value(value: Any, rule_id: str) -> str:
    if value is None:
        raise MappingValidationError(f"null cannot be assigned to HTTP parameters for rule {rule_id}")
    if isinstance(value, (dict, list)):
        raise MappingValidationError(f"non-scalar value cannot be assigned to HTTP parameters for rule {rule_id}")
    return _stringify(value)


def _stringify(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, (dict, list)):
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return str(value)


def _validate_header_name(name: str) -> None:
    lowered = name.lower()
    if lowered in _FORBIDDEN_HEADERS or lowered.startswith("proxy-"):
        raise MappingValidationError(f"forbidden header: {name}")


def _field_pointer_from_source(source: str) -> str | None:
    tokens = _pointer_tokens(source)
    if len(tokens) >= 3 and tokens[0] == "ocr" and tokens[1] == "fields":
        return "/" + "/".join(tokens[:3])
    if len(tokens) >= 5 and tokens[0] == "ocr" and tokens[1] == "line_items":
        return "/" + "/".join(tokens[:5])
    return None


def _target_path(rule: MappingRule) -> str:
    target = rule.target
    if target.location == "body":
        return f"/body{target.pointer}"
    return f"/{target.location}/{target.name}"


def _is_empty_value(value: Any) -> bool:
    return value is None or value == "" or value == [] or value == {}


def _is_valid_deduplication_value(value: Any) -> bool:
    if value is _MISSING or value is None or value == "":
        return False
    if isinstance(value, bool):
        return False
    if isinstance(value, list | dict):
        return False
    if isinstance(value, float) and not math.isfinite(value):
        return False
    return isinstance(value, (str, int, float))