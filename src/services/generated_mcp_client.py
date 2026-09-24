"""Client helpers embedded into deterministic MCP artifacts."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from mcp import ClientSession
from mcp.client.sse import sse_client
from mcp.client.streamable_http import streamablehttp_client


def parse_client_arguments(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generated MCP client")
    parser.add_argument(
        "--server-url",
        default=os.environ.get("MCP_SERVER_URL", "http://127.0.0.1:9000/mcp/"),
    )
    parser.add_argument(
        "--transport",
        choices=["streamable-http", "sse"],
        default=os.environ.get("MCP_TRANSPORT", "streamable-http"),
    )
    parser.add_argument("--tool")
    parser.add_argument("--arguments")
    parser.add_argument("--output-file")
    return parser.parse_args(argv)


def parse_tool_arguments(raw: str | None) -> dict[str, Any]:
    if raw is None:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("tool arguments must be valid JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("tool arguments must be a JSON object")
    return value


def serialize_call_result(result: Any) -> Any:
    if isinstance(result, (dict, list, str, int, float, bool)) or result is None:
        return result
    content = getattr(result, "content", [])
    values: list[Any] = []
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
    if url.rstrip("/").endswith(suffix.rstrip("/")):
        return url
    return url.rstrip("/") + suffix


def _configure_local_proxy_bypass(url: str) -> None:
    hostname = urlparse(url).hostname
    if hostname not in {"localhost", "127.0.0.1", "::1"}:
        return
    current = [value for value in os.environ.get("NO_PROXY", "").split(",") if value]
    for value in (hostname, "localhost", "127.0.0.1", "::1"):
        if value not in current:
            current.append(value)
    os.environ["NO_PROXY"] = ",".join(current)


@asynccontextmanager
async def _connect(url: str, transport: str) -> AsyncIterator[tuple[Any, Any, Any]]:
    _configure_local_proxy_bypass(url)
    endpoint = _endpoint(url, transport)
    if transport == "sse":
        async with sse_client(url=endpoint) as (read, write):
            yield read, write, None
    else:
        async with streamablehttp_client(url=endpoint) as (
            read,
            write,
            get_session_id,
        ):
            yield read, write, get_session_id


async def invoke_tool(
    server_url: str,
    transport: str,
    tool_name: str | None,
    arguments: dict[str, Any],
) -> Any:
    async with _connect(server_url, transport) as (read, write, _get_session_id):
        async with ClientSession(read, write) as session:
            await session.initialize()
            if tool_name is None:
                response = await session.list_tools()
                return [
                    {
                        "name": tool.name,
                        "description": getattr(tool, "description", None),
                    }
                    for tool in response.tools
                ]
            result = await session.call_tool(tool_name, arguments)
            if getattr(result, "isError", False):
                return {"error": serialize_call_result(result)}
            return serialize_call_result(result)


async def main(argv: Sequence[str] | None = None) -> int:
    args = parse_client_arguments(argv)
    try:
        result = await invoke_tool(
            args.server_url,
            args.transport,
            args.tool,
            parse_tool_arguments(args.arguments),
        )
    except ValueError as exc:
        result = {"error": {"kind": "configuration", "message": str(exc)}}
    except Exception:
        result = {
            "error": {
                "kind": "connection",
                "message": "MCP server connection failed",
            }
        }

    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    print(rendered)
    if args.output_file:
        Path(args.output_file).write_text(rendered + "\n", encoding="utf-8")
    return 1 if isinstance(result, dict) and "error" in result else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))