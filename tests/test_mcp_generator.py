import pytest

from src.services.mcp_generator import MCPServerGenerator


def test_validate_generated_server_rejects_fallback_code():
    code = MCPServerGenerator._generate_fallback_server(
        MCPServerGenerator.__new__(MCPServerGenerator),
        {"info": {"title": "Test"}, "paths": {}},
    )

    result = MCPServerGenerator._validate_generated_server(
        code, expected_operation_count=1
    )

    assert result["fallback"] is True
    assert result["valid"] is False
    assert any("fallback" in error.lower() for error in result["errors"])


@pytest.mark.asyncio
async def test_generate_server_code_uses_deterministic_artifacts(tmp_path):
    generator = MCPServerGenerator.__new__(MCPServerGenerator)
    generator.use_legacy_llm = False

    files, usage = await generator.generate_server_code(
        {},
        {
            "info": {"title": "Test API", "version": "1"},
            "paths": {"/records": {"get": {"operationId": "getRecords"}}},
        },
        tmp_path,
    )

    assert set(files) == {
        "server.py",
        "runtime.py",
        "client.py",
        "tool_spec.txt",
        "requirements.txt",
        "README.md",
    }
    assert usage["generation_status"] == "complete"
    assert usage["operation_count"] == 1
    assert usage["tool_count"] == 1
    assert usage["server_usage"]["source"] == "deterministic"