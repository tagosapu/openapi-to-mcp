from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.models.generated_validation import (
    GeneratedArtifactVerificationResult,
    OperationVerificationResult,
    RepairAttemptResult,
    VerificationStatus,
)
from src.services.generated_artifact_repair import (
    LLMGeneratedArtifactRepairer,
    NoOpRepairer,
)
from src.services.generated_artifact_verifier import verify_with_repair
from src.services.openapi_mcp_codegen import write_generated_artifacts


def _spec() -> dict:
    return {
        "openapi": "3.0.3",
        "info": {"title": "Users API", "version": "1.0.0"},
        "paths": {
            "/users/{userId}": {
                "get": {
                    "operationId": "getUser",
                    "parameters": [
                        {
                            "name": "userId",
                            "in": "path",
                            "required": True,
                            "schema": {"type": "integer", "minimum": 1},
                        }
                    ],
                    "responses": {
                        "200": {
                            "description": "Success",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "required": ["id"],
                                        "properties": {
                                            "id": {"type": "integer", "minimum": 1}
                                        },
                                    }
                                }
                            },
                        }
                    },
                }
            }
        },
    }


def _spec_hash(spec: dict) -> str:
    serialized = json.dumps(spec, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


class FakeRepairer:
    def __init__(self, replacements_by_attempt: dict[int, dict[str, str]]) -> None:
        self.replacements_by_attempt = replacements_by_attempt
        self.calls: list[int] = []

    async def repair(self, openapi_spec, report, attempt, candidate_dir):
        del openapi_spec, report
        self.calls.append(attempt)
        for filename, content in self.replacements_by_attempt.get(attempt, {}).items():
            (candidate_dir / filename).write_text(content, encoding="utf-8")
        changed_files = list(self.replacements_by_attempt.get(attempt, {}))
        return RepairAttemptResult(
            attempt=attempt,
            changed_files=changed_files,
            candidate_dir=str(candidate_dir.resolve()),
            accepted=False,
            failure_codes=[],
        )


class TraversalRepairer:
    def __init__(self) -> None:
        self.calls = 0

    async def repair(self, openapi_spec, report, attempt, candidate_dir):
        del openapi_spec, report
        self.calls += 1
        escape_dir = candidate_dir / ".." / ".." / "escape-dir"
        escape_dir.mkdir(parents=True, exist_ok=True)
        return RepairAttemptResult(
            attempt=attempt,
            changed_files=["../../escape-dir"],
            candidate_dir=str(candidate_dir.resolve()),
            accepted=False,
            failure_codes=[],
        )


class BackupPoisoningRepairer:
    def __init__(self, valid_server: str) -> None:
        self.valid_server = valid_server
        self.calls = 0

    async def repair(self, openapi_spec, report, attempt, candidate_dir):
        del openapi_spec, report, attempt
        self.calls += 1
        poisoned_backup = candidate_dir.parent / "original" / "server.py"
        poisoned_backup.parent.mkdir(parents=True, exist_ok=True)
        poisoned_backup.write_text("poisoned original", encoding="utf-8")
        (candidate_dir / "server.py").write_text(self.valid_server, encoding="utf-8")
        return RepairAttemptResult(
            attempt=1,
            changed_files=["server.py", "../original/server.py"],
            candidate_dir=str(candidate_dir.resolve()),
            accepted=False,
            failure_codes=[],
        )


class VerificationRootPoisoningRepairer:
    def __init__(self, valid_server: str) -> None:
        self.valid_server = valid_server
        self.calls: list[int] = []

    async def repair(self, openapi_spec, report, attempt, candidate_dir):
        del openapi_spec, report
        self.calls.append(attempt)
        (candidate_dir / "server.py").write_text(self.valid_server, encoding="utf-8")
        if attempt == 1:
            poisoned_dir = candidate_dir.parent / "original" / "escape"
            poisoned_dir.mkdir(parents=True, exist_ok=True)
        return RepairAttemptResult(
            attempt=attempt,
            changed_files=["server.py"] + (["../original/escape"] if attempt == 1 else []),
            candidate_dir=str(candidate_dir.resolve()),
            accepted=False,
            failure_codes=[],
        )


class EmptyDirectoryRepairer:
    def __init__(self, valid_server: str) -> None:
        self.valid_server = valid_server
        self.calls = 0

    async def repair(self, openapi_spec, report, attempt, candidate_dir):
        del openapi_spec, report, attempt
        self.calls += 1
        (candidate_dir / "server.py").write_text(self.valid_server, encoding="utf-8")
        (candidate_dir / "empty-dir").mkdir()
        return RepairAttemptResult(
            attempt=1,
            changed_files=["server.py", "empty-dir"],
            candidate_dir=str(candidate_dir.resolve()),
            accepted=False,
            failure_codes=[],
        )


class MutatingRepairer:
    def __init__(self, valid_server: str) -> None:
        self.valid_server = valid_server
        self.calls = 0

    async def repair(self, openapi_spec, report, attempt, candidate_dir):
        del openapi_spec, attempt
        self.calls += 1
        original_dir = Path(report.artifact_dir)
        (original_dir / "server.py").write_text("mutated original", encoding="utf-8")
        (candidate_dir / "server.py").write_text(self.valid_server, encoding="utf-8")
        return RepairAttemptResult(
            attempt=1,
            changed_files=["server.py"],
            candidate_dir=str(candidate_dir.resolve()),
            accepted=False,
            failure_codes=[],
        )


class SpecMutatingRepairer:
    def __init__(self, server_content: str) -> None:
        self.server_content = server_content
        self.calls = 0
        self.mutation_error: type[BaseException] | None = None

    async def repair(self, openapi_spec, report, attempt, candidate_dir):
        del report, attempt
        self.calls += 1
        try:
            openapi_spec["paths"]["/users/{userId}"]["get"]["operationId"] = "mutated-operation"
            openapi_spec["paths"]["/users/{userId}"]["get"]["parameters"][0]["schema"][
                "minimum"
            ] = 999
        except TypeError as exc:
            self.mutation_error = type(exc)
        (candidate_dir / "server.py").write_text(self.server_content, encoding="utf-8")
        return RepairAttemptResult(
            attempt=1,
            changed_files=["server.py"],
            candidate_dir=str(candidate_dir.resolve()),
            accepted=False,
            failure_codes=[],
        )


class SymlinkRepairer:
    def __init__(self, valid_server: str) -> None:
        self.valid_server = valid_server
        self.calls = 0

    async def repair(self, openapi_spec, report, attempt, candidate_dir):
        del openapi_spec, report, attempt
        self.calls += 1
        (candidate_dir / "server.py").write_text(self.valid_server, encoding="utf-8")
        link_path = candidate_dir / "linked_escape.py"
        try:
            os.symlink("../escape.txt", link_path)
        except (OSError, NotImplementedError) as exc:
            pytest.skip(f"symlinks unavailable: {exc}")
        return RepairAttemptResult(
            attempt=1,
            changed_files=["server.py", "linked_escape.py"],
            candidate_dir=str(candidate_dir.resolve()),
            accepted=False,
            failure_codes=[],
        )


class FakeLLMClient:
    def __init__(self, text: str) -> None:
        self.text = text
        self.requests: list[str] = []

    async def generate_text(self, request):
        self.requests.append(request.prompt)
        return SimpleNamespace(text=self.text)


def _write_broken_artifact(tmp_path: Path) -> tuple[Path, str]:
    spec = _spec()
    artifact_dir = tmp_path / "mcpserver"
    write_generated_artifacts(spec, artifact_dir)
    valid_server = (artifact_dir / "server.py").read_text(encoding="utf-8")
    (artifact_dir / "server.py").write_text("definitely invalid python", encoding="utf-8")
    return artifact_dir, valid_server


@pytest.mark.asyncio
async def test_repair_candidate_is_adopted_only_after_full_reverification(tmp_path):
    spec = _spec()
    artifact_dir, valid_server = _write_broken_artifact(tmp_path)

    repairer = FakeRepairer({1: {"server.py": valid_server}})
    result = await verify_with_repair(
        spec,
        artifact_dir,
        repairer=repairer,
        seed=0,
        max_repair_attempts=1,
    )

    assert result.status == VerificationStatus.PASSED
    assert (artifact_dir / "server.py").read_text(encoding="utf-8") == valid_server
    assert result.repair_attempts[0].accepted is True
    assert repairer.calls == [1]


@pytest.mark.asyncio
async def test_repair_failure_restores_original_artifact(tmp_path):
    spec = _spec()
    artifact_dir, _valid_server = _write_broken_artifact(tmp_path)

    repairer = FakeRepairer({1: {"server.py": "from pathlib import Path\n"}})
    original_server = (artifact_dir / "server.py").read_text(encoding="utf-8")
    result = await verify_with_repair(
        spec,
        artifact_dir,
        repairer=repairer,
        seed=0,
        max_repair_attempts=1,
    )

    assert result.status == VerificationStatus.FAILED
    assert (artifact_dir / "server.py").read_text(encoding="utf-8") == original_server
    assert result.repair_attempts[0].accepted is False
    assert repairer.calls == [1]


@pytest.mark.asyncio
@pytest.mark.parametrize("repairer_factory", ["success", "failure"], ids=["success", "failure"])
async def test_verify_with_repair_keeps_spec_hash_stable(tmp_path, repairer_factory):
    spec = _spec()
    artifact_dir, valid_server = _write_broken_artifact(tmp_path)
    original_hash = _spec_hash(spec)

    if repairer_factory == "success":
        repairer = SpecMutatingRepairer(valid_server)
    else:
        repairer = SpecMutatingRepairer("from pathlib import Path\n")

    await verify_with_repair(
        spec,
        artifact_dir,
        repairer=repairer,
        seed=0,
        max_repair_attempts=1,
    )

    assert _spec_hash(spec) == original_hash
    assert repairer.mutation_error is TypeError


@pytest.mark.asyncio
async def test_verify_with_repair_rejects_symlinked_candidate_entries(tmp_path):
    spec = _spec()
    artifact_dir, valid_server = _write_broken_artifact(tmp_path)
    original_server = (artifact_dir / "server.py").read_text(encoding="utf-8")

    repairer = SymlinkRepairer(valid_server)
    result = await verify_with_repair(
        spec,
        artifact_dir,
        repairer=repairer,
        seed=0,
        max_repair_attempts=1,
    )

    assert result.status == VerificationStatus.FAILED
    assert (artifact_dir / "server.py").read_text(encoding="utf-8") == original_server
    assert repairer.calls == 1
    assert result.repair_attempts[0].accepted is False
    assert "candidate_symlink_detected:linked_escape.py" in result.repair_attempts[0].failure_codes


@pytest.mark.asyncio
async def test_verify_with_repair_rejects_extra_empty_candidate_directory(tmp_path):
    spec = _spec()
    artifact_dir, valid_server = _write_broken_artifact(tmp_path)
    original_server = (artifact_dir / "server.py").read_text(encoding="utf-8")

    repairer = EmptyDirectoryRepairer(valid_server)
    result = await verify_with_repair(
        spec,
        artifact_dir,
        repairer=repairer,
        seed=0,
        max_repair_attempts=1,
    )

    assert result.status == VerificationStatus.FAILED
    assert (artifact_dir / "server.py").read_text(encoding="utf-8") == original_server
    assert repairer.calls == 1
    assert result.repair_attempts[0].accepted is False
    assert "candidate_tree_state_mismatch" in result.repair_attempts[0].failure_codes


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "max_repair_attempts,expected_calls",
    [(0, 0), (2, 2), (5, 2), (-1, 0)],
)
async def test_verify_with_repair_clamps_attempts(tmp_path, max_repair_attempts, expected_calls):
    spec = _spec()
    artifact_dir, _valid_server = _write_broken_artifact(tmp_path)
    repairer = FakeRepairer({1: {}, 2: {}})

    await verify_with_repair(
        spec,
        artifact_dir,
        repairer=repairer,
        seed=0,
        max_repair_attempts=max_repair_attempts,
    )

    assert len(repairer.calls) == expected_calls


@pytest.mark.asyncio
async def test_verify_with_repair_rejects_path_traversal_and_restores_original(tmp_path):
    spec = _spec()
    artifact_dir, _valid_server = _write_broken_artifact(tmp_path)
    original_server = (artifact_dir / "server.py").read_text(encoding="utf-8")

    repairer = TraversalRepairer()
    result = await verify_with_repair(
        spec,
        artifact_dir,
        repairer=repairer,
        seed=0,
        max_repair_attempts=1,
    )

    assert result.status == VerificationStatus.FAILED
    assert (artifact_dir / "server.py").read_text(encoding="utf-8") == original_server
    assert not (artifact_dir / "escape-dir").exists()
    assert repairer.calls == 1
    assert result.repair_attempts[0].accepted is False
    assert "original_artifact_mutated" in result.repair_attempts[0].failure_codes


@pytest.mark.asyncio
async def test_verify_with_repair_rejects_backup_dir_poisoning_and_restores_original(tmp_path):
    spec = _spec()
    artifact_dir, valid_server = _write_broken_artifact(tmp_path)
    original_server = (artifact_dir / "server.py").read_text(encoding="utf-8")

    repairer = BackupPoisoningRepairer(valid_server)
    result = await verify_with_repair(
        spec,
        artifact_dir,
        repairer=repairer,
        seed=0,
        max_repair_attempts=1,
    )

    assert result.status == VerificationStatus.FAILED
    assert (artifact_dir / "server.py").read_text(encoding="utf-8") == original_server
    assert not (artifact_dir / ".verification").exists()
    assert repairer.calls == 1
    assert result.repair_attempts[0].accepted is False
    assert "candidate_path_traversal" in result.repair_attempts[0].failure_codes


@pytest.mark.asyncio
async def test_verify_with_repair_resets_verification_root_between_attempts(tmp_path):
    spec = _spec()
    artifact_dir, valid_server = _write_broken_artifact(tmp_path)
    original_server = (artifact_dir / "server.py").read_text(encoding="utf-8")

    repairer = VerificationRootPoisoningRepairer(valid_server)
    result = await verify_with_repair(
        spec,
        artifact_dir,
        repairer=repairer,
        seed=0,
        max_repair_attempts=2,
    )

    assert result.status == VerificationStatus.PASSED
    assert (artifact_dir / "server.py").read_text(encoding="utf-8") == valid_server
    assert not (artifact_dir / ".verification").exists()
    assert (artifact_dir / "server.py").read_text(encoding="utf-8") != original_server
    assert repairer.calls == [1, 2]
    assert [attempt.accepted for attempt in result.repair_attempts] == [False, True]
    assert "candidate_path_traversal" in result.repair_attempts[0].failure_codes
    assert result.repair_attempts[1].failure_codes == []


@pytest.mark.asyncio
async def test_verify_with_repair_rejects_original_artifact_mutation(tmp_path):
    spec = _spec()
    artifact_dir, valid_server = _write_broken_artifact(tmp_path)
    original_server = (artifact_dir / "server.py").read_text(encoding="utf-8")

    repairer = MutatingRepairer(valid_server)
    result = await verify_with_repair(
        spec,
        artifact_dir,
        repairer=repairer,
        seed=0,
        max_repair_attempts=1,
    )

    assert result.status == VerificationStatus.FAILED
    assert (artifact_dir / "server.py").read_text(encoding="utf-8") == original_server
    assert repairer.calls == 1
    assert result.repair_attempts[0].accepted is False
    assert "original_artifact_mutated" in result.repair_attempts[0].failure_codes


@pytest.mark.asyncio
async def test_noop_repairer_reports_disabled_reason(tmp_path):
    candidate_dir = tmp_path / "candidate"
    candidate_dir.mkdir()
    report = GeneratedArtifactVerificationResult(
        artifact_dir=str(tmp_path / "artifact"),
        spec_sha256="abc",
        generator_version="test",
        seed=1,
        status=VerificationStatus.FAILED,
        attempts=1,
        operations=[
            OperationVerificationResult(
                operation_id="getUser",
                tool_name="getUser",
                scenario_kind="success",
                status=VerificationStatus.FAILED,
                request_valid=False,
                response_valid=False,
                failure_code="request_schema_mismatch",
            )
        ],
    )

    result = await NoOpRepairer().repair(_spec(), report, 1, candidate_dir)

    assert result.accepted is False
    assert result.failure_codes == ["repair_disabled"]


@pytest.mark.asyncio
async def test_llm_repairer_parses_strict_json_and_writes_allowlisted_files(tmp_path):
    candidate_dir = tmp_path / "candidate"
    candidate_dir.mkdir()
    for filename in ("server.py", "client.py", "runtime.py"):
        (candidate_dir / filename).write_text("original", encoding="utf-8")
    report = GeneratedArtifactVerificationResult(
        artifact_dir=str(tmp_path / "artifact"),
        spec_sha256="abc",
        generator_version="test",
        seed=1,
        status=VerificationStatus.FAILED,
        attempts=1,
        operations=[],
    )
    llm_client = FakeLLMClient(
        json.dumps(
            {
                "files": {
                    "server.py": "server-new",
                    "client.py": "client-new",
                    "runtime.py": "runtime-new",
                }
            }
        )
    )

    result = await LLMGeneratedArtifactRepairer(llm_client=llm_client).repair(
        _spec(), report, 1, candidate_dir
    )

    assert result.changed_files == ["server.py", "client.py", "runtime.py"]
    assert (candidate_dir / "server.py").read_text(encoding="utf-8") == "server-new"
    assert (candidate_dir / "client.py").read_text(encoding="utf-8") == "client-new"
    assert (candidate_dir / "runtime.py").read_text(encoding="utf-8") == "runtime-new"


@pytest.mark.asyncio
async def test_llm_repairer_sanitizes_report_messages_and_secrets(tmp_path, monkeypatch):
    candidate_dir = tmp_path / "candidate"
    candidate_dir.mkdir()
    for filename in ("server.py", "client.py", "runtime.py"):
        (candidate_dir / filename).write_text("original", encoding="utf-8")
    sentinel_secret = "sentinel-secret-value"
    monkeypatch.setenv("API_KEY", sentinel_secret)
    report = GeneratedArtifactVerificationResult(
        artifact_dir=str(tmp_path / "artifact"),
        spec_sha256="abc",
        generator_version="test",
        seed=1,
        status=VerificationStatus.FAILED,
        attempts=1,
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
                redacted_input={"detail": f"input {sentinel_secret}"},
                redacted_output={"detail": f"output {sentinel_secret}"},
            )
        ],
        failures=["request_schema_mismatch"],
    )
    llm_client = FakeLLMClient(
        json.dumps(
            {
                "files": {
                    "server.py": "server-new",
                    "client.py": "client-new",
                    "runtime.py": "runtime-new",
                }
            }
        )
    )

    await LLMGeneratedArtifactRepairer(llm_client=llm_client).repair(
        _spec(), report, 1, candidate_dir
    )

    prompt = llm_client.requests[0]
    assert sentinel_secret not in prompt
    assert "raw validation message" not in prompt
    assert "request_schema_mismatch" in prompt
    assert "failure_code" in prompt


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response_text", "expected_code"),
    [
        ("not json", "llm_repair_response_must_be_json"),
        (
            json.dumps({"files": {"server.py": "x", "client.py": "y", "docs.md": "z"}}),
            "llm_repair_response_must_list_only_server_client_runtime",
        ),
    ],
)
async def test_llm_repairer_rejects_malformed_responses(
    tmp_path, response_text, expected_code
):
    candidate_dir = tmp_path / "candidate"
    candidate_dir.mkdir()
    for filename in ("server.py", "client.py", "runtime.py"):
        (candidate_dir / filename).write_text("original", encoding="utf-8")
    report = GeneratedArtifactVerificationResult(
        artifact_dir=str(tmp_path / "artifact"),
        spec_sha256="abc",
        generator_version="test",
        seed=1,
        status=VerificationStatus.FAILED,
        attempts=1,
        operations=[],
    )

    result = await LLMGeneratedArtifactRepairer(
        llm_client=FakeLLMClient(response_text)
    ).repair(_spec(), report, 1, candidate_dir)

    assert result.accepted is False
    assert expected_code in result.failure_codes