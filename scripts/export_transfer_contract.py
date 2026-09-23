from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path
from typing import Any

import yaml
from pydantic import SecretStr

from src.transfer.app import create_app
from src.transfer.models import MappingDefinition, TransferRequest
from src.transfer.settings import TransferSettings

ROOT = Path(__file__).resolve().parents[1]
CANONICAL_OPENAPI = ROOT / "docs" / "api" / "ocr-transfer-openapi.yaml"
CANONICAL_OCR_SCHEMA = ROOT / "schemas" / "ocr-transfer-v1.json"
CANONICAL_MAPPING_SCHEMA = ROOT / "schemas" / "mapping-v1.json"
DEFAULT_OUTPUT_DIR = ROOT / "build" / "transfer-contract"
_HTTP_METHODS = {"get", "post", "put", "patch", "delete", "options", "head"}


def _contract_settings() -> TransferSettings:
    # Contract generation does not open the database or resolve credentials.
    return TransferSettings.model_validate(
        {
            "database_path": ":memory:",
            "jwt_issuer": "https://issuer.example.com",
            "jwt_audience": "ocr-transfer",
            "jwks_url": "https://issuer.example.com/.well-known/jwks.json",
            "allowed_hosts": ["localhost"],
            "worker_enabled": False,
            "max_attempts": 3,
            "max_payload_bytes": 10 * 1024 * 1024,
            "requests_per_minute": 120,
            "burst": 20,
            "credentials_json": SecretStr("{}"),
            "data_encryption_key_ref": SecretStr("key://contract-generation"),
        }
    )


def _generated_documents() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    app = create_app(_contract_settings())
    return (
        app.openapi(),
        TransferRequest.model_json_schema(),
        MappingDefinition.model_json_schema(),
    )


def _load_canonical() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    return (
        yaml.safe_load(CANONICAL_OPENAPI.read_text(encoding="utf-8")),
        json.loads(CANONICAL_OCR_SCHEMA.read_text(encoding="utf-8")),
        json.loads(CANONICAL_MAPPING_SCHEMA.read_text(encoding="utf-8")),
    )


def _operation_keys(paths: dict[str, Any]) -> dict[str, set[str]]:
    return {
        path: {method for method in operations if method in _HTTP_METHODS}
        for path, operations in paths.items()
    }


def _security(operation: dict[str, Any]) -> list[dict[str, list[str]]]:
    return operation.get("security", [])


def _success_statuses(operation: dict[str, Any]) -> set[str]:
    return {
        status
        for status in operation.get("responses", {})
        if str(status).startswith("2")
    }


def _operation_parameters(
    document: dict[str, Any], path: str, method: str
) -> tuple[Any, ...]:
    path_item = document.get("paths", {}).get(path, {})
    parameters = [
        *path_item.get("parameters", []),
        *path_item.get(method, {}).get("parameters", []),
    ]
    normalized = []
    for parameter in parameters:
        resolved = parameter
        if isinstance(parameter, dict) and isinstance(parameter.get("$ref"), str):
            resolved = _resolve_schema_reference(parameter["$ref"], document)
        if not isinstance(resolved, dict):
            normalized.append(("invalid", repr(parameter)))
            continue
        normalized.append(
            (
                resolved.get("name"),
                resolved.get("in"),
                bool(resolved.get("required", False)),
                _schema_semantics(resolved.get("schema", {}), document),
            )
        )
    return tuple(sorted(normalized, key=repr))


def _request_body_media_types(operation: dict[str, Any]) -> tuple[bool, tuple[str, ...]]:
    request_body = operation.get("requestBody") or {}
    content = request_body.get("content") or {}
    return bool(request_body.get("required", False)), tuple(sorted(content))


def _response_media_types(operation: dict[str, Any], status: str) -> tuple[str, ...]:
    response = operation.get("responses", {}).get(status, {})
    return tuple(sorted((response.get("content") or {}).keys()))


def _schema_shape(schema: dict[str, Any]) -> tuple[set[str], set[str], Any, Any]:
    return (
        set(schema.get("properties", {})),
        set(schema.get("required", [])),
        schema.get("type"),
        schema.get("additionalProperties"),
    )


def _schema_semantics(
    schema: Any,
    document: dict[str, Any],
    resolving: tuple[str, ...] = (),
) -> Any:
    if schema is True or schema == {}:
        return ("any",)
    if schema is False:
        return ("never",)
    if not isinstance(schema, dict):
        return ("invalid", repr(schema))

    reference = schema.get("$ref")
    if isinstance(reference, str):
        if reference in resolving:
            return ("recursive", reference)
        resolved = _resolve_schema_reference(reference, document)
        if resolved is not None:
            return _schema_semantics(resolved, document, (*resolving, reference))

    if "anyOf" in schema or "oneOf" in schema:
        keyword = "anyOf" if "anyOf" in schema else "oneOf"
        variants = schema.get(keyword, [])
        non_null = [
            variant
            for variant in variants
            if not (isinstance(variant, dict) and variant.get("type") == "null")
        ]
        if len(non_null) == 1 and len(non_null) < len(variants):
            return _schema_semantics(non_null[0], document, resolving)

    normalized: dict[str, Any] = {}
    schema_type = schema.get("type")
    if isinstance(schema_type, list):
        normalized_types = sorted(value for value in schema_type if value != "null")
        if normalized_types:
            normalized["type"] = (
                normalized_types[0]
                if len(normalized_types) == 1
                else tuple(normalized_types)
            )
    elif schema_type is not None and schema_type != "null":
        normalized["type"] = schema_type

    for key in (
        "const",
        "enum",
        "format",
        "maxLength",
        "maxItems",
        "maxProperties",
        "maximum",
        "exclusiveMaximum",
        "minLength",
        "minItems",
        "minProperties",
        "minimum",
        "exclusiveMinimum",
        "multipleOf",
        "pattern",
        "uniqueItems",
    ):
        if key in schema:
            value = schema[key]
            normalized[key] = tuple(value) if key == "enum" else value

    if "const" not in normalized and isinstance(normalized.get("enum"), tuple):
        enum_values = normalized["enum"]
        if len(enum_values) == 1:
            normalized.pop("enum")
            normalized["const"] = enum_values[0]

    properties = schema.get("properties")
    if isinstance(properties, dict):
        normalized["properties"] = tuple(
            (name, _schema_semantics(properties[name], document, resolving))
            for name in sorted(properties)
        )
    if "required" in schema:
        normalized["required"] = tuple(sorted(schema.get("required", [])))

    additional_properties = schema.get("additionalProperties")
    if additional_properties is not None:
        normalized["additionalProperties"] = (
            _schema_semantics(additional_properties, document, resolving)
            if isinstance(additional_properties, dict)
            else additional_properties
        )

    items = schema.get("items")
    prefix_items = schema.get("prefixItems")
    if items is None and isinstance(prefix_items, list):
        prefix_semantics = [
            _schema_semantics(item, document, resolving) for item in prefix_items
        ]
        if prefix_semantics and len(set(prefix_semantics)) == 1:
            items = prefix_items[0]
        else:
            normalized["prefixItems"] = tuple(prefix_semantics)
    if items is not None:
        normalized["items"] = _schema_semantics(items, document, resolving)

    for keyword in ("allOf", "anyOf", "oneOf"):
        if keyword in schema:
            normalized[keyword] = tuple(
                sorted(
                    (
                        _schema_semantics(item, document, resolving)
                        for item in schema[keyword]
                    ),
                    key=repr,
                )
            )
    for keyword in ("if", "then", "else", "not", "propertyNames"):
        if keyword in schema:
            normalized[keyword] = _schema_semantics(
                schema[keyword], document, resolving
            )

    return tuple(sorted(normalized.items(), key=lambda item: item[0]))


def _resolve_schema_reference(reference: str, document: dict[str, Any]) -> Any:
    if not reference.startswith("#/"):
        return None
    value: Any = document
    for token in reference[2:].split("/"):
        token = token.replace("~1", "/").replace("~0", "~")
        if not isinstance(value, dict) or token not in value:
            return None
        value = value[token]
    return value


def contract_differences(
    canonical_openapi: dict[str, Any],
    generated_openapi: dict[str, Any],
    canonical_ocr: dict[str, Any],
    generated_ocr: dict[str, Any],
    canonical_mapping: dict[str, Any],
    generated_mapping: dict[str, Any],
) -> list[str]:
    differences: list[str] = []
    canonical_paths = _operation_keys(canonical_openapi.get("paths", {}))
    generated_paths = _operation_keys(generated_openapi.get("paths", {}))
    if canonical_paths != generated_paths:
        differences.append(
            "path/method mismatch: "
            f"canonical={canonical_paths!r}, generated={generated_paths!r}"
        )

    for path, methods in canonical_paths.items():
        for method in methods:
            canonical_operation = canonical_openapi["paths"][path][method]
            generated_operation = generated_openapi.get("paths", {}).get(path, {}).get(method)
            if generated_operation is None:
                continue
            operation_id = canonical_operation.get("operationId")
            if operation_id != generated_operation.get("operationId"):
                differences.append(
                    f"operationId mismatch for {method.upper()} {path}: "
                    f"canonical={operation_id!r}, generated={generated_operation.get('operationId')!r}"
                )
            if _security(canonical_operation) != _security(generated_operation):
                differences.append(f"security mismatch for {method.upper()} {path}")
            canonical_parameters = _operation_parameters(canonical_openapi, path, method)
            generated_parameters = _operation_parameters(generated_openapi, path, method)
            if canonical_parameters != generated_parameters:
                differences.append(f"parameters mismatch for {method.upper()} {path}")

            canonical_body = _request_body_media_types(canonical_operation)
            generated_body = _request_body_media_types(generated_operation)
            if canonical_body != generated_body:
                differences.append(f"request body mismatch for {method.upper()} {path}")

            canonical_success = _success_statuses(canonical_operation)
            generated_success = _success_statuses(generated_operation)
            if canonical_success != generated_success:
                differences.append(
                    f"success response mismatch for {method.upper()} {path}: "
                    f"canonical={sorted(canonical_success)}, generated={sorted(generated_success)}"
                )
            for status in sorted(canonical_success & generated_success):
                if _response_media_types(canonical_operation, status) != _response_media_types(
                    generated_operation, status
                ):
                    differences.append(
                        f"response media type mismatch for {method.upper()} {path} {status}"
                    )

    for name, canonical_schema, generated_schema in (
        ("TransferRequest", canonical_ocr, generated_ocr),
        ("MappingDefinition", canonical_mapping, generated_mapping),
    ):
        canonical_shape = _schema_shape(canonical_schema)
        generated_shape = _schema_shape(generated_schema)
        if canonical_shape != generated_shape:
            differences.append(f"top-level schema shape mismatch for {name}")
        if _schema_semantics(canonical_schema, canonical_schema) != _schema_semantics(
            generated_schema, generated_schema
        ):
            differences.append(f"nested schema semantics mismatch for {name}")

    return differences


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, delete=False
    ) as temporary:
        temporary.write(content)
        temporary.flush()
        os.fsync(temporary.fileno())
        temporary_path = Path(temporary.name)
    os.replace(temporary_path, path)


def export_contract(output_dir: Path) -> None:
    generated_openapi, generated_ocr, generated_mapping = _generated_documents()
    _atomic_write(
        output_dir / "openapi.json",
        json.dumps(generated_openapi, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )
    _atomic_write(
        output_dir / "openapi.yaml",
        yaml.safe_dump(generated_openapi, allow_unicode=True, sort_keys=False),
    )
    _atomic_write(
        output_dir / "ocr-transfer-v1.generated.json",
        json.dumps(generated_ocr, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )
    _atomic_write(
        output_dir / "mapping-v1.generated.json",
        json.dumps(generated_mapping, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )


def check_contract() -> list[str]:
    canonical_openapi, canonical_ocr, canonical_mapping = _load_canonical()
    generated_openapi, generated_ocr, generated_mapping = _generated_documents()
    return contract_differences(
        canonical_openapi,
        generated_openapi,
        canonical_ocr,
        generated_ocr,
        canonical_mapping,
        generated_mapping,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Export and check the OCR transfer contract")
    parser.add_argument(
        "--check",
        action="store_true",
        help="compare the generated contract with canonical paths and schemas",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="directory for generated contract artifacts",
    )
    args = parser.parse_args()

    if args.check:
        differences = check_contract()
        if differences:
            for difference in differences:
                print(difference)
            return 1
        print("transfer contract check passed")
        return 0

    export_contract(args.output_dir)
    print(f"exported transfer contract to {args.output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
