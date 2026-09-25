"""Repair strategies for generated artifacts."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Protocol, Sequence, runtime_checkable

from src.models.generated_validation import (
    GeneratedArtifactVerificationResult,
    RepairAttemptResult,
)


_ALLOWED_FILES = ("server.py", "client.py", "runtime.py")
_SECRET_ENV_NAME_PATTERN = re.compile(r"(token|secret|password|api[_-]?key|auth)", re.IGNORECASE)


@runtime_checkable
class GeneratedArtifactRepairer(Protocol):
    async def repair(
        self,
        openapi_spec: Mapping[str, Any],
        report: GeneratedArtifactVerificationResult,
        attempt: int,
        candidate_dir: Path,
    ) -> RepairAttemptResult: ...


class NoOpRepairer:
    """A repairer that never changes the candidate artifact."""

    def __init__(self, reason: str = "repair_disabled") -> None:
        self.reason = reason

    async def repair(
        self,
        openapi_spec: Mapping[str, Any],
        report: GeneratedArtifactVerificationResult,
        attempt: int,
        candidate_dir: Path,
    ) -> RepairAttemptResult:
        del openapi_spec, report
        return RepairAttemptResult(
            attempt=attempt,
            changed_files=[],
            candidate_dir=str(candidate_dir.resolve()),
            accepted=False,
            failure_codes=[self.reason],
        )


class LLMGeneratedArtifactRepairer:
    """Repair generated artifacts with an LLM-backed candidate patch."""

    def __init__(self, llm_client: Any | None = None) -> None:
        if llm_client is None:
            from .llm_client import get_llm_client

            llm_client = get_llm_client()
        self._llm_client = llm_client

    async def repair(
        self,
        openapi_spec: Mapping[str, Any],
        report: GeneratedArtifactVerificationResult,
        attempt: int,
        candidate_dir: Path,
    ) -> RepairAttemptResult:
        del openapi_spec

        candidate_sources = _load_candidate_sources(candidate_dir)
        prompt = _build_prompt(report, attempt, candidate_sources)

        try:
            from .llm_client import LLMRequest

            llm_response = await self._llm_client.generate_text(
                LLMRequest(prompt=prompt, max_tokens=2000, temperature=0.0)
            )
        except Exception as exc:  # pragma: no cover - exercised via failure tests
            return RepairAttemptResult(
                attempt=attempt,
                changed_files=[],
                candidate_dir=str(candidate_dir.resolve()),
                accepted=False,
                failure_codes=[f"llm_error:{type(exc).__name__}"],
            )

        try:
            files = _parse_repair_response(llm_response.text)
        except ValueError as exc:
            return RepairAttemptResult(
                attempt=attempt,
                changed_files=[],
                candidate_dir=str(candidate_dir.resolve()),
                accepted=False,
                failure_codes=[str(exc)],
            )

        changed_files: list[str] = []
        for filename, content in files.items():
            path = candidate_dir / filename
            path.write_text(content, encoding="utf-8")
            changed_files.append(filename)

        return RepairAttemptResult(
            attempt=attempt,
            changed_files=changed_files,
            candidate_dir=str(candidate_dir.resolve()),
            accepted=False,
            failure_codes=[],
        )


def _load_candidate_sources(candidate_dir: Path) -> dict[str, str]:
    sources: dict[str, str] = {}
    for filename in _ALLOWED_FILES:
        path = candidate_dir / filename
        sources[filename] = path.read_text(encoding="utf-8") if path.exists() else ""
    return sources


def _build_prompt(
    report: GeneratedArtifactVerificationResult,
    attempt: int,
    candidate_sources: Mapping[str, str],
) -> str:
    report_json = json.dumps(
        _sanitize_report(report), ensure_ascii=False, indent=2
    )
    file_blocks = []
    for filename in _ALLOWED_FILES:
        file_blocks.append(f"{filename}:\n{candidate_sources.get(filename, '')}")
    return (
        "Repair the generated MCP artifact using only the redacted report and the "
        "allowlisted source files below.\n"
        f"Attempt: {attempt}\n\n"
        "Return strict JSON exactly in this shape:\n"
        '{"files": {"server.py": "...", "client.py": "...", "runtime.py": "..."}}\n\n'
        "Redacted report:\n"
        f"{report_json}\n\n"
        "Allowlisted source files:\n"
        + "\n\n".join(file_blocks)
    )


def _sanitize_report(report: GeneratedArtifactVerificationResult) -> dict[str, Any]:
    payload = report.model_dump(mode="json")
    secret_values = _secret_env_values()
    operations = []
    for operation in payload.get("operations", []):
        if not isinstance(operation, Mapping):
            continue
        operations.append(
            {
                "operation_id": operation.get("operation_id"),
                "tool_name": operation.get("tool_name"),
                "scenario_kind": operation.get("scenario_kind"),
                "status": operation.get("status"),
                "request_valid": operation.get("request_valid"),
                "response_valid": operation.get("response_valid"),
                "error_kind": operation.get("error_kind"),
                "failure_code": operation.get("failure_code"),
                "redacted_input": _sanitize_value(
                    operation.get("redacted_input"), secret_values
                ),
                "redacted_output": _sanitize_value(
                    operation.get("redacted_output"), secret_values
                ),
            }
        )
    return {
        "status": payload.get("status"),
        "attempts": payload.get("attempts"),
        "spec_sha256": payload.get("spec_sha256"),
        "generator_version": payload.get("generator_version"),
        "seed": payload.get("seed"),
        "failures": _sanitize_value(payload.get("failures"), secret_values),
        "operations": operations,
    }


def _sanitize_value(value: Any, secret_values: Sequence[str]) -> Any:
    if isinstance(value, str):
        return _redact_secrets(value, secret_values)
    if isinstance(value, Mapping):
        return {
            str(key): _sanitize_value(item, secret_values)
            for key, item in value.items()
            if str(key) != "message"
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [_sanitize_value(item, secret_values) for item in value]
    return value


def _secret_env_values() -> list[str]:
    return list(
        dict.fromkeys(
            value
            for name, value in os.environ.items()
            if value and _SECRET_ENV_NAME_PATTERN.search(name)
        )
    )


def _redact_secrets(value: str, secret_values: Sequence[str]) -> str:
    redacted = value
    for secret in secret_values:
        if secret:
            redacted = redacted.replace(secret, "[REDACTED]")
    return redacted


def _parse_repair_response(text: str) -> dict[str, str]:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError("llm_repair_response_must_be_json") from exc
    if not isinstance(payload, Mapping):
        raise ValueError("llm_repair_response_must_be_json_object")
    if set(payload) != {"files"}:
        raise ValueError("llm_repair_response_must_contain_only_files")

    files = payload["files"]
    if not isinstance(files, Mapping):
        raise ValueError("llm_repair_response_files_must_be_object")

    expected_files = set(_ALLOWED_FILES)
    actual_files = {str(name) for name in files}
    if actual_files != expected_files:
        raise ValueError("llm_repair_response_must_list_only_server_client_runtime")

    parsed: dict[str, str] = {}
    for filename in _ALLOWED_FILES:
        content = files.get(filename)
        if not isinstance(content, str):
            raise ValueError("llm_repair_response_file_contents_must_be_strings")
        parsed[filename] = content
    return parsed