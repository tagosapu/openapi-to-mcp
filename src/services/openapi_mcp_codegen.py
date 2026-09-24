"""Deterministic OpenAPI operation normalization and MCP source generation."""

from __future__ import annotations

import json
import keyword
import re
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal

HTTP_METHODS = ("delete", "get", "head", "options", "patch", "post", "put", "trace")
ParameterLocation = Literal["path", "query", "header", "cookie"]


@dataclass(frozen=True)
class ParameterMetadata:
    wire_name: str
    python_name: str
    location: ParameterLocation
    required: bool
    schema: Mapping[str, Any]
    description: str | None = None


@dataclass(frozen=True)
class RequestBodyMetadata:
    required: bool
    content_type: str
    schema: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class OperationMetadata:
    operation_id: str
    tool_name: str
    method: str
    path: str
    parameters: tuple[ParameterMetadata, ...]
    request_body: RequestBodyMetadata | None
    security: tuple[dict[str, tuple[str, ...]], ...]
    security_schemes: Mapping[str, Mapping[str, Any]]

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-like mapping suitable for the generated runtime."""
        return {
            "operation_id": self.operation_id,
            "tool_name": self.tool_name,
            "method": self.method,
            "path": self.path,
            "parameters": [asdict(parameter) for parameter in self.parameters],
            "request_body": (
                asdict(self.request_body) if self.request_body is not None else None
            ),
            "security": [
                {name: list(scopes) for name, scopes in requirement.items()}
                for requirement in self.security
            ],
            "security_schemes": {
                name: dict(scheme) for name, scheme in self.security_schemes.items()
            },
        }


def count_operations(openapi_spec: Mapping[str, Any]) -> int:
    """Count supported HTTP operations under ``paths``."""
    paths = openapi_spec.get("paths", {})
    if not isinstance(paths, Mapping):
        return 0
    return sum(
        1
        for path_item in paths.values()
        if isinstance(path_item, Mapping)
        for method in path_item
        if isinstance(method, str) and method.lower() in HTTP_METHODS
    )


def make_tool_name(
    operation_id: str | None,
    method: str,
    path: str,
    used_names: set[str],
) -> str:
    """Create a valid, unique Python identifier for an OpenAPI operation."""
    if operation_id:
        base_name = _operation_identifier(operation_id)
    else:
        path_name = _python_identifier(path.replace("{", "").replace("}", ""))
        base_name = _python_identifier(f"{method.lower()}_{path_name}")

    candidate = base_name or "operation"
    suffix = 2
    while candidate in used_names:
        candidate = f"{base_name}_{suffix}"
        suffix += 1
    used_names.add(candidate)
    return candidate


def collect_operations(openapi_spec: Mapping[str, Any]) -> list[OperationMetadata]:
    """Normalize every OpenAPI path operation in stable order."""
    paths = openapi_spec.get("paths", {})
    if not isinstance(paths, Mapping):
        return []

    components = openapi_spec.get("components", {})
    security_schemes = _resolve_security_schemes(components, openapi_spec)
    root_security = _normalize_security(openapi_spec.get("security", []))
    operations: list[OperationMetadata] = []
    used_names: set[str] = set()

    for path, path_item in paths.items():
        if not isinstance(path, str) or not isinstance(path_item, Mapping):
            continue
        path_parameters = path_item.get("parameters", [])
        for method in HTTP_METHODS:
            operation = path_item.get(method)
            if not isinstance(operation, Mapping):
                continue

            tool_name = make_tool_name(
                operation.get("operationId"), method, path, used_names
            )
            operation_id = str(operation.get("operationId") or tool_name)
            parameters = _collect_parameters(
                path_parameters, operation.get("parameters", []), openapi_spec
            )
            request_body = _collect_request_body(
                operation.get("requestBody"), openapi_spec
            )
            security = (
                _normalize_security(operation["security"])
                if "security" in operation
                else root_security
            )
            operations.append(
                OperationMetadata(
                    operation_id=operation_id,
                    tool_name=tool_name,
                    method=method.upper(),
                    path=path,
                    parameters=parameters,
                    request_body=request_body,
                    security=security,
                    security_schemes=security_schemes,
                )
            )

    return operations


def _collect_parameters(
    path_parameters: Any,
    operation_parameters: Any,
    openapi_spec: Mapping[str, Any],
) -> tuple[ParameterMetadata, ...]:
    merged: dict[tuple[str, str], Mapping[str, Any]] = {}
    for raw_parameter in _as_sequence(path_parameters) + _as_sequence(
        operation_parameters
    ):
        parameter = _resolve_value(raw_parameter, openapi_spec)
        if not isinstance(parameter, Mapping):
            continue
        location = parameter.get("in")
        wire_name = parameter.get("name")
        if location not in {"path", "query", "header", "cookie"} or not wire_name:
            continue
        merged[(str(location), str(wire_name))] = parameter

    used_python_names: set[str] = set()
    result: list[ParameterMetadata] = []
    for parameter in merged.values():
        location = str(parameter["in"])
        wire_name = str(parameter["name"])
        python_name = _unique_parameter_name(
            _python_identifier(wire_name) or "value", location, used_python_names
        )
        schema = parameter.get("schema")
        if schema is None and isinstance(parameter.get("content"), Mapping):
            content = parameter["content"]
            first_content = next(iter(content.values()), {})
            schema = first_content.get("schema", {}) if isinstance(first_content, Mapping) else {}
        resolved_schema = _resolve_value(schema or {}, openapi_spec)
        result.append(
            ParameterMetadata(
                wire_name=wire_name,
                python_name=python_name,
                location=location,  # type: ignore[arg-type]
                required=bool(parameter.get("required", location == "path")),
                schema=(
                    dict(resolved_schema)
                    if isinstance(resolved_schema, Mapping)
                    else {}
                ),
                description=(
                    str(parameter["description"])
                    if parameter.get("description") is not None
                    else None
                ),
            )
        )
    return tuple(result)


def _collect_request_body(
    raw_request_body: Any, openapi_spec: Mapping[str, Any]
) -> RequestBodyMetadata | None:
    request_body = _resolve_value(raw_request_body, openapi_spec)
    if not isinstance(request_body, Mapping):
        return None
    content = request_body.get("content", {})
    if not isinstance(content, Mapping) or not content:
        return RequestBodyMetadata(
            required=bool(request_body.get("required", False)),
            content_type="application/json",
            schema=None,
        )

    content_type = (
        "application/json" if "application/json" in content else next(iter(content))
    )
    media_type = content.get(content_type, {})
    schema = media_type.get("schema") if isinstance(media_type, Mapping) else None
    resolved_schema = _resolve_value(schema, openapi_spec)
    return RequestBodyMetadata(
        required=bool(request_body.get("required", False)),
        content_type=str(content_type),
        schema=(
            dict(resolved_schema) if isinstance(resolved_schema, Mapping) else None
        ),
    )


def _resolve_security_schemes(
    components: Any, openapi_spec: Mapping[str, Any]
) -> dict[str, Mapping[str, Any]]:
    if not isinstance(components, Mapping):
        return {}
    raw_schemes = components.get("securitySchemes", {})
    if not isinstance(raw_schemes, Mapping):
        return {}
    return {
        str(name): dict(resolved)
        for name, value in raw_schemes.items()
        if isinstance(resolved := _resolve_value(value, openapi_spec), Mapping)
    }


def _normalize_security(raw_security: Any) -> tuple[dict[str, tuple[str, ...]], ...]:
    if not isinstance(raw_security, Sequence) or isinstance(raw_security, (str, bytes)):
        return ()
    normalized: list[dict[str, tuple[str, ...]]] = []
    for requirement in raw_security:
        if not isinstance(requirement, Mapping):
            continue
        normalized.append(
            {
                str(name): tuple(str(scope) for scope in scopes)
                if isinstance(scopes, Sequence) and not isinstance(scopes, (str, bytes))
                else ()
                for name, scopes in requirement.items()
            }
        )
    return tuple(normalized)


def _resolve_value(value: Any, openapi_spec: Mapping[str, Any]) -> Any:
    return _resolve_value_with_stack(value, openapi_spec, set())


def _resolve_value_with_stack(
    value: Any, openapi_spec: Mapping[str, Any], resolving: set[str]
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
            resolved = _resolve_value_with_stack(
                target, openapi_spec, resolving | {reference}
            )
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


def _as_sequence(value: Any) -> list[Any]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return list(value)
    return []


def _python_identifier(value: str) -> str:
    snake = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", value)
    snake = re.sub(r"[^0-9a-zA-Z_]+", "_", snake).strip("_").lower()
    snake = re.sub(r"_+", "_", snake)
    if snake and snake[0].isdigit():
        snake = f"value_{snake}"
    if keyword.iskeyword(snake):
        snake = f"{snake}_value"
    return snake


def _operation_identifier(value: str) -> str:
    identifier = re.sub(r"[^0-9a-zA-Z_]+", "_", value).strip("_")
    identifier = re.sub(r"_+", "_", identifier)
    if identifier and identifier[0].isdigit():
        identifier = f"operation_{identifier}"
    if keyword.iskeyword(identifier):
        identifier = f"{identifier}_value"
    return identifier


def _unique_parameter_name(
    base_name: str, location: str, used_names: set[str]
) -> str:
    candidate = base_name
    if candidate in used_names:
        suffix = "value" if location == "cookie" else location
        candidate = f"{base_name}_{suffix}"
    counter = 2
    while candidate in used_names:
        candidate = f"{base_name}_{counter}"
        counter += 1
    used_names.add(candidate)
    return candidate


def render_server_source(api_title: str, operations: Sequence[OperationMetadata]) -> str:
    """Render a deterministic FastMCP server for normalized operations."""
    title_literal = json.dumps(api_title)
    lines = [
        '"""Deterministic MCP server generated from an OpenAPI specification."""',
        "",
        "import argparse",
        "import json",
        "import os",
        "from typing import Annotated, Any",
        "",
        "from mcp.server.fastmcp import FastMCP",
        "from pydantic import Field",
        "",
        "from runtime import GeneratedMcpRuntime, StructuredApiError",
        "",
        "",
        "def parse_arguments():",
        f"    parser = argparse.ArgumentParser(description={title_literal} + \" MCP server\")",
        '    parser.add_argument("--port", type=int, default=int(os.environ.get("MCP_SERVER_LISTEN_PORT", "9000")))',
        '    parser.add_argument("--transport", default=os.environ.get("MCP_TRANSPORT", "streamable-http"))',
        '    parser.add_argument("--base-url", default=os.environ.get("API_BASE_URL", ""))',
        '    parser.add_argument("--timeout", type=float, default=float(os.environ.get("API_TIMEOUT_SECONDS", "30")))',
        "    return parser.parse_args()",
        "",
        f"mcp = FastMCP({title_literal}, host=\"0.0.0.0\", port=int(os.environ.get(\"MCP_SERVER_LISTEN_PORT\", \"9000\")))",
        'runtime = GeneratedMcpRuntime(os.environ.get("API_BASE_URL", ""), timeout_seconds=float(os.environ.get("API_TIMEOUT_SECONDS", "30")))',
        "",
        "",
        "def _invoke(operation: dict[str, Any], arguments: dict[str, Any]) -> Any:",
        "    try:",
        "        return runtime.request(operation, arguments)",
        "    except StructuredApiError as exc:",
        "        return exc.as_dict()",
        "",
    ]

    for operation in operations:
        constant_name = _operation_constant_name(operation.tool_name)
        metadata = repr(operation.as_dict())
        lines.extend(
            [
                f"{constant_name} = {metadata}",
                "",
            ]
        )
        lines.extend(_render_tool_function(operation, constant_name))
        lines.append("")

    tool_names = json.dumps([operation.tool_name for operation in operations])
    lines.extend(
        [
            "@mcp.prompt()",
            "def system_prompt_for_agent() -> str:",
            f"    return {json.dumps(f'You are using the {api_title} MCP server.')}",
            "",
            '@mcp.resource("config://api")',
            "def api_metadata() -> str:",
            f"    return json.dumps({{'title': {title_literal}, 'tools': {tool_names}}})",
            "",
            "",
            "def main() -> None:",
            "    global runtime",
            "    args = parse_arguments()",
            "    mcp.settings.port = args.port",
            "    mcp.settings.host = \"0.0.0.0\"",
            "    runtime = GeneratedMcpRuntime(args.base_url, timeout_seconds=args.timeout)",
            "    mcp.run(transport=args.transport)",
            "",
            'if __name__ == "__main__":',
            "    main()",
            "",
        ]
    )
    return "\n".join(lines)


def render_client_source(api_title: str) -> str:
    """Render the generated MCP client source."""
    import inspect

    from . import generated_mcp_client

    source = inspect.getsource(generated_mcp_client)
    return source.replace(
        'description="Generated MCP client"',
        f"description={json.dumps(api_title)} + \" MCP client\"",
        1,
    )

    title_literal = json.dumps(api_title)
    return f'''"""MCP client generated for {api_title}."""

import argparse
import asyncio
import json
import os
from contextlib import asynccontextmanager
from typing import Any, Sequence

from mcp import ClientSession
from mcp.client.sse import sse_client
from mcp.client.streamable_http import streamablehttp_client


def parse_client_arguments(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description={title_literal} + " MCP client")
    parser.add_argument("--server-url", default=os.environ.get("MCP_SERVER_URL", "http://127.0.0.1:9000/mcp/"))
    parser.add_argument("--transport", choices=["streamable-http", "sse"], default=os.environ.get("MCP_TRANSPORT", "streamable-http"))
    parser.add_argument("--tool")
    parser.add_argument("--arguments")
    parser.add_argument("--output-file")
    return parser.parse_args(argv)


def parse_tool_arguments(raw: str | None) -> dict[str, Any]:
    if raw is None:
        return {{}}
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("tool arguments must be a JSON object")
    return value


def serialize_call_result(result: Any) -> Any:
    if isinstance(result, (dict, list, str, int, float, bool)) or result is None:
        return result
    content = getattr(result, "content", [])
    values = []
    for item in content:
        text = getattr(item, "text", None)
        if text is None:
            continue
        try:
            values.append(json.loads(text))
        except json.JSONDecodeError:
            values.append(text)
    if len(values) == 1:
        return values[0]
    return values


def _endpoint(url: str, transport: str) -> str:
    suffix = "/sse/" if transport == "sse" else "/mcp/"
    return url if url.rstrip("/").endswith(suffix.rstrip("/")) else url.rstrip("/") + suffix


@asynccontextmanager
async def _connect(url: str, transport: str):
    endpoint = _endpoint(url, transport)
    if transport == "sse":
        async with sse_client(url=endpoint) as streams:
            yield streams
    else:
        async with streamablehttp_client(url=endpoint) as streams:
            yield streams


async def invoke_tool(server_url: str, transport: str, tool_name: str | None, arguments: dict[str, Any]) -> Any:
    async with _connect(server_url, transport) as (read, write, _get_session_id):
        async with ClientSession(read, write) as session:
            await session.initialize()
            if tool_name is None:
                response = await session.list_tools()
                return [{{"name": tool.name, "description": getattr(tool, "description", None)}} for tool in response.tools]
            result = await session.call_tool(tool_name, arguments)
            if getattr(result, "isError", False):
                return {{"error": serialize_call_result(result)}}
            return serialize_call_result(result)


async def main() -> int:
    args = parse_client_arguments()
    result = await invoke_tool(args.server_url, args.transport, args.tool, parse_tool_arguments(args.arguments))
    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    print(rendered)
    if args.output_file:
        with open(args.output_file, "w", encoding="utf-8") as output:
            output.write(rendered + "\\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
'''


def render_runtime_source() -> str:
    """Render the generated HTTP runtime source."""
    import inspect

    from . import generated_mcp_runtime

    return inspect.getsource(generated_mcp_runtime)


def write_generated_artifacts(
    openapi_spec: Mapping[str, Any], output_dir: Path
) -> dict[str, Path]:
    """Write all deterministic generated artifacts."""
    output_dir.mkdir(parents=True, exist_ok=True)
    operations = collect_operations(openapi_spec)
    api_title = str(openapi_spec.get("info", {}).get("title", "OpenAPI API"))
    files = {
        "server.py": render_server_source(api_title, operations),
        "runtime.py": render_runtime_source(),
        "client.py": render_client_source(api_title),
        "tool_spec.txt": _render_tool_spec(api_title, operations),
        "requirements.txt": _render_requirements(),
        "README.md": _render_readme(api_title, operations),
    }
    paths: dict[str, Path] = {}
    for filename, content in files.items():
        path = output_dir / filename
        path.write_text(content, encoding="utf-8")
        paths[filename] = path
    return paths


def _render_tool_function(
    operation: OperationMetadata, constant_name: str
) -> list[str]:
    body_name = _body_parameter_name(operation)
    signature_parts = [
        _render_parameter_signature(parameter) for parameter in operation.parameters
    ]
    if operation.request_body is not None:
        schema = operation.request_body.schema or {}
        annotation = _schema_annotation(schema, None)
        default = "" if operation.request_body.required else " = None"
        signature_parts.append(f"{body_name}: {annotation}{default}")
    signature = ", ".join(signature_parts)
    if signature:
        signature = "*, " + signature

    lines = [
        "@mcp.tool()",
        f"def {operation.tool_name}({signature}) -> dict[str, Any]:",
        f"    \"\"\"Call {operation.method} {operation.path}.\"\"\"",
        "    arguments: dict[str, Any] = {",
    ]
    for location in ("path", "query", "header", "cookie"):
        wire_location = {
            "path": "path",
            "query": "query",
            "header": "headers",
            "cookie": "cookies",
        }[location]
        lines.append(f'        "{wire_location}": {{')
        for parameter in operation.parameters:
            if parameter.location == location:
                lines.append(
                    f'            {json.dumps(parameter.wire_name)}: {parameter.python_name},'
                )
        lines.append("        },")
    body_value = body_name if operation.request_body is not None else "None"
    lines.extend(
        [
            f'        "body": {body_value},',
            "    }",
            f"    return _invoke({constant_name}, arguments)",
        ]
    )
    return lines


def _render_parameter_signature(parameter: ParameterMetadata) -> str:
    annotation = _schema_annotation(parameter.schema, parameter.description)
    if parameter.required:
        return f"{parameter.python_name}: {annotation}"
    if "| None" not in annotation:
        annotation = f"{annotation} | None"
    default = parameter.schema.get("default") if "default" in parameter.schema else None
    return f"{parameter.python_name}: {annotation} = {_literal(default)}"


def _schema_annotation(schema: Mapping[str, Any], description: str | None) -> str:
    annotation = _schema_type(schema)
    if description:
        return f"Annotated[{annotation}, Field(description={json.dumps(description)})]"
    return annotation


def _schema_type(schema: Mapping[str, Any]) -> str:
    schema_type = schema.get("type")
    if schema_type == "array":
        items = schema.get("items", {})
        item_type = _schema_type(items) if isinstance(items, Mapping) else "Any"
        return f"list[{item_type}]"
    return {
        "integer": "int",
        "number": "float",
        "boolean": "bool",
        "string": "str",
        "object": "dict[str, Any]",
    }.get(schema_type, "Any")


def _body_parameter_name(operation: OperationMetadata) -> str:
    used_names = {parameter.python_name for parameter in operation.parameters}
    candidate = "body"
    suffix = 2
    while candidate in used_names:
        candidate = f"body_{suffix}"
        suffix += 1
    return candidate


def _operation_constant_name(tool_name: str) -> str:
    return "_OPERATION_" + re.sub(r"[^0-9A-Za-z]+", "_", tool_name).upper()


def _literal(value: Any) -> str:
    try:
        return repr(value)
    except TypeError:
        return "None"


def _render_tool_spec(
    api_title: str, operations: Sequence[OperationMetadata]
) -> str:
    lines = [f"# MCP Tool Specifications for {api_title}", ""]
    for operation in operations:
        lines.extend(
            [
                f"TOOL: {operation.tool_name}",
                f"METHOD: {operation.method}",
                f"PATH: {operation.path}",
                "",
            ]
        )
    lines.append(f"Total MCP Tools: {len(operations)}")
    return "\n".join(lines) + "\n"


def _render_requirements() -> str:
    return """fastmcp>=2.0.0
httpx>=0.25.0
pydantic>=2.5.0
PyYAML>=6.0.0
mcp>=1.0.0
"""


def _render_readme(api_title: str, operations: Sequence[OperationMetadata]) -> str:
    return f"""# {api_title} MCP Server

OpenAPI仕様から決定的に生成されたMCPサーバーです。

- ツール数: {len(operations)}
- デフォルトtransport: `streamable-http`
- 転記先API endpoint: `API_BASE_URL`
- MCP endpoint: `http://127.0.0.1:9001/mcp/`

OpenAPI operation数とツール数が一致し、`server.py` にfallback markerがなければ
完全生成物です。fallback生成物は `api_info` だけを公開し、API連携には使用できません。

## ローカルKintoneモック

リポジトリのルートから、3つの端末で次のコマンドを実行します。

### 端末1: モックAPI

```bash
uv run python examples/kintone_stub_server.py --port 9100 --token mock-token
```

### 端末2: 生成MCPサーバー

```bash
API_BASE_URL=http://127.0.0.1:9100 \\
KINTONE_API_TOKEN=mock-token \\
uv run python results/azure/openapi/mcpserver/server.py \\
    --port 9001 --transport streamable-http
```

### 端末3: 生成MCPクライアント

クライアントの接続先はサーバーの `/mcp/` endpointです。OCR fixtureは
`examples/ocr/kintone-transfer.json` にあり、正常データ3件と異常データ3件を確認できます。
次のコマンドは正常データの1件目をKintoneの `postRecords` toolへ送信する例です。

```bash
uv run python results/azure/openapi/mcpserver/client.py \\
    --server-url http://127.0.0.1:9001/mcp/ \\
    --tool postRecords \\
    --arguments '{{"body":{{"app":1,"records":[{{"document_id":{{"type":"SINGLE_LINE_TEXT","value":"ocr-e2e-001"}},"text":{{"type":"MULTI_LINE_TEXT","value":"OCR transfer text\\nInvoice total: 12800"}},"status":{{"type":"DROP_DOWN","value":"registered"}},"confidence":{{"type":"NUMBER","value":0.98}},"source_file":{{"type":"SINGLE_LINE_TEXT","value":"invoice-001.png"}}}}]}}}}'
```

`--tool` を省略するとツール一覧を取得します。別の明示的なAPI endpointへ接続する
場合は、`API_BASE_URL` または `--base-url` を設定してください。異常データの期待結果と
正常系の実際の出力は `docs/KINTONE_MCP_MOCK_RESULTS.md` を参照してください。
"""