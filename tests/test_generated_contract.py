import ast
import importlib.util
from pathlib import Path

import yaml

from src.services.openapi_mcp_codegen import count_operations

ROOT = Path(__file__).resolve().parents[1]
SPEC_PATH = ROOT / "input_data/openapi-spec-1/openapi.yaml"
ARTIFACT_DIR = ROOT / "results/azure/openapi/mcpserver"


def test_generated_kintone_artifact_is_complete_and_compilable():
    spec = yaml.safe_load(SPEC_PATH.read_text(encoding="utf-8"))
    server_source = (ARTIFACT_DIR / "server.py").read_text(encoding="utf-8")
    tool_spec = (ARTIFACT_DIR / "tool_spec.txt").read_text(encoding="utf-8")

    expected_tool_count = count_operations(spec)
    assert expected_tool_count == 204
    assert server_source.count("@mcp.tool()") == expected_tool_count
    assert f"Total MCP Tools: {expected_tool_count}" in tool_spec
    assert "fallback" not in server_source.lower()
    assert "api_info" not in server_source

    for tool_name in (
        "postRecords",
        "getRecords",
        "getRecord",
        "putRecord",
        "deleteRecords",
    ):
        assert f"def {tool_name}(" in server_source

    runtime_path = ARTIFACT_DIR / "runtime.py"
    runtime_source = runtime_path.read_text(encoding="utf-8")
    assert "StructuredApiError" in runtime_source
    module_spec = importlib.util.spec_from_file_location(
        "generated_contract_runtime", runtime_path
    )
    assert module_spec is not None and module_spec.loader is not None
    runtime_module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(runtime_module)
    assert runtime_module.resolve_auth_headers(
        [{"apiToken": []}],
        {
            "apiToken": {
                "type": "apiKey",
                "in": "header",
                "name": "X-Cybozu-API-Token",
            }
        },
        {"KINTONE_API_TOKEN": "contract-token"},
    ) == {"X-Cybozu-API-Token": "contract-token"}

    for filename in ("server.py", "client.py", "runtime.py"):
        source = (ARTIFACT_DIR / filename).read_text(encoding="utf-8")
        ast.parse(source, filename=filename)
        compile(source, filename, "exec")