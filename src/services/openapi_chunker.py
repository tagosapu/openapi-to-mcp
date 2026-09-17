"""Create token-bounded OpenAPI evaluation chunks without breaking references."""

import json
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Set, Tuple


HTTP_METHODS = frozenset(
    {
        "get",
        "put",
        "post",
        "delete",
        "options",
        "head",
        "patch",
        "trace",
    }
)


@dataclass(frozen=True)
class OpenAPIChunk:
    """A self-contained subset of an OpenAPI document for evaluation."""

    index: int
    spec: Dict[str, Any]
    paths: Tuple[str, ...]
    schema_names: Tuple[str, ...]
    estimated_tokens: int
    schema_only: bool = False


def estimate_tokens(value: Any) -> int:
    """Estimate tokens using the same conservative character ratio as the client."""
    if isinstance(value, str):
        character_count = len(value)
    else:
        character_count = len(
            json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        )
    return max(1, character_count // 4)


def build_openapi_chunks(
    spec: Dict[str, Any],
    max_prompt_tokens: int,
    prompt_overhead_tokens: int = 2_000,
) -> List[OpenAPIChunk]:
    """Build deterministic chunks for operation and unreferenced schema evaluation."""
    if max_prompt_tokens <= prompt_overhead_tokens:
        raise ValueError("max_prompt_tokens must exceed prompt_overhead_tokens")

    operation_units = _operation_units(spec)
    chunks: List[OpenAPIChunk] = []
    grouped_units: List[List[Tuple[str, Dict[str, Any]]]] = []
    current_units: List[Tuple[str, Dict[str, Any]]] = []

    for unit in operation_units:
        candidate_units = [*current_units, unit]
        candidate_spec = _build_operation_spec(spec, candidate_units)
        candidate_tokens = prompt_overhead_tokens + estimate_tokens(candidate_spec)
        if current_units and candidate_tokens > max_prompt_tokens:
            grouped_units.append(current_units)
            current_units = [unit]
        else:
            current_units = candidate_units

    if current_units:
        grouped_units.append(current_units)

    referenced_schema_names: Set[str] = set()
    for units in grouped_units:
        chunk_spec = _build_operation_spec(spec, units)
        schema_names = _schema_names(chunk_spec)
        referenced_schema_names.update(schema_names)
        chunks.append(
            OpenAPIChunk(
                index=len(chunks),
                spec=chunk_spec,
                paths=tuple(dict.fromkeys(path for path, _ in units)),
                schema_names=tuple(sorted(schema_names)),
                estimated_tokens=prompt_overhead_tokens + estimate_tokens(chunk_spec),
            )
        )

    all_schema_names = set(spec.get("components", {}).get("schemas", {}))
    unreferenced_schema_names = sorted(all_schema_names - referenced_schema_names)
    schema_units: List[Tuple[str, Dict[str, Any]]] = [
        (schema_name, {"$ref": f"#/components/schemas/{schema_name}"})
        for schema_name in unreferenced_schema_names
    ]
    current_schema_units: List[Tuple[str, Dict[str, Any]]] = []

    for unit in schema_units:
        candidate_units = [*current_schema_units, unit]
        candidate_spec = _build_schema_spec(spec, candidate_units)
        candidate_tokens = prompt_overhead_tokens + estimate_tokens(candidate_spec)
        if current_schema_units and candidate_tokens > max_prompt_tokens:
            chunks.append(
                _make_schema_chunk(
                    spec, current_schema_units, chunks, prompt_overhead_tokens
                )
            )
            current_schema_units = [unit]
        else:
            current_schema_units = candidate_units

    if current_schema_units:
        chunks.append(
            _make_schema_chunk(
                spec, current_schema_units, chunks, prompt_overhead_tokens
            )
        )

    return chunks


def _operation_units(spec: Dict[str, Any]) -> List[Tuple[str, Dict[str, Any]]]:
    units: List[Tuple[str, Dict[str, Any]]] = []
    for path, path_item in spec.get("paths", {}).items():
        if not isinstance(path_item, dict):
            units.append((path, path_item))
            continue

        common = {
            key: value
            for key, value in path_item.items()
            if key.lower() not in HTTP_METHODS
        }
        methods = [key for key in path_item if key.lower() in HTTP_METHODS]
        if not methods:
            units.append((path, dict(path_item)))
            continue

        for method in methods:
            operation = dict(common)
            operation[method] = path_item[method]
            units.append((path, operation))
    return units


def _build_operation_spec(
    spec: Dict[str, Any], units: Iterable[Tuple[str, Dict[str, Any]]]
) -> Dict[str, Any]:
    paths: Dict[str, Any] = {}
    for path, path_item in units:
        if path not in paths:
            paths[path] = path_item
        elif isinstance(paths[path], dict) and isinstance(path_item, dict):
            paths[path] = {**paths[path], **path_item}

    refs = _collect_component_refs(paths)
    return _build_subset_spec(spec, paths, refs)


def _build_schema_spec(
    spec: Dict[str, Any], units: Iterable[Tuple[str, Dict[str, Any]]]
) -> Dict[str, Any]:
    refs = {("schemas", schema_name) for schema_name, _ in units}
    return _build_subset_spec(spec, {}, refs)


def _build_subset_spec(
    spec: Dict[str, Any], paths: Dict[str, Any], refs: Set[Tuple[str, str]]
) -> Dict[str, Any]:
    subset = {
        key: value
        for key, value in spec.items()
        if key not in {"paths", "components"}
    }
    subset["paths"] = paths

    components = _component_closure(spec.get("components", {}), refs)
    if components:
        subset["components"] = components
    return subset


def _component_closure(
    components: Dict[str, Any], initial_refs: Set[Tuple[str, str]]
) -> Dict[str, Dict[str, Any]]:
    selected: Dict[str, Dict[str, Any]] = {}
    pending = list(initial_refs)

    security_schemes = components.get("securitySchemes", {})
    if security_schemes:
        selected["securitySchemes"] = dict(security_schemes)

    while pending:
        section, name = pending.pop()
        section_values = components.get(section, {})
        if not isinstance(section_values, dict) or name not in section_values:
            continue
        if name in selected.setdefault(section, {}):
            continue

        value = section_values[name]
        selected[section][name] = value
        pending.extend(_collect_component_refs(value))

    return selected


def _collect_component_refs(value: Any) -> Set[Tuple[str, str]]:
    refs: Set[Tuple[str, str]] = set()
    if isinstance(value, dict):
        for key, nested in value.items():
            if key == "$ref" and isinstance(nested, str):
                component_ref = _parse_component_ref(nested)
                if component_ref:
                    refs.add(component_ref)
            else:
                refs.update(_collect_component_refs(nested))
    elif isinstance(value, list):
        for nested in value:
            refs.update(_collect_component_refs(nested))
    return refs


def _parse_component_ref(ref: str) -> Tuple[str, str] | None:
    if not ref.startswith("#/components/"):
        return None
    parts = ref.split("/")
    if len(parts) < 4:
        return None
    section = parts[2].replace("~1", "/").replace("~0", "~")
    name = "/".join(parts[3:]).replace("~1", "/").replace("~0", "~")
    return section, name


def _schema_names(spec: Dict[str, Any]) -> Set[str]:
    return set(spec.get("components", {}).get("schemas", {}))


def _make_schema_chunk(
    spec: Dict[str, Any],
    units: List[Tuple[str, Dict[str, Any]]],
    existing_chunks: List[OpenAPIChunk],
    prompt_overhead_tokens: int,
) -> OpenAPIChunk:
    chunk_spec = _build_schema_spec(spec, units)
    return OpenAPIChunk(
        index=len(existing_chunks),
        spec=chunk_spec,
        paths=(),
        schema_names=tuple(sorted(_schema_names(chunk_spec))),
        estimated_tokens=prompt_overhead_tokens + estimate_tokens(chunk_spec),
        schema_only=True,
    )