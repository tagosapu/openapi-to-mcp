import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from src.services.generated_mcp_client import (
    invoke_tool,
    parse_client_arguments,
    parse_tool_arguments,
)


def test_client_requires_arguments_when_a_tool_is_selected():
    args = parse_client_arguments(
        ["--tool", "postRecords", "--arguments", '{"body": {}}']
    )

    assert args.tool == "postRecords"
    assert json.loads(args.arguments) == {"body": {}}
    assert args.transport == "streamable-http"


def test_parse_tool_arguments_requires_a_json_object():
    with pytest.raises(ValueError, match="JSON object"):
        parse_tool_arguments("[1, 2]")


@asynccontextmanager
async def fake_streamable_transport(*_args, **_kwargs):
    yield object(), object(), lambda: "test-session"


@pytest.mark.asyncio
async def test_invoke_tool_calls_call_tool(monkeypatch):
    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def initialize(self):
            return None

        async def call_tool(self, name, arguments):
            assert name == "postRecords"
            assert arguments == {"body": {}}
            return SimpleNamespace(
                content=[
                    SimpleNamespace(text='{"ids":["2"],"revisions":["2"]}')
                ],
                isError=False,
            )

    monkeypatch.setattr(
        "src.services.generated_mcp_client.ClientSession",
        lambda *_args, **_kwargs: FakeSession(),
    )
    monkeypatch.setattr(
        "src.services.generated_mcp_client.streamablehttp_client",
        fake_streamable_transport,
    )

    assert await invoke_tool(
        "http://127.0.0.1:9001/mcp/",
        "streamable-http",
        "postRecords",
        {"body": {}},
    ) == {"ids": ["2"], "revisions": ["2"]}