import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src import cli
from src.models.generated_validation import (
    GeneratedArtifactVerificationResult,
    OperationVerificationResult,
    RepairAttemptResult,
    VerificationStatus,
)
from src.services.config_loader import ConfigLoader
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


def test_generated_artifact_verification_settings_clamp_repair_attempts():
    args = SimpleNamespace(
        verify_generated=None,
        repair_generated=True,
        generated_verification_seed=11,
        generated_repair_attempts=9,
    )

    settings = cli._resolve_generated_artifact_verification_settings(args)

    assert settings["verify_enabled"] is True
    assert settings["repair_enabled"] is True
    assert settings["seed"] == 11
    assert settings["repair_attempts"] == 2


def test_config_defaults_include_safe_generated_verification_keys():
    loader = ConfigLoader(config_dir="config")

    assert loader.get_bool("generated_verification_enabled", True) is False
    assert loader.get_int("generated_verification_seed", -1) == 0
    assert loader.get_int("generated_verification_timeout_seconds", -1) == 30
    assert loader.get_bool("generated_repair_enabled", True) is False
    assert loader.get_int("generated_repair_max_attempts", -1) == 2


@pytest.mark.asyncio
async def test_handle_mcp_generation_verifies_generated_artifacts_when_flag_enabled(
    monkeypatch, tmp_path
):
    args = SimpleNamespace(
        eval_only=False,
        verify_generated=True,
        repair_generated=None,
        generated_verification_seed=13,
        generated_repair_attempts=None,
    )
    evaluation = MagicMock()
    result = SimpleNamespace(evaluation=evaluation)
    output_config = MagicMock()
    output_config.get_results_dir.return_value = tmp_path
    mcpserver_dir = tmp_path / "mcpserver"
    mcpserver_dir.mkdir()
    captured_usage = []
    original_spec = '{"openapi":"3.0.3","paths":{"/users":{"get":{"operationId":"listUsers"}}}}'
    verification_report = GeneratedArtifactVerificationResult(
        artifact_dir=str(mcpserver_dir),
        spec_sha256=hashlib.sha256(
            json.dumps(
                json.loads(original_spec),
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
        ).hexdigest(),
        generator_version="0.1.0",
        seed=13,
        status=VerificationStatus.PASSED,
        attempts=1,
        operations=[
            OperationVerificationResult(
                operation_id="listUsers",
                tool_name="listUsers",
                scenario_kind="success",
                status=VerificationStatus.PASSED,
                request_valid=True,
                response_valid=True,
            )
        ],
        repair_attempts=[],
        failures=[],
    )

    monkeypatch.setattr(cli, "_check_mcp_generation_criteria", lambda _evaluation: True)
    monkeypatch.setattr(
        cli,
        "_generate_mcp_server",
        AsyncMock(
            return_value=(
                mcpserver_dir,
                {
                    "generation_status": "complete",
                    "server_usage": {},
                    "client_usage": {},
                    "total_tokens": 0,
                    "total_cost_usd": 0.0,
                    "calls_count": 0,
                },
            )
        ),
    )
    verify_mock = AsyncMock(return_value=verification_report)
    monkeypatch.setattr(cli, "verify_generated_artifacts", verify_mock)
    monkeypatch.setattr(
        cli,
        "verify_with_repair",
        AsyncMock(side_effect=AssertionError("repair verification should not run")),
    )
    monkeypatch.setattr(
        cli,
        "_update_evaluation_with_mcp_usage",
        lambda _evaluation, mcp_usage, _result, _output_config, openapi_spec: captured_usage.append(
            (mcp_usage, openapi_spec)
        ),
    )

    path = await cli._handle_mcp_generation(
        args, evaluation, original_spec, output_config, result
    )

    assert path == mcpserver_dir
    verify_mock.assert_awaited_once_with(
        json.loads(original_spec), mcpserver_dir, seed=13, timeout_seconds=30
    )
    assert captured_usage[0][1] == original_spec
    assert captured_usage[0][0]["generated_artifact_verification"]["status"] == "passed"
    assert captured_usage[0][0]["generated_artifact_verification"]["seed"] == 13
    assert (mcpserver_dir / "verification" / "verification_report.json").exists()
    assert (mcpserver_dir / "verification" / "verification_summary.md").exists()


@pytest.mark.asyncio
async def test_handle_mcp_generation_repair_implies_verification(monkeypatch, tmp_path):
    args = SimpleNamespace(
        eval_only=False,
        verify_generated=None,
        repair_generated=True,
        generated_verification_seed=5,
        generated_repair_attempts=3,
    )
    evaluation = MagicMock()
    result = SimpleNamespace(evaluation=evaluation)
    output_config = MagicMock()
    output_config.get_results_dir.return_value = tmp_path
    mcpserver_dir = tmp_path / "mcpserver"
    mcpserver_dir.mkdir()

    monkeypatch.setattr(cli, "_check_mcp_generation_criteria", lambda _evaluation: True)
    monkeypatch.setattr(
        cli,
        "_generate_mcp_server",
        AsyncMock(
            return_value=(
                mcpserver_dir,
                {
                    "generation_status": "complete",
                    "server_usage": {},
                    "client_usage": {},
                    "total_tokens": 0,
                    "total_cost_usd": 0.0,
                    "calls_count": 0,
                },
            )
        ),
    )
    repairer_factory = MagicMock(return_value=MagicMock(repair=AsyncMock()))
    verify_mock = AsyncMock(
        return_value=GeneratedArtifactVerificationResult(
            artifact_dir=str(mcpserver_dir),
            spec_sha256="abc",
            generator_version="0.1.0",
            seed=5,
            status=VerificationStatus.PASSED,
            attempts=1,
            operations=[],
            repair_attempts=[],
            failures=[],
        )
    )
    monkeypatch.setattr(cli, "_create_generated_artifact_repairer", repairer_factory)
    monkeypatch.setattr(cli, "verify_with_repair", verify_mock)
    monkeypatch.setattr(
        cli,
        "verify_generated_artifacts",
        AsyncMock(side_effect=AssertionError("plain verification should not run")),
    )
    monkeypatch.setattr(
        cli,
        "_update_evaluation_with_mcp_usage",
        lambda *_args, **_kwargs: None,
    )

    path = await cli._handle_mcp_generation(
        args, evaluation, '{"openapi":"3.0.3","paths":{}}', output_config, result
    )

    assert path == mcpserver_dir
    repairer_factory.assert_called_once_with()
    assert verify_mock.await_count == 1
    assert verify_mock.await_args.kwargs["max_repair_attempts"] == 2
    assert verify_mock.await_args.kwargs["repairer"] is repairer_factory.return_value


@pytest.mark.asyncio
async def test_handle_mcp_generation_skips_generated_verification_by_default(
    monkeypatch, tmp_path
):
    args = SimpleNamespace(eval_only=False)
    evaluation = MagicMock()
    result = SimpleNamespace(evaluation=evaluation)
    output_config = MagicMock()
    output_config.get_results_dir.return_value = tmp_path
    mcpserver_dir = tmp_path / "mcpserver"
    mcpserver_dir.mkdir()

    monkeypatch.setattr(cli, "_check_mcp_generation_criteria", lambda _evaluation: True)
    monkeypatch.setattr(
        cli,
        "_generate_mcp_server",
        AsyncMock(
            return_value=(
                mcpserver_dir,
                {
                    "generation_status": "complete",
                    "server_usage": {},
                    "client_usage": {},
                    "total_tokens": 0,
                    "total_cost_usd": 0.0,
                    "calls_count": 0,
                },
            )
        ),
    )
    verify_mock = AsyncMock(side_effect=AssertionError("verification should not run"))
    repairer_factory = MagicMock(
        side_effect=AssertionError("repairer should not be created")
    )
    monkeypatch.setattr(cli, "_create_generated_artifact_repairer", repairer_factory)
    monkeypatch.setattr(cli, "verify_generated_artifacts", verify_mock)
    monkeypatch.setattr(
        cli,
        "verify_with_repair",
        AsyncMock(side_effect=AssertionError("repair verification should not run")),
    )
    monkeypatch.setattr(
        cli,
        "_update_evaluation_with_mcp_usage",
        lambda *_args, **_kwargs: None,
    )

    path = await cli._handle_mcp_generation(
        args, evaluation, '{"openapi":"3.0.3","paths":{}}', output_config, result
    )

    assert path == mcpserver_dir
    assert verify_mock.await_count == 0
    repairer_factory.assert_not_called()


@pytest.mark.asyncio
async def test_failed_generated_verification_marks_generation_failed_and_writes_report(
    monkeypatch, tmp_path
):
    args = SimpleNamespace(
        eval_only=False,
        verify_generated=True,
        repair_generated=None,
        generated_verification_seed=7,
        generated_repair_attempts=None,
    )
    evaluation = MagicMock()
    result = SimpleNamespace(evaluation=evaluation)
    output_config = MagicMock()
    output_config.get_results_dir.return_value = tmp_path
    mcpserver_dir = tmp_path / "mcpserver"
    mcpserver_dir.mkdir()
    original_spec = '{"openapi":"3.0.3","paths":{"/users":{"get":{"operationId":"listUsers"}}}}'
    captured_usage = []
    verification_report = GeneratedArtifactVerificationResult(
        artifact_dir=str(mcpserver_dir),
        spec_sha256=hashlib.sha256(
            json.dumps(
                json.loads(original_spec),
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
        ).hexdigest(),
        generator_version="0.1.0",
        seed=7,
        status=VerificationStatus.FAILED,
        attempts=1,
        operations=[
            OperationVerificationResult(
                operation_id="listUsers",
                tool_name="listUsers",
                scenario_kind="success",
                status=VerificationStatus.FAILED,
                request_valid=False,
                response_valid=False,
                failure_code="missing_generated_tool",
                message="missing generated tool for operation listUsers",
            )
        ],
        repair_attempts=[],
        failures=["missing_generated_tool"],
    )

    monkeypatch.setattr(cli, "_check_mcp_generation_criteria", lambda _evaluation: True)
    monkeypatch.setattr(
        cli,
        "_generate_mcp_server",
        AsyncMock(
            return_value=(
                mcpserver_dir,
                {
                    "generation_status": "complete",
                    "server_usage": {},
                    "client_usage": {},
                    "total_tokens": 0,
                    "total_cost_usd": 0.0,
                    "calls_count": 0,
                },
            )
        ),
    )
    monkeypatch.setattr(
        cli,
        "verify_generated_artifacts",
        AsyncMock(return_value=verification_report),
    )
    monkeypatch.setattr(
        cli,
        "verify_with_repair",
        AsyncMock(side_effect=AssertionError("repair verification should not run")),
    )
    monkeypatch.setattr(
        cli,
        "_update_evaluation_with_mcp_usage",
        lambda _evaluation, mcp_usage, _result, _output_config, openapi_spec: captured_usage.append(
            (mcp_usage, openapi_spec)
        ),
    )

    with pytest.raises(MCPGenerationError, match="MCP generation failed"):
        await cli._handle_mcp_generation(
            args, evaluation, original_spec, output_config, result
        )

    assert captured_usage[0][1] == original_spec
    assert captured_usage[0][0]["generation_status"] == "failed"
    assert captured_usage[0][0]["generated_artifact_verification"]["status"] == "failed"
    assert captured_usage[0][0]["generated_artifact_verification"]["seed"] == 7
    assert captured_usage[0][0]["generated_artifact_verification"]["spec_sha256"] == verification_report.spec_sha256

    report_json = mcpserver_dir / "verification" / "verification_report.json"
    report_markdown = mcpserver_dir / "verification" / "verification_summary.md"
    assert report_json.exists()
    assert report_markdown.exists()
    assert json.loads(report_json.read_text(encoding="utf-8"))["status"] == "failed"


def test_generated_artifact_verification_report_redacts_messages_and_secrets(
    tmp_path, monkeypatch
):
    sentinel_secret = "sentinel-secret-value"
    monkeypatch.setenv("API_KEY", sentinel_secret)

    report = GeneratedArtifactVerificationResult(
        artifact_dir="/tmp/mcpserver",
        spec_sha256="abc123",
        generator_version="0.1.0",
        seed=2,
        status=VerificationStatus.FAILED,
        attempts=2,
        operations=[
            OperationVerificationResult(
                operation_id="getUser",
                tool_name="getUser",
                scenario_kind="success",
                status=VerificationStatus.FAILED,
                request_valid=False,
                response_valid=False,
                error_kind="validation",
                failure_code="request_schema_mismatch",
                message=f"raw validation message {sentinel_secret}",
                redacted_input={
                    "headers": {"Authorization": sentinel_secret},
                    "body": {"token": sentinel_secret},
                },
                redacted_output={"detail": f"output {sentinel_secret}"},
            )
        ],
        repair_attempts=[
            RepairAttemptResult(
                attempt=1,
                accepted=False,
                candidate_dir="/tmp/candidate",
                changed_files=["server.py", "client.py"],
                failure_codes=["repair_failed"],
            )
        ],
        failures=["request_schema_mismatch"],
    )

    json_path, markdown_path = cli._save_generated_artifact_verification_report(
        report, tmp_path / "verification"
    )

    json_text = json_path.read_text(encoding="utf-8")
    markdown_text = markdown_path.read_text(encoding="utf-8")

    assert sentinel_secret not in json_text
    assert sentinel_secret not in markdown_text
    assert "raw validation message" not in json_text
    assert "raw validation message" not in markdown_text

    payload = json.loads(json_text)
    operation = payload["operations"][0]
    assert "message" not in operation
    assert "redacted_input" not in operation
    assert "redacted_output" not in operation
    assert payload["repair_attempts"][0]["attempt"] == 1
    assert payload["repair_attempts"][0]["accepted"] is False
    assert payload["repair_attempts"][0]["changed_files"] == ["server.py", "client.py"]
    assert "candidate_dir" not in payload["repair_attempts"][0]


def test_generated_artifact_verification_report_is_deterministic(tmp_path):
    report = {
        "artifact_dir": "/tmp/mcpserver",
        "spec_sha256": "abc123",
        "generator_version": "0.1.0",
        "seed": 2,
        "timeout_seconds": 30,
        "status": "passed",
        "file_checks": [
            {"status": "passed", "file_name": "client.py"},
            {"file_name": "server.py", "status": "passed"},
        ],
        "operation_statuses": [
            {
                "tool_name": "listUsers",
                "operation_id": "listUsers",
                "status": "passed",
            }
        ],
        "repair_attempts": [
            {
                "attempt": 1,
                "accepted": False,
                "candidate_dir": "/tmp/mcpserver",
                "changed_files": ["client.py"],
                "failure_codes": ["noop"],
            }
        ],
        "changed_files": ["client.py"],
        "failure_codes": [],
        "failures": [],
    }

    json_path_one, markdown_path_one = cli._save_generated_artifact_verification_report(
        report, tmp_path / "first"
    )
    json_path_two, markdown_path_two = cli._save_generated_artifact_verification_report(
        report, tmp_path / "second"
    )

    assert json_path_one.read_text(encoding="utf-8") == json_path_two.read_text(
        encoding="utf-8"
    )
    assert markdown_path_one.read_text(encoding="utf-8") == markdown_path_two.read_text(
        encoding="utf-8"
    )