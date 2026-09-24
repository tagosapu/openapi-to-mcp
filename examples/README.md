# Testing with Sample API

This README describes how to test the OpenAPI to MCP converter using the provided `sample_api.yaml` and a stub server that provides a backend for the API implementation. You can test with the generated MCP server and client code.

## Prerequisites

We assume you have followed the steps in the main project's README.md quickstart section and have:

- Installed the project dependencies
- Configured your API credentials
- Successfully generated MCP server code for the sample API

## Testing Commands

Run these commands in separate terminals:

### Terminal 1: Start the Stub Server
```bash
uv run python examples/stub_server.py
```

### Terminal 2: Set Auth Token and Run MCP Server
```bash
export MISSION_AUTH_TOKEN=secret-token
uv run python results/anthropic/sample_api/mcpserver/server.py
```

### Terminal 3: Run MCP Client
```bash
uv run python results/anthropic/sample_api/mcpserver/client.py
```

## What This Tests

- The stub server provides mock backend responses for the sample API
- The MCP server connects to the stub server and exposes API endpoints as MCP tools
- The MCP client tests all available tools and validates the integration

## Expected Results

You should see:
1. Stub server starts and serves mock API responses
2. MCP server starts and registers tools based on the sample API
3. MCP client connects, lists available tools, and tests each one

This validates the complete flow from OpenAPI specification to working MCP server integration.

## Testing Kintone Output with a Local Mock

`kintone_stub_server.py` provides an in-memory Kintone-compatible backend for local
testing. It supports API-token authentication, app metadata, record list/get/create/
update/delete operations, GET-over-POST for long queries, and request inspection.

Start it in one terminal:

```bash
uv run python examples/kintone_stub_server.py --port 9100
```

The default API token is `mock-token`. The mock backend is available at
`http://127.0.0.1:9100`, so a fully generated MCP server can be pointed at it with
`--base-url http://127.0.0.1:9100` or `API_BASE_URL`.

The current Kintone result may contain a fallback MCP server when LLM code
generation fails. In that case it exposes only `api_info` and cannot call the
Kintone mock. Confirm that the generated server contains endpoint tools before
running the end-to-end test. The mock request history can be inspected at
`GET http://127.0.0.1:9100/__mock/requests`.