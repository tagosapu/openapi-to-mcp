import ast
from pathlib import Path

import yaml

from src.services.openapi_mcp_codegen import (
    collect_operations,
    count_operations,
    make_tool_name,
    render_server_source,
    write_generated_artifacts,
)

MINIMAL_SPEC = {
    "openapi": "3.0.3",
    "info": {"title": "Test API", "version": "1"},
    "paths": {
        "/records/{recordId}": {
            "get": {
                "operationId": "getRecord",
                "parameters": [
                    {
                        "name": "recordId",
                        "in": "path",
                        "required": True,
                        "schema": {"type": "integer"},
                    },
                    {
                        "name": "fields",
                        "in": "query",
                        "required": False,
                        "schema": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                    },
                ],
            },
            "post": {
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {"schema": {"type": "object"}}
                    },
                }
            },
        }
    },
}


def test_collect_operations_extracts_path_query_and_body_metadata():
    operations = collect_operations(MINIMAL_SPEC)

    assert len(operations) == 2
    assert operations[0].tool_name == "getRecord"
    assert operations[0].parameters[0].python_name == "record_id"
    assert operations[0].parameters[1].location == "query"
    assert operations[1].request_body.required is True


def test_tool_name_is_unique_when_operation_ids_are_missing_or_duplicated():
    names = {
        make_tool_name("same", "get", "/a", set()),
        make_tool_name("same", "get", "/b", {"same"}),
    }

    assert names == {"same", "same_2"}


def test_real_kintone_spec_has_one_operation_metadata_item_per_operation():
    spec = yaml.safe_load(
        Path("input_data/openapi-spec-1/openapi.yaml").read_text(encoding="utf-8")
    )

    operations = collect_operations(spec)

    assert count_operations(spec) == 204
    assert len(operations) == 204
    assert len({operation.tool_name for operation in operations}) == 204
    assert {
        "getRecord",
        "postRecord",
        "putRecord",
        "getRecords",
        "postRecords",
        "putRecords",
        "deleteRecords",
    } <= {operation.tool_name for operation in operations}


def test_render_server_contains_one_tool_per_operation():
    source = render_server_source("Test API", collect_operations(MINIMAL_SPEC))

    assert "from mcp.server.fastmcp import FastMCP" in source
    assert source.count("@mcp.tool()") == 2
    assert "def getRecord(" in source
    assert "def post_records_record_id(" in source
    assert "mcp.settings.port = args.port" in source
    assert "Fallback implementation" not in source


def test_write_generated_artifacts_creates_standalone_sources(tmp_path):
    paths = write_generated_artifacts(MINIMAL_SPEC, tmp_path)

    assert set(paths) == {
        "server.py",
        "runtime.py",
        "client.py",
        "tool_spec.txt",
        "requirements.txt",
        "README.md",
    }
    for filename in ("server.py", "runtime.py", "client.py"):
        ast.parse(paths[filename].read_text(encoding="utf-8"))
    assert "requests" not in paths["requirements.txt"].read_text(encoding="utf-8")
    assert "Fallback implementation" not in paths["server.py"].read_text(
        encoding="utf-8"
    )