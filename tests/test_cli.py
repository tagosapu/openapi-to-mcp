from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src import cli
from src.services.mcp_generator import MCPGenerationError


@pytest.mark.asyncio
async def test_handle_mcp_generation_raises_for_failed_generation(monkeypatch):
    args = SimpleNamespace(eval_only=False)
    monkeypatch.setattr(cli, "_check_mcp_generation_criteria", lambda _evaluation: True)
    monkeypatch.setattr(
        cli,
        "_generate_mcp_server",
        AsyncMock(
            return_value=(
                None,
                {
                    "generation_status": "failed",
                    "validation_errors": [
                        "generated code is a fallback implementation"
                    ],
                },
            )
        ),
    )
    monkeypatch.setattr(cli, "_update_evaluation_with_mcp_usage", lambda *_args: None)

    with pytest.raises(MCPGenerationError, match="MCP generation failed"):
        await cli._handle_mcp_generation(
            args, MagicMock(), "{}", MagicMock(), MagicMock()
        )


@pytest.mark.asyncio
async def test_handle_mcp_generation_raises_for_invalid_openapi(monkeypatch):
    args = SimpleNamespace(eval_only=False)
    monkeypatch.setattr(cli, "_check_mcp_generation_criteria", lambda _evaluation: True)

    with pytest.raises(MCPGenerationError, match="failed to parse"):
        await cli._handle_mcp_generation(
            args, MagicMock(), "not: [valid", MagicMock(), MagicMock()
        )


@pytest.mark.asyncio
async def test_generate_mcp_server_preserves_validation_metadata(monkeypatch, tmp_path):
    async def fail_generation(_openapi_spec, _output_dir):
        raise MCPGenerationError(
            "generated code is a fallback implementation",
            validation={
                "fallback": True,
                "operation_count": 1,
                "tool_count": 0,
                "errors": ["generated code is a fallback implementation"],
            },
        )

    monkeypatch.setattr(
        cli,
        "MCPServerGenerator",
        lambda **_kwargs: SimpleNamespace(generate_mcp_server=fail_generation),
    )
    output_config = MagicMock()
    output_config.get_results_dir.return_value = tmp_path

    path, usage = await cli._generate_mcp_server(
        MagicMock(), {"paths": {"/record": {"get": {}}}}, output_config
    )

    assert path is None
    assert usage["generation_status"] == "failed"
    assert usage["fallback"] is True
    assert usage["operation_count"] == 1
    assert usage["tool_count"] == 0


@pytest.mark.asyncio
async def test_execute_cli_workflow_returns_false_for_generation_failure(monkeypatch):
    args = SimpleNamespace(enhancement_level="comprehensive", eval_only=False)
    evaluation = MagicMock()
    result = SimpleNamespace(evaluation=evaluation)
    cleanup = AsyncMock()

    monkeypatch.setattr(
        cli,
        "_prepare_execution_context",
        lambda _args: ("api.yaml", "File: api.yaml", "test/model", MagicMock()),
    )
    monkeypatch.setattr(cli, "_load_specification", AsyncMock(return_value="{}"))
    monkeypatch.setattr(
        cli, "_run_enhancement_process", AsyncMock(return_value=result)
    )
    monkeypatch.setattr(cli, "_save_evaluation_files", lambda *_args: None)
    monkeypatch.setattr(cli, "_display_evaluation_results", lambda _evaluation: None)
    monkeypatch.setattr(
        cli,
        "_handle_mcp_generation",
        AsyncMock(side_effect=MCPGenerationError("MCP generation failed")),
    )
    monkeypatch.setattr(cli, "cleanup_llm_client", cleanup)

    assert await cli._execute_cli_workflow(args) is False
    cleanup.assert_awaited_once()