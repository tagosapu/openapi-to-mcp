"""
Command-line interface for OpenAPI to MCP converter.
Supports evaluation and enhancement of OpenAPI specifications using LLM providers.
"""

import argparse
import asyncio
import datetime
import json
import logging
import os
import re
from collections.abc import Mapping as MappingABC
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import yaml
from pydantic import ValidationError
from .models.evaluation import OpenAPIEvaluationResult
from .services.config_loader import config
from .services.generated_artifact_repair import LLMGeneratedArtifactRepairer
from .services.generated_artifact_verifier import (
    verify_generated_artifacts,
    verify_with_repair,
)
from .services.mcp_generator import MCPGenerationError, MCPServerGenerator, _count_openapi_operations
from .services.openapi_enhancer import (
    EnhancementRequest,
    EnhancementResult,
    OpenAPIEnhancer,
)
from .services.llm_client import cleanup_llm_client
from .services.config_manager import (
    _detect_provider_from_model,
    _setup_logging,
    _show_environment_info,
)
from .services.spec_loader import _load_specification
from .services.output_formatter import (
    _display_evaluation_results,
    _print_results_summary,
)
from .services.output_config import OutputConfig

# No need to load .env file - let LiteLLM handle credentials from environment


# Configure logging with basicConfig
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s,p%(process)s,{%(filename)s:%(lineno)d},%(levelname)s,%(message)s",
)

logger = logging.getLogger(__name__)


_SECRET_ENV_NAME_PATTERN = re.compile(
    r"(token|secret|password|api[_-]?key|auth)", re.IGNORECASE
)


# Thresholds loaded from config.yml
# good_evaluation_threshold: Minimum score for "good" evaluation
# generate_mcp_threshold: Minimum score to generate MCP server

# -----------------------------------------------------------------------------
# Argument & Context Private Functions
# -----------------------------------------------------------------------------


def _create_argument_parser() -> argparse.ArgumentParser:
    """Create and configure the argument parser."""
    parser = argparse.ArgumentParser(
        description="Evaluate OpenAPI specifications using LLM providers (auto-detected from MODEL).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Evaluate local YAML file (provider auto-detected from MODEL env var)
  openapi-to-mcp my-api.yaml

  # Evaluate only, skip MCP generation
  openapi-to-mcp my-api.yaml --eval-only

  # Evaluate local JSON file with specific model
  MODEL=bedrock/us.anthropic.claude-3-5-sonnet-20241022-v2:0 openapi-to-mcp api-spec.json

  # Evaluate from URL
  openapi-to-mcp --url https://example.com/api/openapi.json

  # Specify custom output file
  openapi-to-mcp my-api.yaml --output my-results.json

Note: The LLM provider is automatically detected from the MODEL environment variable:
- azure/deployment-name -> Azure OpenAI
- bedrock/model-id → Amazon Bedrock
- claude-model-name → Anthropic

Azure OpenAI can also use AZURE_OPENAI_ENDPOINT, AZURE_OPENAI_API_KEY, and
VISION_MODEL, matching the official Azure OpenAI SDK convention.
        """,
    )

    # Add input arguments
    input_group = parser.add_mutually_exclusive_group()
    input_group.add_argument(
        "filename", nargs="?", help="OpenAPI specification file (YAML or JSON)"
    )
    input_group.add_argument("--url", help="URL to fetch OpenAPI specification from")

    # Add output arguments
    parser.add_argument(
        "--output",
        help="Output file for results (default: auto-generated based on input filename)",
    )
    parser.add_argument(
        "--enhancement-level",
        choices=["basic", "comprehensive", "ai-optimized"],
        default="comprehensive",
        help="Level of enhancement analysis (default: comprehensive)",
    )

    # Add utility arguments
    parser.add_argument("--verbose", action="store_true", help="Enable verbose logging")
    parser.add_argument(
        "--show-env", action="store_true", help="Show environment configuration"
    )
    parser.add_argument(
        "--eval-only",
        action="store_true",
        help="Only run evaluation, skip MCP server/client generation",
    )
    parser.add_argument(
        "--verify-generated",
        action="store_true",
        default=None,
        help="Run generated artifact verification after MCP generation",
    )
    parser.add_argument(
        "--repair-generated",
        action="store_true",
        default=None,
        help="Enable bounded repair attempts for generated artifacts (implies verification)",
    )
    parser.add_argument(
        "--generated-verification-seed",
        type=int,
        help="Seed for deterministic generated artifact verification",
    )
    parser.add_argument(
        "--generated-repair-attempts",
        type=int,
        help="Maximum generated artifact repair attempts (clamped to 0..2)",
    )

    return parser


def _validate_cli_arguments(
    args: argparse.Namespace, parser: argparse.ArgumentParser
) -> bool:
    """Validate command line arguments."""
    if not any([args.filename, args.url]):
        logger.error("No input specified. Use filename or --url option.")
        parser.print_help()
        return False
    return True


def _determine_spec_name(args: argparse.Namespace, filename: Optional[str]) -> str:
    """Determine spec name from arguments."""
    if filename:
        # Don't use generic script names as spec names
        spec_stem = Path(filename).stem
        if spec_stem in ["main", "evaluate", "script", "run"]:
            spec_name = "generic_spec"
        else:
            spec_name = spec_stem
    else:
        spec_name = "api_spec"

    return spec_name


def _determine_output_config(
    args: argparse.Namespace, model: str, filename: Optional[str] = None
) -> OutputConfig:
    """Determine output configuration based on arguments."""
    provider_name = _detect_provider_from_model(model)

    if args.output:
        output_path = Path(args.output)
        # Extract spec name from the path or use defaults
        spec_name = output_path.stem
    else:
        output_path = None
        spec_name = _determine_spec_name(args, filename)

    # Generate timestamp for uniqueness
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")

    return OutputConfig(
        output_path=output_path,
        provider_name=provider_name,
        spec_name=spec_name,
        timestamp=timestamp,
    )


def _prepare_execution_context(
    args: argparse.Namespace,
) -> Tuple[str, str, str, OutputConfig]:
    """Prepare execution context with model and output configuration."""
    filename = args.filename
    if args.url:
        spec_source = f"URL: {args.url}"
    else:
        spec_source = f"File: {filename}"

    # Get model from config.yml
    model = config.get_model()
    output_config = _determine_output_config(args, model, filename)

    return filename, spec_source, model, output_config


def _create_generated_artifact_repairer() -> LLMGeneratedArtifactRepairer:
    return LLMGeneratedArtifactRepairer()


def _resolve_generated_artifact_verification_settings(
    args: argparse.Namespace,
) -> Dict[str, Any]:
    verify_flag = getattr(args, "verify_generated", None)
    repair_flag = getattr(args, "repair_generated", None)
    seed_arg = getattr(args, "generated_verification_seed", None)
    attempts_arg = getattr(args, "generated_repair_attempts", None)

    verify_enabled = (
        bool(verify_flag)
        if verify_flag is not None
        else config.get_bool("generated_verification_enabled", True)
    )
    repair_enabled = (
        bool(repair_flag)
        if repair_flag is not None
        else config.get_bool("generated_repair_enabled", True)
    )
    if repair_enabled:
        verify_enabled = True

    seed = (
        int(seed_arg)
        if seed_arg is not None
        else config.get_int("generated_verification_seed", 0)
    )
    timeout_seconds = config.get_int("generated_verification_timeout_seconds", 30)
    repair_attempts = (
        int(attempts_arg)
        if attempts_arg is not None
        else config.get_int("generated_repair_max_attempts", 2)
    )
    repair_attempts = max(0, min(repair_attempts, 2))

    return {
        "verify_enabled": verify_enabled,
        "repair_enabled": repair_enabled,
        "seed": seed,
        "timeout_seconds": timeout_seconds,
        "repair_attempts": repair_attempts,
    }


def _coerce_generated_artifact_verification_report(
    report: Any,
) -> Dict[str, Any]:
    if callable(getattr(report, "model_dump", None)):
        payload = report.model_dump(mode="json")
    elif isinstance(report, dict):
        payload = dict(report)
    elif isinstance(report, MappingABC):
        payload = dict(report)
    else:
        payload = dict(vars(report))
    if not isinstance(payload, MappingABC):
        payload = dict(payload)
    return _sanitize_generated_artifact_verification_report(payload)


def _sanitize_generated_artifact_verification_report(
    report: MappingABC[str, Any],
) -> Dict[str, Any]:
    secret_values = _secret_values()
    operations = _collect_operation_summaries(report, secret_values)
    repair_attempts = _collect_repair_attempts(report, secret_values)
    failure_codes = _collect_failure_codes(report)
    changed_files = _collect_changed_files(report, repair_attempts)

    sanitized: Dict[str, Any] = {
        "artifact_dir": _sanitize_string(report.get("artifact_dir", ""), secret_values),
        "spec_sha256": _sanitize_string(report.get("spec_sha256", ""), secret_values),
        "generator_version": _sanitize_string(
            report.get("generator_version", ""), secret_values
        ),
        "seed": report.get("seed", 0),
        "status": report.get("status", "unvalidated"),
        "operations": operations,
        "repair_attempts": repair_attempts,
        "failure_codes": failure_codes,
        "failures": list(failure_codes),
    }

    if "attempts" in report:
        sanitized["attempts"] = report.get("attempts", 0)
    if "timeout_seconds" in report:
        sanitized["timeout_seconds"] = report.get("timeout_seconds", 0)
    if changed_files:
        sanitized["changed_files"] = changed_files
    if "file_checks" in report:
        sanitized["file_checks"] = _collect_file_checks(report, secret_values)

    return sanitized


def _collect_operation_summaries(
    report: MappingABC[str, Any], secret_values: list[str]
) -> list[Dict[str, Any]]:
    raw_operations = report.get("operations") or report.get("operation_statuses") or []
    operations: list[Dict[str, Any]] = []
    for item in raw_operations:
        if not isinstance(item, MappingABC):
            continue
        summary: Dict[str, Any] = {
            "operation_id": _sanitize_string(item.get("operation_id", ""), secret_values),
            "tool_name": _sanitize_string(item.get("tool_name", ""), secret_values),
            "status": _sanitize_string(item.get("status", "unvalidated"), secret_values),
        }
        if item.get("failure_code") is not None:
            summary["failure_code"] = _sanitize_string(item.get("failure_code"), secret_values)
        if item.get("request_valid") is not None:
            summary["request_valid"] = bool(item.get("request_valid"))
        if item.get("response_valid") is not None:
            summary["response_valid"] = bool(item.get("response_valid"))
        if item.get("error_kind") is not None:
            summary["error_kind"] = _sanitize_string(item.get("error_kind"), secret_values)
        if item.get("scenario_kind") is not None:
            summary["scenario_kind"] = _sanitize_string(item.get("scenario_kind"), secret_values)
        operations.append(summary)
    return operations


def _collect_repair_attempts(
    report: MappingABC[str, Any], secret_values: list[str]
) -> list[Dict[str, Any]]:
    raw_attempts = report.get("repair_attempts") or []
    attempts: list[Dict[str, Any]] = []
    for item in raw_attempts:
        if not isinstance(item, MappingABC):
            continue
        attempt: Dict[str, Any] = {
            "attempt": item.get("attempt", 0),
            "accepted": bool(item.get("accepted", False)),
        }
        changed_files = item.get("changed_files") or []
        if changed_files:
            attempt["changed_files"] = [
                _sanitize_string(str(file_name), secret_values) for file_name in changed_files
            ]
        failure_codes = _extract_failure_codes(item.get("failure_codes") or [])
        if failure_codes:
            attempt["failure_codes"] = failure_codes
        attempts.append(attempt)
    return attempts


def _collect_file_checks(
    report: MappingABC[str, Any], secret_values: list[str]
) -> list[Dict[str, Any]]:
    raw_checks = report.get("file_checks") or []
    checks: list[Dict[str, Any]] = []
    for item in raw_checks:
        if not isinstance(item, MappingABC):
            continue
        checks.append(
            {
                "file_name": _sanitize_string(item.get("file_name", ""), secret_values),
                "status": _sanitize_string(item.get("status", ""), secret_values),
            }
        )
    return checks


def _collect_failure_codes(report: MappingABC[str, Any]) -> list[str]:
    failure_codes: list[str] = []
    failure_codes.extend(_extract_failure_codes(report.get("failure_codes") or []))
    failure_codes.extend(_extract_failure_codes(report.get("failures") or []))
    return list(dict.fromkeys(failure_codes))


def _collect_changed_files(
    report: MappingABC[str, Any], repair_attempts: list[Dict[str, Any]]
) -> list[str]:
    changed_files: list[str] = []
    if "changed_files" in report and isinstance(report.get("changed_files"), list):
        changed_files.extend(str(item) for item in report.get("changed_files", []))
    for attempt in repair_attempts:
        changed_files.extend(str(item) for item in attempt.get("changed_files", []))
    return list(dict.fromkeys(changed_files))


def _extract_failure_codes(failures: Any) -> list[str]:
    if not isinstance(failures, list):
        return []

    codes: list[str] = []
    for failure in failures:
        code = _extract_failure_code(failure)
        if code:
            codes.append(code)
    return codes


def _extract_failure_code(failure: Any) -> str | None:
    if isinstance(failure, str):
        return failure
    if isinstance(failure, MappingABC):
        code = failure.get("code") or failure.get("failure_code")
        if code is None:
            return None
        return str(code)
    if failure is None:
        return None
    return None


def _sanitize_string(value: Any, secret_values: list[str]) -> Any:
    if not isinstance(value, str):
        return value
    sanitized = value
    for secret in secret_values:
        if secret:
            sanitized = sanitized.replace(secret, "[REDACTED]")
    return sanitized


def _secret_values() -> list[str]:
    return [
        value
        for name, value in sorted(os.environ.items())
        if value and _SECRET_ENV_NAME_PATTERN.search(name)
    ]


def _build_generated_artifact_verification_metadata(
    report: Any,
    json_report_path: Path,
    markdown_report_path: Path,
) -> Dict[str, Any]:
    report = _coerce_generated_artifact_verification_report(report)
    operations = list(report.get("operations", []))
    repair_attempts = list(report.get("repair_attempts", []))
    return {
        "status": report.get("status", "unvalidated"),
        "spec_sha256": report.get("spec_sha256", ""),
        "seed": report.get("seed", 0),
        "generator_version": report.get("generator_version", ""),
        "operation_count": len(operations),
        "failed_operation_count": sum(
            1 for item in operations if item.get("status") != "passed"
        ),
        "failure_codes": list(report.get("failure_codes", [])),
        "repair_attempt_count": len(repair_attempts),
        "repair_attempts": repair_attempts,
        "report_paths": {
            "json": str(json_report_path),
            "markdown": str(markdown_report_path),
        },
    }


def _render_generated_artifact_verification_summary(report: Any) -> str:
    report = _coerce_generated_artifact_verification_report(report)
    lines = ["# Generated Artifact Verification Report", ""]
    lines.append("## Overview")
    lines.append("")
    lines.append("| Field | Value |")
    lines.append("|-------|-------|")
    lines.append(f"| **Artifact Dir** | {report.get('artifact_dir', '')} |")
    lines.append(f"| **Spec SHA-256** | {report.get('spec_sha256', '')} |")
    lines.append(f"| **Seed** | {report.get('seed', 0)} |")
    lines.append(f"| **Generator Version** | {report.get('generator_version', '')} |")
    lines.append(f"| **Status** | {report.get('status', 'unvalidated')} |")
    lines.append("")

    operations = list(report.get("operations", []))
    if operations:
        lines.append("## Operation Statuses")
        lines.append("")
        lines.append("| Operation | Tool | Status | Failure Code |")
        lines.append("|-----------|------|--------|--------------|")
        for item in operations:
            lines.append(
                "| {operation_id} | {tool_name} | {status} | {failure_code} |".format(
                    operation_id=item.get("operation_id", ""),
                    tool_name=item.get("tool_name", ""),
                    status=item.get("status", ""),
                    failure_code=item.get("failure_code", ""),
                )
            )
        lines.append("")

    repair_attempts = list(report.get("repair_attempts", []))
    if repair_attempts:
        lines.append("## Repair Attempts")
        lines.append("")
        lines.append("| Attempt | Accepted | Changed Files | Failure Codes |")
        lines.append("|---------|----------|---------------|---------------|")
        for item in repair_attempts:
            lines.append(
                "| {attempt} | {accepted} | {changed_files} | {failure_codes} |".format(
                    attempt=item.get("attempt", ""),
                    accepted=str(item.get("accepted", False)).lower(),
                    changed_files=", ".join(item.get("changed_files", [])),
                    failure_codes=", ".join(item.get("failure_codes", [])),
                )
            )
        lines.append("")

    changed_files = list(report.get("changed_files", []))
    if changed_files:
        lines.append("## Changed Files")
        lines.append("")
        for file_name in changed_files:
            lines.append(f"- {file_name}")
        lines.append("")

    failures = list(report.get("failure_codes", []))
    if failures:
        lines.append("## Failures")
        lines.append("")
        for failure in failures:
            lines.append(f"- {failure}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def _save_generated_artifact_verification_report(
    report: Any, verification_dir: Path
) -> Tuple[Path, Path]:
    verification_dir.mkdir(parents=True, exist_ok=True)
    report_data = _coerce_generated_artifact_verification_report(report)
    json_path = verification_dir / "verification_report.json"
    markdown_path = verification_dir / "verification_summary.md"

    with open(json_path, "w", encoding="utf-8") as json_file:
        json.dump(report_data, json_file, ensure_ascii=False, indent=2, sort_keys=True)

    with open(markdown_path, "w", encoding="utf-8") as markdown_file:
        markdown_file.write(_render_generated_artifact_verification_summary(report_data))

    return json_path, markdown_path


# -----------------------------------------------------------------------------
# File I/O & Data Private Functions
# -----------------------------------------------------------------------------


def _save_usage_file(evaluation: OpenAPIEvaluationResult, usage_file: Path) -> None:
    """Save detailed usage information to a separate JSON file."""
    try:
        usage_data = {
            "timestamp": evaluation.timestamp.isoformat(),
            "evaluation_id": evaluation.evaluation_id,
            "api_title": evaluation.api_title,
            "model": evaluation.model,
            "summary": {
                "total_tokens": getattr(evaluation, "total_tokens", 0) or 0,
                "total_cost_usd": getattr(evaluation, "total_cost_usd", 0.0) or 0.0,
                "total_calls": getattr(evaluation, "llm_calls_count", 0) or 0,
            },
            "detailed_breakdown": {
                "evaluation": getattr(evaluation, "evaluation_usage", {}),
                "mcp_server_generation": getattr(evaluation, "mcp_server_usage", {}),
                "mcp_client_generation": getattr(evaluation, "mcp_client_usage", {}),
            },
        }

        with open(usage_file, "w", encoding="utf-8") as f:
            json.dump(usage_data, f, indent=2)

        logger.info(f"Saved detailed usage information to {usage_file}")
    except Exception as e:
        logger.error(f"Failed to save usage file: {e}")
        raise


def _save_evaluation_files(
    result: EnhancementResult, output_config: OutputConfig, original_spec: str
) -> Tuple[Path, Path, Path, Path, Path]:
    """Save evaluation results to files."""
    try:
        eval_file, enhanced_file, original_file, summary_file, usage_file = (
            output_config.get_output_files()
        )

        # Save evaluation JSON
        with open(eval_file, "w", encoding="utf-8") as f:
            # Use model_dump with mode="json" to handle datetime serialization
            json.dump(result.evaluation.model_dump(mode="json"), f, indent=2)

        # Save enhanced specification if available
        if result.enhanced_spec:
            with open(enhanced_file, "w", encoding="utf-8") as f:
                f.write(result.enhanced_spec)

        # Save original specification
        with open(original_file, "w", encoding="utf-8") as f:
            f.write(original_spec)

        # Save summary
        _save_summary_file(result.evaluation, summary_file, output_config)

        # Save detailed usage information
        _save_usage_file(result.evaluation, usage_file)

        logger.info(f"Saved evaluation files to {output_config.get_results_dir()}")
        return eval_file, enhanced_file, original_file, summary_file, usage_file
    except Exception as e:
        logger.error(f"Failed to save evaluation files: {e}")
        raise


def _save_summary_file(
    evaluation: OpenAPIEvaluationResult, summary_file: Path, output_config: OutputConfig
) -> None:
    """Save a comprehensive human-readable summary of the evaluation for developers."""
    try:
        with open(summary_file, "w", encoding="utf-8") as f:
            # Header
            f.write("# OpenAPI Evaluation Report\n\n")
            f.write(
                "> **Generated for developers** - This report provides a comprehensive analysis of your OpenAPI specification\n\n"
            )

            # Basic Information
            f.write("## Basic Information\n\n")
            f.write("| Field | Value |\n")
            f.write("|-------|-------|\n")
            f.write(f"| **API Title** | {evaluation.api_title} |\n")
            f.write(f"| **API Version** | {evaluation.api_version} |\n")
            f.write(f"| **OpenAPI Version** | {evaluation.openapi_version} |\n")
            f.write(f"| **Evaluation Date** | {evaluation.timestamp} |\n")
            f.write(f"| **Model Used** | {evaluation.model} |\n")
            f.write(f"| **Enhancement Level** | {evaluation.enhancement_level} |\n")
            f.write(f"| **Provider** | {output_config.provider_name} |\n\n")

            # Overall Scores
            f.write("## Overall Scores\n\n")
            f.write("| Metric | Score | Status |\n")
            f.write("|--------|-------|--------|\n")
            f.write(
                f"| **Overall Quality** | {evaluation.overall.overall_quality.value.upper()} | {'Pass' if evaluation.overall.overall_quality.value.upper() in ['GOOD', 'EXCELLENT'] else 'Warning'} |\n"
            )
            f.write(
                f"| **Completeness Score** | {evaluation.overall.completeness_score}/5 | {'Pass' if evaluation.overall.completeness_score >= 3 else 'Fail'} |\n"
            )
            f.write(
                f"| **AI Readiness Score** | {evaluation.overall.ai_readiness_score}/5 | {'Pass' if evaluation.overall.ai_readiness_score >= 3 else 'Fail'} |\n"
            )
            f.write(
                f"| **MCP Generation Ready** | {'Yes' if evaluation.overall.completeness_score >= 3 and evaluation.overall.ai_readiness_score >= 3 else 'No'} | {'Pass' if evaluation.overall.completeness_score >= 3 and evaluation.overall.ai_readiness_score >= 3 else 'Fail'} |\n"
            )

            # Always include pagination and filtering support flags
            pagination_support = getattr(
                evaluation.overall, "pagination_support_adequate", None
            )
            filtering_support = getattr(
                evaluation.overall, "filtering_support_adequate", None
            )

            if pagination_support is not None:
                status = "Pass" if pagination_support else "Fail"
                f.write(
                    f"| **Pagination Support Adequate** | {'Yes' if pagination_support else 'No'} | {status} |\n"
                )
            else:
                f.write("| **Pagination Support Adequate** | Not Applicable | N/A |\n")

            if filtering_support is not None:
                status = "Pass" if filtering_support else "Fail"
                f.write(
                    f"| **Filtering Support Adequate** | {'Yes' if filtering_support else 'No'} | {status} |\n"
                )
            else:
                f.write("| **Filtering Support Adequate** | Not Applicable | N/A |\n")

            f.write("\n")

            # Usage and Cost Information (Evaluation Only)
            f.write("## Evaluation Usage & Cost Information\n\n")

            # Get evaluation usage information only
            eval_usage = getattr(evaluation, "evaluation_usage", {})

            # Evaluation summary table
            f.write("| Metric | Value |\n")
            f.write("|--------|-------|\n")

            if eval_usage:
                prompt_tokens = eval_usage.get("prompt_tokens", 0)
                completion_tokens = eval_usage.get("completion_tokens", 0)
                total_tokens = eval_usage.get("total_tokens", 0)
                cost = eval_usage.get("total_cost_usd", 0.0)
                calls = eval_usage.get("calls_count", 0)

                f.write(f"| **Prompt Tokens** | {prompt_tokens:,} |\n")
                f.write(f"| **Completion Tokens** | {completion_tokens:,} |\n")
                f.write(f"| **Total Tokens** | {total_tokens:,} |\n")
                f.write(f"| **LLM Calls** | {calls} |\n")

                if cost > 0:
                    f.write(f"| **Total Cost (USD)** | ${cost:.6f} |\n")
                else:
                    f.write("| **Total Cost (USD)** | Not available |\n")
            else:
                f.write("| **Prompt Tokens** | N/A |\n")
                f.write("| **Completion Tokens** | N/A |\n")
                f.write("| **Total Tokens** | N/A |\n")
                f.write("| **LLM Calls** | N/A |\n")
                f.write("| **Total Cost (USD)** | N/A |\n")

            f.write(
                f"\n*Note: For complete usage information including MCP generation costs, see the usage_{output_config.timestamp}.json file*\n"
            )

            f.write("\n")

            # API Information
            if hasattr(evaluation, "api_info") and evaluation.api_info:
                f.write("## API Information Analysis\n\n")
                info = evaluation.api_info
                f.write("| Component | Status | Notes |\n")
                f.write("|-----------|--------|\n")
                f.write(
                    f"| **Title** | {'Present' if info.has_title else 'Missing'} | Clear and descriptive API name |\n"
                )
                f.write(
                    f"| **Description** | {'Present' if info.has_description else 'Missing'} | Comprehensive overview of API purpose |\n"
                )
                f.write(
                    f"| **Version** | {'Present' if info.has_version else 'Missing'} | Proper API versioning |\n"
                )
                f.write(
                    f"| **Contact Info** | {'Present' if info.has_contact else 'Missing'} | Contact details for support |\n"
                )
                f.write(
                    f"| **License** | {'Present' if info.has_license else 'Missing'} | License information |\n"
                )
                f.write(
                    f"| **Terms of Service** | {'Present' if info.has_terms_of_service else 'Missing'} | Legal terms |\n\n"
                )

                if info.suggestions:
                    f.write("### API Info Suggestions\n\n")
                    for suggestion in info.suggestions:
                        f.write(f"- {suggestion}\n")
                    f.write("\n")

            # Security Analysis
            if hasattr(evaluation, "security_schemes") and evaluation.security_schemes:
                f.write("## Security Analysis\n\n")
                f.write(
                    f"**Total Security Schemes**: {len(evaluation.security_schemes)}\n\n"
                )
                for scheme in evaluation.security_schemes:
                    f.write(f"### {scheme.name}\n")
                    f.write(f"- **Type**: {scheme.type}\n")
                    f.write(f"- **Description**: {scheme.description}\n")
                    f.write(f"- **Required**: {'Yes' if scheme.required else 'No'}\n\n")

            # Operations Analysis
            if hasattr(evaluation, "operations") and evaluation.operations:
                f.write("## Operations Analysis\n\n")
                f.write(f"**Total Operations**: {len(evaluation.operations)}\n\n")

                # Summary table
                f.write("### Operations Overview\n\n")
                f.write(
                    "| Operation | Method | Path | Description Quality | Parameter Completeness | Response Completeness | Pagination | Filtering |\n"
                )
                f.write(
                    "|-----------|--------|------|--------------------|-----------------------|----------------------|------------|-----------|\n"
                )

                for op in evaluation.operations:
                    desc_icon = (
                        "Excellent"
                        if op.description_quality == "excellent"
                        else "Good" if op.description_quality == "good" else "Poor"
                    )
                    param_icon = (
                        "Excellent"
                        if op.parameter_completeness == "excellent"
                        else "Good"
                        if op.parameter_completeness == "good"
                        else "N/A"
                        if op.parameter_completeness == "not_applicable"
                        else "Poor"
                    )
                    resp_icon = (
                        "Excellent"
                        if op.response_completeness == "excellent"
                        else "Good" if op.response_completeness == "good" else "Poor"
                    )

                    # Check for pagination and filtering support
                    returns_list = getattr(op, "returns_list", False)
                    has_pagination = getattr(op, "has_pagination", False)
                    has_filtering = getattr(op, "has_filtering", False)

                    if returns_list:
                        pagination_status = "✅ Yes" if has_pagination else "❌ No"
                        filtering_status = "✅ Yes" if has_filtering else "❌ No"
                    else:
                        pagination_status = "N/A"
                        filtering_status = "N/A"

                    f.write(
                        f"| `{op.operation_id}` | {op.method} | `{op.path}` | {desc_icon} {op.description_quality} | {param_icon} {op.parameter_completeness} | {resp_icon} {op.response_completeness} | {pagination_status} | {filtering_status} |\n"
                    )

                f.write("\n### Detailed Operation Analysis\n\n")
                for op in evaluation.operations:
                    f.write(f"#### `{op.operation_id}` - {op.method} {op.path}\n\n")
                    f.write(f"**Summary**: {op.summary}\n\n")
                    if op.description:
                        f.write(f"**Description**: {op.description}\n\n")

                    # Add pagination and filtering information for operations returning lists
                    returns_list = getattr(op, "returns_list", False)
                    if returns_list:
                        f.write("**List Operation Support**:\n")
                        has_pagination = getattr(op, "has_pagination", False)
                        has_filtering = getattr(op, "has_filtering", False)
                        pagination_readiness = getattr(
                            op, "pagination_readiness", "not_applicable"
                        )
                        filtering_readiness = getattr(
                            op, "filtering_readiness", "not_applicable"
                        )

                        f.write("- Returns List: Yes\n")
                        f.write(
                            f"- Pagination Support: {'✅ Yes' if has_pagination else '❌ No'} (Readiness: {pagination_readiness})\n"
                        )
                        f.write(
                            f"- Filtering Support: {'✅ Yes' if has_filtering else '❌ No'} (Readiness: {filtering_readiness})\n"
                        )
                        f.write("\n")

                    # Parameters
                    if op.parameters:
                        f.write(f"**Parameters** ({len(op.parameters)}):\n\n")
                        for param in op.parameters:
                            f.write(f"- `{param.name}` ({param.location})")
                            if hasattr(param, "description_quality"):
                                f.write(f" - Quality: {param.description_quality}")
                            if hasattr(param, "example_provided"):
                                f.write(
                                    f", Example: {'Yes' if param.example_provided else 'No'}"
                                )
                            if hasattr(param, "constraints_defined"):
                                f.write(
                                    f", Constraints: {'Yes' if param.constraints_defined else 'No'}"
                                )
                            f.write("\n")
                            if hasattr(param, "suggestions") and param.suggestions:
                                for suggestion in param.suggestions:
                                    f.write(f"  - Suggestion: {suggestion}\n")

                    # Responses
                    if op.responses:
                        f.write(f"\n**Responses** ({len(op.responses)}):\n\n")
                        for resp in op.responses:
                            f.write(
                                f"- `{resp.status_code}` - Quality: {resp.description_quality}"
                            )
                            if hasattr(resp, "schema_provided"):
                                f.write(
                                    f", Schema: {'Yes' if resp.schema_provided else 'No'}"
                                )
                            if hasattr(resp, "examples_provided"):
                                f.write(
                                    f", Examples: {'Yes' if resp.examples_provided else 'No'}"
                                )
                            f.write("\n")

                    # Missing items
                    if op.missing_parameters:
                        f.write(
                            f"\n**Missing Parameters**: {', '.join(op.missing_parameters)}\n"
                        )
                    if op.missing_responses:
                        f.write(
                            f"\n**Missing Response Codes**: {', '.join(op.missing_responses)}\n"
                        )

                    # Enhancement suggestions
                    if op.enhancement_suggestions:
                        f.write("\n**Enhancement Suggestions**:\n")
                        for suggestion in op.enhancement_suggestions:
                            f.write(f"- {suggestion}\n")

                    f.write("\n---\n\n")

            # Schema Analysis
            if hasattr(evaluation, "schemas") and evaluation.schemas:
                f.write("## Schema Analysis\n\n")
                f.write(f"**Total Schemas**: {len(evaluation.schemas)}\n\n")

                # Summary table
                f.write(
                    "| Schema | Completeness | Has Description | Has Examples | Required Fields |\n"
                )
                f.write(
                    "|--------|--------------|-----------------|--------------|----------------|\n"
                )

                for schema in evaluation.schemas:
                    schema_name = getattr(
                        schema, "schema_name", getattr(schema, "name", "Unknown")
                    )
                    completeness = getattr(schema, "completeness", "N/A")
                    has_description = getattr(
                        schema,
                        "has_description",
                        getattr(schema, "description_quality", "N/A") != "N/A",
                    )
                    has_examples = getattr(
                        schema,
                        "has_examples",
                        getattr(schema, "examples_provided", False),
                    )
                    required_count = (
                        len(getattr(schema, "required_fields", []))
                        if hasattr(schema, "required_fields")
                        else "N/A"
                    )
                    f.write(
                        f"| `{schema_name}` | {completeness} | {'Yes' if has_description else 'No'} | {'Yes' if has_examples else 'No'} | {required_count} |\n"
                    )

                f.write("\n### Schema Details\n\n")
                for schema in evaluation.schemas:
                    schema_name = getattr(
                        schema, "schema_name", getattr(schema, "name", "Unknown")
                    )
                    f.write(f"#### `{schema_name}`\n")

                    # Check for missing descriptions
                    missing_descriptions = getattr(schema, "missing_descriptions", [])
                    if missing_descriptions:
                        f.write(
                            f"- **Missing Descriptions**: {', '.join(missing_descriptions)}\n"
                        )

                    # Check for suggestions
                    suggestions = getattr(schema, "suggestions", [])
                    if suggestions:
                        f.write("- **Suggestions**:\n")
                        for suggestion in suggestions:
                            f.write(f"  - {suggestion}\n")

                    # Add quality information
                    if hasattr(schema, "description_quality"):
                        f.write(
                            f"- **Description Quality**: {schema.description_quality}\n"
                        )
                    if hasattr(schema, "properties_documented"):
                        f.write(
                            f"- **Properties Documented**: {'Yes' if schema.properties_documented else 'No'}\n"
                        )
                    if hasattr(schema, "required_fields_specified"):
                        f.write(
                            f"- **Required Fields Specified**: {'Yes' if schema.required_fields_specified else 'No'}\n"
                        )

                    f.write("\n")

            # Linting Results
            if hasattr(evaluation, "linting") and evaluation.linting:
                f.write("## Linting Results\n\n")
                f.write(f"**Linting Score**: {evaluation.linting.linting_score}/5\n\n")
                f.write("| Severity | Count | Description |\n")
                f.write("|----------|-------|-------------|\n")
                f.write(
                    f"| Critical | {evaluation.linting.critical_issues} | Must fix - blocks functionality |\n"
                )
                f.write(
                    f"| Major | {evaluation.linting.major_issues} | Should fix - impacts usability |\n"
                )
                f.write(
                    f"| Minor | {evaluation.linting.minor_issues} | Nice to fix - improves quality |\n"
                )
                f.write(
                    f"| Info | {evaluation.linting.info_issues} | Suggestions - best practices |\n"
                )
                f.write(f"| **Total** | **{evaluation.linting.total_issues}** | |\n\n")

                if hasattr(evaluation.linting, "issues") and evaluation.linting.issues:
                    f.write("### Linting Issues by Severity\n\n")

                    # Group issues by severity
                    critical_issues = [
                        i
                        for i in evaluation.linting.issues
                        if getattr(i, "severity", None) == "critical"
                    ]
                    major_issues = [
                        i
                        for i in evaluation.linting.issues
                        if getattr(i, "severity", None) == "major"
                    ]
                    minor_issues = [
                        i
                        for i in evaluation.linting.issues
                        if getattr(i, "severity", None) == "minor"
                    ]
                    info_issues = [
                        i
                        for i in evaluation.linting.issues
                        if getattr(i, "severity", None) == "info"
                    ]
                    suggestion_issues = [
                        i
                        for i in evaluation.linting.issues
                        if getattr(i, "severity", None) == "suggestion"
                    ]

                    if critical_issues:
                        f.write("#### Critical Issues\n\n")
                        for issue in critical_issues:
                            rule = getattr(issue, "rule", "Unknown")
                            message = getattr(issue, "message", "No message")
                            path = getattr(issue, "path", None)
                            suggestion = getattr(issue, "suggestion", None)

                            f.write(f"- **{rule}**: {message}")
                            if path:
                                f.write(f" (at `{path}`)")
                            f.write("\n")
                            if suggestion:
                                f.write(f"  - Suggestion: {suggestion}\n")
                        f.write("\n")

                    if major_issues:
                        f.write("#### Major Issues\n\n")
                        for issue in major_issues:
                            rule = getattr(issue, "rule", "Unknown")
                            message = getattr(issue, "message", "No message")
                            path = getattr(issue, "path", None)
                            suggestion = getattr(issue, "suggestion", None)

                            f.write(f"- **{rule}**: {message}")
                            if path:
                                f.write(f" (at `{path}`)")
                            f.write("\n")
                            if suggestion:
                                f.write(f"  - Suggestion: {suggestion}\n")
                        f.write("\n")

                    if minor_issues:
                        f.write("#### Minor Issues\n\n")
                        for issue in minor_issues:
                            rule = getattr(issue, "rule", "Unknown")
                            message = getattr(issue, "message", "No message")
                            path = getattr(issue, "path", None)

                            f.write(f"- **{rule}**: {message}")
                            if path:
                                f.write(f" (at `{path}`)")
                            f.write("\n")
                        f.write("\n")

                    if info_issues:
                        f.write("#### Informational\n\n")
                        for issue in info_issues:
                            rule = getattr(issue, "rule", "Unknown")
                            message = getattr(issue, "message", "No message")
                            path = getattr(issue, "path", None)

                            f.write(f"- **{rule}**: {message}")
                            if path:
                                f.write(f" (at `{path}`)")
                            f.write("\n")
                        f.write("\n")

                    if suggestion_issues:
                        f.write("#### Suggestions\n\n")
                        for issue in suggestion_issues:
                            rule = getattr(issue, "rule", "Unknown")
                            message = getattr(issue, "message", "No message")
                            suggestion = getattr(issue, "suggestion", None)

                            f.write(f"- **{rule}**: {message}")
                            if suggestion:
                                f.write(f" - {suggestion}")
                            f.write("\n")
                        f.write("\n")

            # Key Strengths and Improvements
            f.write("## Key Strengths\n\n")
            if evaluation.overall.key_strengths:
                for strength in evaluation.overall.key_strengths:
                    f.write(f"- {strength}\n")
            else:
                f.write("*No specific strengths identified*\n")

            f.write("\n## Areas for Improvement\n\n")
            if evaluation.overall.areas_for_improvement:
                for improvement in evaluation.overall.areas_for_improvement:
                    f.write(f"- {improvement}\n")
            else:
                f.write("*No specific improvements identified*\n")

            # Recommendations
            if evaluation.overall.recommendations:
                f.write("\n## Recommendations\n\n")
                for i, recommendation in enumerate(
                    evaluation.overall.recommendations, 1
                ):
                    f.write(f"{i}. {recommendation}\n")

            # Summary
            f.write("\n## Summary\n\n")
            if hasattr(evaluation.overall, "summary") and evaluation.overall.summary:
                f.write(f"{evaluation.overall.summary}\n")
            else:
                mcp_ready = (
                    evaluation.overall.completeness_score >= 3
                    and evaluation.overall.ai_readiness_score >= 3
                )
                f.write(
                    f"This OpenAPI specification has an overall quality rating of **{evaluation.overall.overall_quality.value.upper()}** "
                )
                f.write(
                    f"with a completeness score of **{evaluation.overall.completeness_score}/5** and "
                )
                f.write(
                    f"AI readiness score of **{evaluation.overall.ai_readiness_score}/5**. "
                )

                if mcp_ready:
                    f.write(
                        "\n\n**The specification meets the criteria for MCP server generation.**"
                    )
                else:
                    f.write(
                        "\n\n**The specification does not yet meet the criteria for MCP server generation.** "
                    )
                    f.write("Both completeness and AI readiness scores must be >=3.")

            # Footer
            f.write("\n\n---\n")
            f.write(
                f"*Generated by OpenAPI to MCP Converter - {evaluation.timestamp}*\n"
            )

        logger.info(f"Saved comprehensive summary to {summary_file}")
    except Exception as e:
        logger.error(f"Failed to save summary file: {e}")
        raise


# -----------------------------------------------------------------------------
# Processing & Generation Private Functions
# -----------------------------------------------------------------------------


async def _run_enhancement_process(
    openapi_spec: str,
    args: argparse.Namespace,
    filename: Optional[str],
    model: str = None,
) -> EnhancementResult:
    """Run the OpenAPI enhancement process."""

    # Pass LLM parameters from config.yml
    enhancer = OpenAPIEnhancer(
        model=model,
        max_tokens=config.get_int("max_tokens"),
        temperature=config.get_float("temperature"),
        timeout_seconds=config.get_int("timeout_seconds"),
        debug=config.get_bool("debug"),
    )

    request = EnhancementRequest(
        spec_content=openapi_spec,
        spec_format="json" if openapi_spec.strip().startswith("{") else "yaml",
        original_filename=filename,
        enhancement_level=args.enhancement_level,
    )

    return await enhancer.enhance_specification(request)


def _check_mcp_generation_criteria(evaluation: OpenAPIEvaluationResult) -> bool:
    """Check if the evaluation meets criteria for MCP server generation."""
    # Get threshold from config.yml
    generate_threshold = config.get_float("generate_mcp_threshold", 3.0)

    completeness_ok = evaluation.overall.completeness_score >= generate_threshold
    ai_readiness_ok = evaluation.overall.ai_readiness_score >= generate_threshold

    logger.info("🔍 MCP Generation Criteria Check:")
    logger.info(
        f"   Completeness Score: {evaluation.overall.completeness_score} (required: >= {generate_threshold})"
    )
    logger.info(
        f"   AI Readiness Score: {evaluation.overall.ai_readiness_score} (required: >= {generate_threshold})"
    )
    logger.info(f"   Completeness OK: {completeness_ok}")
    logger.info(f"   AI Readiness OK: {ai_readiness_ok}")
    logger.info(
        f"   Overall MCP Generation Criteria Met: {completeness_ok and ai_readiness_ok}"
    )

    return completeness_ok and ai_readiness_ok


async def _generate_mcp_server(
    evaluation: OpenAPIEvaluationResult,
    openapi_spec: Dict[str, Any],
    output_config: OutputConfig,
    model: str = None,
) -> Tuple[Optional[Path], Dict[str, Any]]:
    """Generate MCP server code from OpenAPI specification."""
    operation_count = (
        _count_openapi_operations(openapi_spec) if isinstance(openapi_spec, dict) else 0
    )

    def failed_usage(error: Exception) -> Dict[str, Any]:
        validation = getattr(error, "validation", {})
        if isinstance(error, MCPGenerationError):
            error_message = " ".join(str(error).split())[:500]
        else:
            error_message = f"{type(error).__name__}: MCP generation failed"
        return {
            "server_usage": {},
            "client_usage": {},
            "total_tokens": 0,
            "total_cost_usd": 0.0,
            "calls_count": 0,
            "generation_status": "failed",
            "operation_count": validation.get("operation_count", operation_count),
            "tool_count": validation.get("tool_count", 0),
            "fallback": bool(validation.get("fallback", False)),
            "validation_errors": [error_message],
        }

    try:
        logger.info("🚀 Starting MCP server generation process...")

        # Step 1: Verify we have a valid OpenAPI spec dictionary
        logger.info("📋 Step 1: Verifying OpenAPI specification...")
        if not isinstance(openapi_spec, dict):
            logger.error(
                f"❌ Step 1 FAILED: Expected dictionary, got {type(openapi_spec)}"
            )
            return None, failed_usage(MCPGenerationError("invalid OpenAPI specification"))

        logger.info("✅ Step 1 PASSED: Valid OpenAPI specification provided")

        # Step 2: Create directory
        logger.info("📁 Step 2: Creating MCP server directory...")
        mcpserver_dir = output_config.get_results_dir() / "mcpserver"
        logger.info(f"   Target directory: {mcpserver_dir}")
        mcpserver_dir.mkdir(parents=True, exist_ok=True)
        logger.info(f"✅ Step 2 PASSED: Directory created/verified: {mcpserver_dir}")

        # Step 3: Initialize generator
        logger.info("🔧 Step 3: Initializing MCP generator...")
        try:
            # Pass LLM parameters from config.yml
            generator = MCPServerGenerator(
                model=model,
                max_tokens=config.get_int("max_tokens"),
                temperature=config.get_float("temperature"),
                timeout_seconds=config.get_int("timeout_seconds"),
                debug=config.get_bool("debug"),
            )
            logger.info("✅ Step 3 PASSED: MCP generator initialized successfully")
        except Exception as e:
            logger.error(f"❌ Step 3 FAILED: Failed to initialize MCP generator: {e}")
            raise

        # Step 4: Validate OpenAPI structure
        logger.info("📄 Step 4: Validating OpenAPI specification structure...")

        try:
            logger.info(
                f"✅ Step 4 PASSED: OpenAPI spec has {len(openapi_spec)} top-level keys"
            )
            if "info" in openapi_spec:
                logger.info(
                    f"   API Title: {openapi_spec['info'].get('title', 'Unknown')}"
                )
                logger.info(
                    f"   API Version: {openapi_spec['info'].get('version', 'Unknown')}"
                )
            if "paths" in openapi_spec:
                logger.info(f"   Number of paths: {len(openapi_spec['paths'])}")
        except Exception as e:
            logger.error(f"❌ Step 4 FAILED: Failed to validate spec: {e}")
            raise

        # Step 5: Generate MCP server files
        logger.info("🔄 Step 5: Generating MCP server files...")
        try:
            mcp_usage = await generator.generate_mcp_server(openapi_spec, mcpserver_dir)
            logger.info("✅ Step 5 PASSED: MCP server files generated successfully")

            # Log usage information if available
            if mcp_usage:
                logger.info(
                    f"MCP Generation Usage - Total tokens: {mcp_usage.get('total_tokens', 0)}, Cost: ${mcp_usage.get('total_cost_usd', 0.0):.6f}"
                )
                logger.info(f"_generate_mcp_server received mcp_usage: {mcp_usage}")
            else:
                logger.warning("No MCP usage data returned from generator")
                mcp_usage = {}

        except Exception as e:
            logger.error(f"❌ Step 5 FAILED: Failed to generate MCP server files: {e}")
            logger.error(f"   Error type: {type(e).__name__}")
            logger.error(f"   Error details: {str(e)}")
            raise

        # Step 6: Verify generated files
        logger.info("🔍 Step 6: Verifying generated files...")
        expected_files = [
            "server.py",
            "runtime.py",
            "client.py",
            "requirements.txt",
            "README.md",
            "tool_spec.txt",
        ]
        generated_files = []
        for file_name in expected_files:
            file_path = mcpserver_dir / file_name
            if file_path.exists():
                generated_files.append(file_name)
                logger.info(f"   ✅ {file_name}: {file_path.stat().st_size} bytes")
            else:
                logger.warning(f"   ❌ {file_name}: Not found")

        logger.info(
            f"✅ Step 6 PASSED: {len(generated_files)}/{len(expected_files)} files generated"
        )

        logger.info(
            f"🎉 MCP server generation completed successfully in {mcpserver_dir}"
        )

        # Return the actual MCP usage data captured from the generator
        return mcpserver_dir, mcp_usage

    except Exception as e:
        logger.error(f"💥 MCP server generation FAILED with exception: {e}")
        logger.error(f"   Exception type: {type(e).__name__}")
        logger.error(f"   Exception details: {str(e)}")
        import traceback

        logger.error(f"   Full traceback: {traceback.format_exc()}")
        return None, failed_usage(e)
    finally:
        logger.info("🔍 Exiting _generate_mcp_server function")


async def _handle_mcp_generation(
    args: argparse.Namespace,
    evaluation: OpenAPIEvaluationResult,
    openapi_spec: str,
    output_config: OutputConfig,
    result: EnhancementResult,
    model: str = None,
) -> Optional[Path]:
    """Handle MCP server generation if criteria are met."""
    logger.info("🔧 Checking if MCP server generation should be attempted...")

    if args.eval_only:
        logger.info("📋 --eval-only flag set - skipping MCP server generation")
        return None

    if not _check_mcp_generation_criteria(evaluation):
        logger.info(
            "❌ MCP generation criteria not met - skipping MCP server generation"
        )
        return None

    logger.info("✅ MCP generation criteria met - proceeding with generation")

    try:
        if openapi_spec.strip().startswith("{"):
            openapi_spec_dict = json.loads(openapi_spec)
        else:
            openapi_spec_dict = yaml.safe_load(openapi_spec)
        logger.info(f"✅ Parsed OpenAPI spec: {len(openapi_spec_dict)} top-level keys")
    except Exception as e:
        logger.error(f"❌ Failed to parse OpenAPI spec: {e}")
        raise MCPGenerationError("failed to parse OpenAPI specification") from e

    logger.info("🚀 Attempting MCP server generation...")
    mcpserver_path, mcp_usage = await _generate_mcp_server(
        evaluation, openapi_spec_dict, output_config, model=model
    )

    if mcp_usage.get("generation_status") == "failed":
        if mcp_usage:
            _update_evaluation_with_mcp_usage(
                evaluation, mcp_usage, result, output_config, openapi_spec
            )
        validation_errors = mcp_usage.get("validation_errors") or [
            "generated MCP artifact validation failed"
        ]
        raise MCPGenerationError(f"MCP generation failed: {validation_errors[0]}")

    verification_settings = _resolve_generated_artifact_verification_settings(args)
    if verification_settings["verify_enabled"]:
        if not mcpserver_path:
            raise MCPGenerationError(
                "generated artifact directory does not exist for verification"
            )
        if not mcpserver_path.exists():
            raise MCPGenerationError(
                f"generated artifact directory does not exist: {mcpserver_path}"
            )

        verification_kwargs = {
            "seed": verification_settings["seed"],
            "timeout_seconds": verification_settings["timeout_seconds"],
        }
        if verification_settings["repair_enabled"]:
            repairer = _create_generated_artifact_repairer()
            verification_report = await verify_with_repair(
                openapi_spec_dict,
                mcpserver_path,
                repairer=repairer,
                max_repair_attempts=verification_settings["repair_attempts"],
                **verification_kwargs,
            )
        else:
            verification_report = await verify_generated_artifacts(
                openapi_spec_dict,
                mcpserver_path,
                **verification_kwargs,
            )

        verification_report_data = _coerce_generated_artifact_verification_report(
            verification_report
        )

        verification_dir = mcpserver_path / "verification"
        verification_json_path, verification_markdown_path = (
            _save_generated_artifact_verification_report(
                verification_report, verification_dir
            )
        )
        mcp_usage["generated_artifact_verification"] = (
            _build_generated_artifact_verification_metadata(
                verification_report_data,
                verification_json_path,
                verification_markdown_path,
            )
        )

        if verification_report_data.get("status") != "passed":
            mcp_usage["generation_status"] = "failed"
            mcp_usage["validation_errors"] = [
                failure_code
                for failure_code in _extract_failure_codes(
                    verification_report_data.get("failures", [])
                )
            ] or ["generated artifact verification failed"]

    if mcp_usage:
        _update_evaluation_with_mcp_usage(
            evaluation, mcp_usage, result, output_config, openapi_spec
        )

    if mcp_usage.get("generation_status") == "failed":
        validation_errors = mcp_usage.get("validation_errors") or [
            "generated MCP artifact validation failed"
        ]
        raise MCPGenerationError(f"MCP generation failed: {validation_errors[0]}")

    if mcpserver_path:
        logger.info(f"✅ MCP server generation completed: {mcpserver_path}")
    else:
        logger.info("❌ MCP server generation returned None (failed)")

    return mcpserver_path


def _update_evaluation_with_mcp_usage(
    evaluation: OpenAPIEvaluationResult,
    mcp_usage: Dict[str, Any],
    result: EnhancementResult,
    output_config: OutputConfig,
    openapi_spec: str,
) -> None:
    """Update evaluation with MCP generation usage data."""
    logger.info(f"CLI received mcp_usage: {mcp_usage}")
    evaluation.mcp_server_usage = mcp_usage.get("server_usage", {})
    evaluation.mcp_client_usage = mcp_usage.get("client_usage", {})
    logger.info(f"CLI set evaluation.mcp_server_usage: {evaluation.mcp_server_usage}")

    # Update totals
    current_tokens = getattr(evaluation, "total_tokens", 0) or 0
    current_cost = getattr(evaluation, "total_cost_usd", 0.0) or 0.0
    current_calls = getattr(evaluation, "llm_calls_count", 0) or 0

    evaluation.total_tokens = current_tokens + mcp_usage.get("total_tokens", 0)
    evaluation.total_cost_usd = current_cost + mcp_usage.get("total_cost_usd", 0.0)
    evaluation.llm_calls_count = current_calls + mcp_usage.get("calls_count", 0)

    logger.info(
        f"Updated totals - Tokens: {evaluation.total_tokens}, Cost: ${evaluation.total_cost_usd:.6f}, Calls: {evaluation.llm_calls_count}"
    )
    logger.info(
        f"CLI final evaluation usage fields - server: {evaluation.mcp_server_usage}, client: {evaluation.mcp_client_usage}"
    )

    # Re-save the evaluation and summary files with updated MCP usage data
    logger.info("📝 Re-saving evaluation files with updated MCP usage data...")
    try:
        _save_evaluation_files(result, output_config, openapi_spec)
        logger.info("✅ Updated evaluation files saved with MCP usage data")
    except Exception as e:
        logger.error(f"❌ Failed to re-save evaluation files: {e}")


# -----------------------------------------------------------------------------
# Workflow Control Private Functions
# -----------------------------------------------------------------------------


async def _execute_cli_workflow(args: argparse.Namespace) -> bool:
    """Execute the main CLI workflow."""
    try:
        # Prepare execution context
        filename, spec_source, model, output_config = _prepare_execution_context(args)

        logger.info(
            f"🚀 Starting evaluation with {_detect_provider_from_model(model)} provider"
        )
        logger.info(f"📋 Spec source: {spec_source}")
        logger.info(f"🤖 Model: {model}")
        logger.info(f"📊 Enhancement level: {args.enhancement_level}")

        # Load OpenAPI specification
        openapi_spec = await _load_specification(args, filename)

        max_tokens = config.get_int("max_tokens")
        temperature = config.get_float("temperature")

        logger.info(f"Using model from configuration: {model}")
        logger.info(
            f"Model parameters - max_tokens: {max_tokens}, temperature: {temperature}"
        )

        # Run enhancement process with parameters from config.yml
        result = await _run_enhancement_process(
            openapi_spec, args, filename, model=model
        )

        # Save evaluation files
        _save_evaluation_files(result, output_config, openapi_spec)

        # Display results
        evaluation = result.evaluation
        _display_evaluation_results(evaluation)

        # Handle MCP generation with model parameter from config.yml
        mcpserver_path = await _handle_mcp_generation(
            args, evaluation, openapi_spec, output_config, result, model=model
        )

        # Print comprehensive results summary with threshold from config
        generate_threshold = config.get_float("generate_mcp_threshold", 3.0)
        _print_results_summary(
            evaluation, output_config, mcpserver_path, generate_threshold
        )

        # Cleanup
        await cleanup_llm_client()

        logger.info("✅ CLI evaluation completed successfully")
        return True

    except ValidationError as e:
        logger.error(f"❌ Validation error: {e}")
        print(f"❌ Invalid configuration or response format: {e}")
        await cleanup_llm_client()
        return False
    except MCPGenerationError as e:
        logger.error(f"❌ MCP generation failed: {e}")
        print(f"❌ MCP generation failed: {e}")
        await cleanup_llm_client()
        return False
    except Exception as e:
        logger.error(f"❌ Enhancement failed: {e}")
        print(f"❌ Enhancement failed: {e}")
        await cleanup_llm_client()
        return False


# =============================================================================
# PUBLIC FUNCTIONS & CLASSES
# =============================================================================


async def main_cli() -> bool:
    """Main CLI entrypoint for the OpenAPI to MCP converter."""
    try:
        # Parse command line arguments
        parser = _create_argument_parser()
        args = parser.parse_args()

        # Setup logging
        _setup_logging(args.verbose)

        # Handle utility commands
        if args.show_env:
            _show_environment_info()
            return True

        # Validate input arguments
        if not _validate_cli_arguments(args, parser):
            return False

        # Execute main workflow
        return await _execute_cli_workflow(args)

    except KeyboardInterrupt:
        logger.info("❌ Operation cancelled by user")
        print("\n❌ Operation cancelled by user")
        await cleanup_llm_client()
        return False
    except Exception as e:
        logger.error(f"❌ CLI operation failed: {e}")
        print(f"❌ CLI operation failed: {e}")
        await cleanup_llm_client()
        return False


def cli_main() -> None:
    """Sync entry point wrapper for the CLI."""
    success = asyncio.run(main_cli())
    exit(0 if success else 1)


if __name__ == "__main__":
    cli_main()
