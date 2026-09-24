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